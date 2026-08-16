# Plan、dry-run、落盘与验收契约

## 目录

- [1. TV plan](#1-tv-plan)
- [2. Movie plan](#2-movie-plan)
- [3. episode 与 artwork 规则](#3-episode-与-artwork-规则)
- [4. dry-run 报告](#4-dry-run-报告)
- [5. 落盘模式](#5-落盘模式)
- [6. 审查与验收](#6-审查与验收)
- [7. Jellyfin 约束](#7-jellyfin-约束)
- [8. 进度账本](#8-进度账本)

## 1. TV plan

```json
{
  "plan_schema": "anime-scraper-plan",
  "type": "tv",
  "output_dir": "<单元独占时直接填片源根；仅共享根且名称冲突时填独立元数据目录>",
  "library_projection": {"hardlinks_enabled": true, "link_root": "<硬链接库根绝对路径>"},
  "refresh_artwork": false,
  "tmdb_identity": {"id": 66931, "status": "verified", "first_air_date": "YYYY-MM-DD", "name": "<TMDB 名称>", "original_name": "<原名>"},
  "artwork_review": "<完整结构见 §3 的 artwork_review 契约>",
  "show": {
    "title": "<最终显示标题>",
    "sorttitle": "<Agent 确认的系列主标题 YYYY-MM-DD 最终标题>",
    "rating": 8.7,
    "plot": "<作品简介>",
    "premiered": "YYYY-MM-DD",
    "studio": "<制作公司>",
    "actors": [
      {"name": "人员", "role": "角色/原职位", "thumb": "URL", "type": "Actor/Director/Writer/...", "bangumi_person_id": 123}
    ],
    "staff_note": "<无对应类型的职位说明>",
    "staff_status": "present|empty",
    "staff_audit": {"persons_checked": true, "source_person_count": 123, "mappable_crew_count": 4},
    "anidb_aid": 123,
    "bgm_id": 456,
    "lockdata": true
  },
  "episodes": [
    {
      "category": "normal|special|credit|trailer|other",
      "season": 1,
      "episode": 1,
      "title": "<分集标题>",
      "plot": "<可为空；正片为空时必须有 plot_evidence>",
      "plot_evidence": {"bangumi_zh": "present|empty", "tmdb_zh": "present|empty", "bangumi_ja": "present|empty", "tmdb_en": "present|empty"},
      "airdate": "YYYY-MM-DD",
      "runtime": 24,
      "anidb_epno": "S1/C1/...",
      "anidb_type": "special/credit/...",
      "special_order": {"priority": 10, "series_key": "OVA", "series_order": 1, "item_order": 1, "source_index": 0},
      "tmdb_match_status": "matched|not_found|unknown",
      "tmdb_still_url": "<已认证 TMDB still URL；无对应 still 时为空或省略>",
      "song_evidence": {"status": "resolved|exhausted", "sources": ["<kind>:<value>"], "note": "<对应关系或未解决原因>"},
      "directors": [],
      "writers": [],
      "video_path": "<绝对路径或 null>"
    }
  ],
  "artwork": []
}
```

plan 必须声明 `plan_schema: anime-scraper-plan`、`sorttitle`、`lockdata: true` 与 `library_projection`。`rating` 只在 Bangumi 有有效投票时填写。

### 1.1 plan 骨架草稿与 answers 合并（plan_scaffold.py）

TV plan 优先由 `scripts/plan_scaffold.py` 从元数据快照 + manifest 预生成草稿，Agent 不再逐字手写整份 JSON。分工固定：

- 脚本填确定性字段：`plan_schema`/`type`/`library_projection`（config 合成快照）、show 级 `bgm_id`/`anidb_aid`/`premiered`/`rating`/`studio`/staff（复用 `match.populate_show_staff`，含 `staff_status`/`staff_audit`）、正片集号/标题/airdate/runtime/Bangumi 简介、集号唯一对应时的 `video_path`，以及每个已匹配视频的同 stem frame thumb artwork。
- Agent 只写一份小体积 answers sidecar（`anime-scraper-todo-apply-v1`），包含语义结论：`output_dir`、`sorttitle`/`title`/`plot`/`studio` 覆盖、`tmdb_identity`、`episode_patches`（正片补丁：plot/`plot_evidence`/`tmdb_match_status` 等）、`specials`（worksheet 特殊件的 S00E 语义匹配结论，credit 必带 `song_evidence`，thumb 选 frame/tmdb）、`skipped_specials`/`skipped_normal`（六类略过与重复文件，必带理由）、`add_episodes`（冲突集号择一、超出集号表的正片）、`posters`（主/Specials/fanart 等作品级图片决策）、`artwork_review_plan`（紧凑审查记录路径）或内联 `artwork_review`、`extra_artwork`。
- `--apply-todo` 合并时由脚本完成：S00E 条目生成、全部 thumb artwork、`library_relpath` 回填（stem 与建库规则同源）、作品级海报条目、`artwork_review` 嵌入、worksheet 全覆盖与空 plot/credit 护栏校验，并删除整个 `scaffold` 块；草稿原文件保留为审查证据。`scrape.py` 在 dry-run 与实际落盘的 schema 检查阶段一律拒绝仍含该块的 plan。骨架与合并不改变 §6 的任何审查义务。
- 草稿中的 `plot_source`（`bangumi_desc`/`tmdb_overview`）只是来源备注，Agent 审查后可保留；集号重复或多版本文件不会被脚本硬配，列入 `scaffold.worksheet.ambiguous_episodes`，必须在 answers 的 `add_episodes`/`skipped_normal` 中逐一解决。
- movie plan 结构简单，仍由 Agent 直接编写。

当 TV 的 Season 0 实际入库条目超过 1 个时，每条都必须有 `special_order`；其含义和排序键以 `special-rules.md` §3-a 为准。脚本只校验 Agent 已给出的字段和顺序，不替 Agent 判断标题类别。

sorttitle 的字段结构（单字符串，不新增 `series_title`/`franchise_group`）、前缀语义、脚本反向拆分校验与安全回退均以 `metadata-rules.md` §3 为 canonical；dry-run 只做本地结构与后缀校验，不联网。

`library_projection` 快照 config 与本次 CLI 合成后的最终状态。启用时记录解析后的绝对 `link_root`：配置 root 非空就使用该值，为空则记录 `<paths.source_root>/_Jellyfin`；关闭时必须为 `null`。dry-run 和实际落盘都会核对开关与目标，防止审查后配置漂移。

单元独占一个片源根目录时，`output_dir` 默认直接指向该根，禁止只为 `tvshow.nfo` / `movie.nfo` 和作品级图片额外创建 `meta` / `metadata` 子目录；`output_dir` 末级目录恰为 `meta` 或 `metadata` 时，dry-run 和实际落盘都必须拒绝。只有多个单元共用同一片源根、固定文件名会冲突时，才使用能标识单元的独立目录，且各单元目录统一放在同一 `meta/` 父目录下（如 `meta/s1/`、`meta/movie1/`），不得散落成 `<根>/s1-meta/`，也不得使用通用元数据目录。

`bangumi_person_id` 只用于声优去重，不写入 NFO；缺失时按姓名判断。组装时合并同一声优的角色，最多保留 20 位并置于 crew 前；crew 不占额度。

`artwork_review` 由 `scripts/artwork_review.py --compact-review` 确定性生成，示例中的空数组仅表示结构位置。每个 plan 必须保存 `multimodal_review_enabled` 与 `selection_method`：关闭为 `disabled/deterministic_existing_pipeline`，沿用开关前已完成的排序、感知哈希去重和回退结果；开启后的多候选为 `completed/agent_multimodal`；开启但全部组单候选为 `not_required/deterministic_single_candidate`。`original_cache_dir` 仅在 `config.artwork.artwork_cache=true` 且原图缓存目录成功建立时写入；关闭或缓存失败都不构成 dry-run 门槛，也不因重复运行续期。缓存失败只形成 partial/failed 记录，不改变当前选图。人工替换优先使用 `candidate_id`（如 `G01-C07`）和该目录，只有编号找不到时才使用 manifest 中的 `cache_path`，替换器只从本地缓存 staging，不重新请求 TMDB。

## 2. Movie plan

```json
{
  "plan_schema": "anime-scraper-plan",
  "type": "movie",
  "output_dir": "<单元独占时直接填片源根；仅共享根且名称冲突时填独立元数据目录>",
  "library_projection": {"hardlinks_enabled": true, "link_root": "<硬链接库根绝对路径>"},
  "artwork_review": "<完整结构见 §3 的 artwork_review 契约>",
    "movie": {
      "title": "<电影标题>",
      "sorttitle": "<Agent 确认的系列主标题 YYYY-MM-DD 最终标题>",
      "plot": "<可为空；主片为空时必须有 plot_evidence>",
      "plot_evidence": {"bangumi_zh": "present|empty", "tmdb_zh": "present|empty", "bangumi_ja": "present|empty", "tmdb_en": "present|empty"},
      "premiered": "YYYY-MM-DD",
    "video_path": "<主片绝对路径>",
    "lockdata": true
  },
  "extras": [],
  "artwork": []
}
```

`extras` 与 TV `episodes` 结构相同，统一使用 `season: 0`。落库位置为电影目录的 `extras/`，命名采用 `电影名 S00Exx`。

## 3. episode 与 artwork 规则

- `video_path: null` 只用于文件损坏、重复、明确不属于本作品等待人工项；不会生成 NFO。
- 六类规则性略过项完全不进入 plan。
- 所有会入库的 Season 0 / movie extras 必须有非空 `airdate`；dry-run 与实际落盘都会调用 `validate_special_airdates`。
- 每个入库 credit 必填 `song_evidence` 与 `note`；source kind 只用 `official/wiki/bangumi/local_cd/web/network_error`。带歌名用 `resolved`；裸 OP/ED 用 `exhausted`，且至少记录一次 Web 查询或网络失败。缺失、只查 Bangumi/CD 或标题与状态矛盾会被拒绝。
- `tmdb_match_status` 只记录实际 TMDB 认证状态；字段缺失或非法按 `unknown`，不得据此清空其它可靠来源的 plot。
- Season 0 图片选择顺序是“先查 TMDB，再决定回退”：只要出现 `tmdb_match_status=matched` 或非空 `tmdb_still_url`，plan 必须记录 `tmdb_identity`（正整数 `id`、`status`、`first_air_date`、名称）；仅当 `status=verified`、与本地单元首播年份一致且有对应 still 时使用 `method=tmdb`。跨作合并、身份不确定、查询失败或没有对应 still 时，去掉 `tmdb_still_url`，改用 `method=frame` 并标记 `unknown`/`not_found`；不得跳过 TMDB 查询后直接默认截帧。
- `show/movie.staff_note` 只保留无干净类型的职位；可映射职位只进 crew 卡片。写作品级 NFO 时将其追加到 `<plot>` 末尾作为独立 staff 行，不能把 staff 预先混入 plan 的剧情来源。
- plot 与 TMDB 状态解耦：有真实简介就写，最终为空时 NFO 完全省略 `<plot>`。
- `plot_evidence` 只用于有视频的正片集/电影主片：四个字段固定为 `bangumi_zh`、`tmdb_zh`、`bangumi_ja`、`tmdb_en`，值只能是 `present` 或 `empty`。正片 `plot` 为空时必须四项齐全且全部为 `empty`；缺字段、未检查或仍为 `present` 时，dry-run 先留报告再拒绝，实际落盘在写 NFO 前拒绝。Specials/extras 的空 plot 不要求该字段。
- preflight 拒绝作品级或分集 plot 中的明确 staff 字段行、作品级 plot 与 staff_note 重叠，以及 staff_note 中的可映射职位标签；dry-run 分列正片与 Specials/extras 空 plot，并把缺失正片标签与证据错误写入报告，只有记录四级简介来源均为空才可放行。
- Season 0 按 `special_order` 校验重要度、系列连续性、系列内序号和 S00E 连续性；缺失或乱序在写入前拒绝。
- `artwork` 是图片实体化的唯一契约；不再依赖 `episodes[].thumb` 远程 URL。
- artwork review 候选中的 `width/height` 是 TMDB API 元数据，只用于候选排序、分辨率等级和审查展示。最终 `original` 图片必须写入临时文件后由 Pillow 完整解码，落盘和报告使用解码得到的实际尺寸；API 尺寸与实际尺寸不一致本身不是错误。
- 若 episode/movie 主片记录了非空 `tmdb_still_url`，对应 episode/movie thumb 的 artwork 必须使用 `method: "tmdb"` 和完全相同的 URL；只有没有对应 still 证据时才允许 `method: "frame"`。dry-run 会阻断证据与实际来源不一致的 plan。

artwork 对象：

```json
{
  "scope": "show|season|episode|movie|extra",
  "kind": "poster|fanart|clearlogo|banner|thumb|episode_thumb|...",
  "source_path": "<源侧唯一实体绝对路径>",
  "library_relpath": "<建库时必填的库内相对路径>",
  "method": "tmdb|frame",
  "url": "<method=tmdb 时必填>",
  "fallback_video_path": "<method=frame 时必填>",
  "candidate_id": "<可选：当前原图缓存候选编号>"
}
```

`library_relpath="Specials/poster.jpg"` 时，若该海报来自 Specials 选择器，额外写
`"specials_selection":"season_zero|main_pool_alternative|series_specials_reuse"`。它的
TMDB URL 仍必须出现在同一份 `artwork_review` 候选池中；不得复用主海报，也不得以该
字段引入未审查候选。

同一 source_path 可在同一 plan 内出现多次以投影到多个库侧位置。不同 plan 的作品级图不得共享 source_path。`library_relpath` 不能绝对、不能含 `..`、不能重复，且必须带合法图片扩展名。

TMDB `kind=poster` 的新 plan 还必须带顶层 `artwork_review`：

```json
{
  "schema": "anime-scraper-artwork-review-v1",
  "status": "completed",
  "multimodal_review_enabled": true,
  "selection_method": "agent_multimodal",
  "generated_at": "YYYY-MM-DDTHH:MM:SS+00:00",
  "source_manifest_sha256": "<完整本机 manifest 的 64 位 SHA-256>",
  "preview": {"width": 320, "height": 480, "resize": "contain-and-pad"},
  "candidate_limit": 5,
  "groups": [{
    "group_id": "season-01",
    "candidates": [{"candidate_id": "G01-C01", "url": "https://image.tmdb.org/t/p/original/x.jpg", "language": "ja", "width": 1000, "height": 1500, "resolution_class": "preferred", "visual_issues": []}]
  }],
  "selections": [{
    "group_id": "season-01",
    "candidate_id": "G01-C01",
    "confidence": "high",
    "reason": "无明显水印，构图完整且与其它季度风格协调",
    "flags": [],
    "decision_factors": {"language": "日文标题", "resolution": "preferred", "title": "完整醒目", "visual_quality": "无水印且构图完整"}
  }]
}
```

完整 manifest 只存本机，再由 `--compact-review` 生成 plan 记录。新综合记录必须保留候选语言、原始尺寸、`preferred/acceptable/low` 等级、标题标记和 `visual_issues`；selection 必须记录语言、分辨率、标题、视觉质量四项 `decision_factors`。dry-run 每次读取开关并与记录快照核对；综合策略只验证四项均已审查并拒绝错作品、第三方水印、严重裁切或损坏的入选图，不按票数、语言或分辨率单项替 Agent 排名。关闭时 `status=disabled` 且每组只记录原流程给出的 `deterministic_selection`，不得重新按多模态候选池改选；开启时多候选必须 `completed`。每组最多 5 个候选的限制只属于开启后的多模态预览，不限制关闭前的原有哈希/排序流程。请求 JSON 中的 `cache_candidates` 与 `candidates` 分开：前者是完整原图缓存池，不写入 plan，也不受 5 张识图上限影响；缓存仍独立按票数、语言、竖图和 TMDB `file_path` 过滤。

## 4. dry-run 报告

运行：

```text
python scripts/bootstrap.py --run scripts/scrape.py --plan <plan.json> --dry-run --report-file <本机报告.json>
```

省略 `--report-file` 时，完整报告写入系统临时目录 `anime-scraper/dry-run/`。终端只输出：

- 类型、标题、source-only/link-library 模式，以及硬链接开关来源和最终目标根；
- sorttitle 前缀、排序日期、原文子串状态及外部系列前缀语义审查提醒；
- bgm_id、anidb_aid；
- 正片、Specials/extras、跳过，以及两类空 plot 数量；
- TMDB Season 0 三态统计；
- OP/ED `song_evidence` 校验状态；
- artwork 总数及 tmdb/frame 分布；
- 特殊集日期、图片契约、可选库 preflight 状态；
- 海报选择状态（passed/disabled/not_required）、开关快照及候选、分组数量；
- 当 `artwork_review.original_cache_dir` 存在时，原样输出该原图缓存目录；单张候选的 `cache_path` 留在缓存 `manifest.json`，不在终端展开；
- 源视频存在性、源侧同 stem thumb 完备性与 artwork 方法字段状态；
- warnings 与完整报告路径。

完整报告必须保留：原始 plan、标准化 dataclass、媒体路径、图片、preflight、动作清单和摘要。默认不要使用 `--verbose`，因为它会把完整 JSON 再次打印到终端。

审查规则：

1. 先看摘要；任何 warning、unknown、异常数量都要处理或记账。
2. 定向读取报告里的异常 episode/artwork，不要重新整份打印。
3. 即使所有工程验证通过，也要抽查作品标题、若干正片、全部 Specials/extras、图片选择和跳过理由；preflight 不能代替语义审查。
4. 全部单元通过后才允许阶段三落盘。

两种落盘模式共用同一套源侧 preflight。任何非空 `video_path` 不存在、对应同 stem `-thumb.jpg` 未在 artwork 中声明、`method=tmdb` 缺 URL，或 `method=frame` 缺有效 `fallback_video_path` 时，dry-run 与实际落盘都必须在写 NFO 前拒绝。

## 5. 落盘模式

| 模式 | 触发 | 行为 |
|---|---|---|
| 默认建库 | `library.hardlinks.enabled=true` | 先写源侧；root 非空时投影到该目录，为空时投影到 `<paths.source_root>/_Jellyfin` |
| 配置关闭 | `library.hardlinks.enabled=false` | 只写源侧 NFO/图片，不建库 |
| 本次显式关闭 | 用户明确要求且 CLI 加 `--no-hardlinks` | 只覆盖本次执行，不修改配置 |
| 覆盖库根 | 总开关开启且 `--link-root <DIR>` | 本次使用指定绝对路径；不能启用关闭的总开关 |

`--no-hardlinks` 与 `--link-root` 互斥。总开关开启但 root 与 `paths.source_root` 都为空时，在写 NFO 前拒绝；解析后的目标根和最终模式还必须与 plan 的 `library_projection` 一致。

建库时先写正确源侧 NFO/图，再建立库侧硬链接。源和库 NFO 内容必须一致；图片实体只在源侧。硬链接要求源与库同卷，失败时立即停止，绝不复制回退。外挂同名字幕一并硬链接。

重刮 NFO 是全量替换；图片默认增量。库树模式只在经过审查后处理同名旧作品目录，任一单元失败停止后续单元。

## 6. 审查与验收

阶段三前：

- 所有 plan dry-run 报告存在且验证通过；
- 标题、sorttitle、bgm_id、aid、premiered、rating 有证据；
- Agent 已审查 sorttitle 系列前缀；无法可靠确认系列时使用完整 title；
- 正片数量与集号一致；
- 全部 Specials/extras 有匹配理由或“特典 N”兜底理由；标题非空且不含压制/技术标签，「特典 N」兜底在文件名携带明确标题时会在 dry-run 产生 warning；
- 六类略过项单独列出；
- 所有入库 Season 0 有 airdate；
- Season 0 `special_order` 完整、排序通过且 S00E 连续；
- 每个有视频的 episode/extra 有 artwork；
- 多单元 output_dir 与作品级 source_path 独立。
- plan 的识图开关快照与当前 config 一致；关闭时沿用原有哈希/排序选择，开启时完成所需 Agent 识图。
- plan 的 `library_projection` 与 config/CLI 合成后的最终开关及目标根一致。
- 声优卡位于 crew 前、唯一声优最多 20 位。
- 有 `bgm_id` 的 plan 已完成 `staff_status` 二态审查；`present` 有 crew/staff_note，`empty` 为已检查后的明确空结果。只有 staff_note 或 empty 状态必须带 `staff_audit.persons_checked=true` 和 `mappable_crew_count=0`，并且审计数量必须与 actors 中的 crew 数一致。

阶段四：

- 视频、NFO、字幕、thumb、作品/季图片数量一致；
- 没有空 `<plot />`；空简介完全没有 plot 节点；
- Specials/extras 全部有非空 `<aired>`，年份合理；
- 使用 link root 时抽查视频、NFO、字幕、图片均为硬链接；
- 源/库 NFO 内容相同；
- 分别报告“按规则略过”和“待人工”，不能混为一类。

## 7. Jellyfin 约束

- Jellyfin 先依赖文件名和目录结构识别；杂乱原名应通过可选硬链接树整理。
- `<actor>` 用于声优与 crew 卡片；crew 使用真实 PersonKind，保留头像和 Bangumi 原职位名；作品级简介另在末尾输出 `staff_note` 行。
- 非 Actor 类型会显示类型与 role 两行，这是预期行为。
- `CDs/Scans` 等无视频目录不得残留 NFO。
- tvshow/movie NFO 使用 `<lockdata>true</lockdata>`，防止 Jellyfin 联网覆盖本地完整元数据。

## 8. 进度账本

`scrape.py` 在每次成功运行后把进度写入 plan 同目录的 `<plan名>.progress.json`（schema `anime-scraper-progress-v1`）：dry-run 通过记 `stages.dry_run`（含报告路径），实际落盘记 `stages.source`，完成库投影再记 `stages.library`（含 link_root）。写入是 best-effort，失败只打印警告，不影响运行结果；stdin plan 不记账。

账本由脚本维护，Agent 只读不写。重入时以账本为准：先读 `stages`，用廉价抽查验证（存在性/`<lockdata>`/inode 抽一处即可），再从缺失的下一阶段继续；已记完成的阶段禁止重做，也不得因账本存在而跳过本次 plan 的既有 dry-run 审查义务——账本记录“做过”，不代替“审过”。

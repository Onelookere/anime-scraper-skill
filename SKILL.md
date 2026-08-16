---
name: anime-scraper
description: >-
  用于本地压制组动画（BDRip/BDMV）的 Kodi/Jellyfin/Emby 通用 NFO 刮削、
  SP/OP/ED/NCOP/NCED/特典整理，以及由这些片源建立 Jellyfin 硬链接媒体库。
  正片元数据取自 Bangumi，特殊集结构取自 AniDB，由 Agent 按文件名、集号和时长识别；
  用户只说“电影刮削”“电视剧刮削”或“剧集刮削”而未明确为动画时不触发。
  不用于真人影视、下载或抓取视频/字幕本体，或与动画 NFO 无关的站点/API/音乐元数据。
---

# anime-scraper · 动画刮削（Agent 编排）

> **维护验证分流：**如果变更集只包含人类发布资料（`README.md`、`LICENSE`、`assets/` 下的非执行静态资源），则不运行测试。修改本文件或 `references/`、`scripts/`、`tests/`、`config*.json`、`requirements.txt` 时，按 `tests/smoke_routing.md` 运行受影响的测试套件；交付前再运行完整入口。

> **人类文档边界：**`README.md` 只给人类安装/发布用，刮削、维护、改规则时**禁止**读取；运行时只读本文件与 `references/`、`scripts/`、`tests/`、`config*.json`。

> **稳定规则 ID：**`4.0` / `4.1` / `4.3.x` / `4.4`–`4.6` / `4.7a` / `4.7b` 是跨文档锚点，与 `##` 章节序号无关；细则以对应 `references/*.md` 为 canonical，本文件只保留执行路径摘要。

## 0. 使用方式与全局硬规则

识别引擎是 Agent；Python 脚本只执行确定性扫描、取数、校验、写 NFO、候选预览、图片实体化与硬链接。切单元、认番、特殊集语义匹配、标题和跳过判断由 Agent 完成；只有 config 显式开启时，海报构图与美学复核才交给支持识图的 Agent。

始终遵守：

1. 按“只读盘点 → 元数据与 plan → 落盘 → 只读验证”四阶段执行。用户发起完整刮削，即授权在 plan 与 dry-run 审查通过后按既定范围继续落盘；不得仅因进入落盘阶段再次索要确认，也无需因阶段切换再次征求用户确认。
2. 完整 manifest、plan 与完整 dry-run 报告写本机文件；终端只显示有界摘要，异常时再定向读取完整产物。
3. 正片主用 Bangumi，特殊集结构主用 AniDB，三源编号必须交叉认证；Season 0 图片先查询并交叉认证 TMDB，父条目 `verified`、首播年份一致且存在对应 still 才用远程图，否则回退本地截帧并如实保留 `unknown/not_found` 状态。
4. 没把握时不硬配 AniDB；非六类、匹配失败且 ≥60 秒的有观看价值内容优先使用文件名/字幕中的真实标题入库，完全没有标题依据时才用“特典 N”兜底，不创建空 NFO 或占位 NFO。
5. 只通过本 skill 的受限脚本入口执行程序；不永久允许任意 `python *`、`PowerShell *` 等解释器白名单，权限说明必须与阶段和路径边界一致。需要把多个数据源模块结果留盘时，统一调用 `scripts/metadata_snapshot.py`，禁止为一次刮削临时编写 Python/PowerShell 调用脚本。
6. 原始片源与硬链接树的 NFO 内容必须一致；tvshow/movie NFO 必须有 `<lockdata>true</lockdata>`；作品级简介按既有库格式在剧情后保留 staff 行，人员卡写入 `<actor>`；图片实体只落源侧，库侧只建硬链接。跨卷失败立即停止，绝不复制回退。
7. 每个有 `video_path` 的视频都必须有同 stem 的 `-thumb.jpg`；所有 Season 0 / movie extras 必须有非空 `airdate`。
8. 绝不凭记忆猜歌名或 staff；查不到就留白并记账。
9. 已有正确成果的少量维护默认走增量变更模式；禁止用整单元重刮代替可枚举的局部修改。
10. `library_audit.py` 是可选的全库体检，普通刮削默认跳过，阶段四只做本次作品的定向验证；只有用户明确要求审计时才运行，且绝不能把公共 `link_root` 直接当作单作品的 `--library-root`。

## 1. 渐进式引用路由

先完整读本文件。只在即将进入相应阶段时完整读取一级 reference；任务中途出现新类别时，先补读所需 reference，再继续判断或写入，不要一次性加载无关内容。

| 触发条件 | 必须读取 |
|---|---|
| 首次配置、扫描、ffprobe、切单元、作品类型、API/缓存/限流 | [`references/workflow.md`](references/workflow.md) |
| 认番、标题、sorttitle、评分、简介、人员、按集 staff、OP/ED 歌名 | [`references/metadata-rules.md`](references/metadata-rules.md) |
| 出现 SP/OVA/OAD/NCOP/NCED/Menu/PV/特典/movie extra 或 Season 0 | [`references/special-rules.md`](references/special-rules.md) |
| 选择、下载、截帧、命名或投影任何图片 | [`references/artwork-library.md`](references/artwork-library.md) |
| 组 plan、dry-run、落盘、建库或验收 | [`references/plan-contract.md`](references/plan-contract.md) |
| 已有成果的少量维护（≤10 实体、不改身份/拓扑） | [`references/incremental.md`](references/incremental.md) + 目标对象 reference |
| 用户明确要求音频指纹、全库审计或 staff 修复 | [`references/optional-tools.md`](references/optional-tools.md) |

完整刮削通常会依次读取全部相关 references，但必须按阶段加载。

## Step 0：环境与配置检查

首次完整刮削或需重新联网的增量变更，先运行 `python scripts/bootstrap.py --step-zero-status`。返回“Step 0 已完成（本机）”即进入阶段一；否则在扫描或联网前读取 [`references/workflow.md`](references/workflow.md) 的「Step 0：首次本机预检」，按其步骤检查并写标记。标记只对当前电脑有效，命中后不得重复执行 Step 0。

## 2. 四阶段执行门

**进度账本与重入协议：**`scrape.py` 在 dry-run 通过、源侧落盘、库投影各成功节点自动把进度写入 plan 同目录的 `<plan名>.progress.json`（`anime-scraper-progress-v1`，脚本写入，Agent 不手改）。任何时刻不确定当前进度（上下文压缩后、会话恢复后、跨天续刮）：先读进度账本，再做廉价抽查确认账本可信（如该单元任一 NFO 存在且含 `<lockdata>`），随后只续做账本之后的下一步；禁止重做账本已记完成的阶段，禁止靠对话记忆重建进度。元数据快照文件已存在且请求 ID 一致时直接复用，禁止重新联网取数。

### 阶段一：只读盘点

从 config `paths.source_root` 扫描，用户本次显式给目录时才覆盖：

```text
python scripts/bootstrap.py --run scripts/identify.py <源目录> --manifest <本机路径.json>
```

摘要只作入口：必须查看目录候选、集号/标题异常、特殊件和未知件；切单元前按异常定向读取 manifest。阶段一不得联网或修改片源/库。

### 阶段二：元数据与计划

- 按单元复用 Bangumi、AniDB、TMDB 缓存；OP/ED 完成 4.6 查证并填写 `song_evidence` 后再组 plan。需要按名称确认 Bangumi subject 时，用受限只读入口 `python scripts/bootstrap.py --run scripts/bangumi_search.py <关键词> [--limit N] [--json]`；确认 TMDB 电影 ID 用 `python scripts/bootstrap.py --run scripts/tmdb.py --movie <关键词>`（TV 用 `scripts/tmdb.py <关键词>`）；ID 确认后，用 `python scripts/bootstrap.py --run scripts/metadata_snapshot.py --output <快照.json> --bgm-id <id> --anidb-aid <aid> --tmdb-tv-id <id> --tmdb-season <tv_id:season>` 一次性留存所需元数据，按需追加图片参数。
- TV 单元不要手写整份 plan：快照留盘后先运行 `python scripts/bootstrap.py --run scripts/plan_scaffold.py --snapshot <快照.json> --manifest <manifest.json> --output <plan草稿.json> --bgm-id <id> [--anidb-aid <aid>] [--tmdb-tv-id <id> --tmdb-main-season <N>] [--unit-dir <相对前缀>]`，由脚本填好全部确定性字段；Agent 只写一份小体积 answers sidecar（认番确认、sorttitle、特殊集匹配与 S00E、TMDB 认证、图片决策、空 plot 证据等语义结论），再运行 `plan_scaffold.py --apply-todo --plan <草稿.json> --answers <answers.json> --manifest <manifest.json> --output <最终plan.json>` 合并：S00E 条目、thumb artwork、library_relpath、作品级海报与 artwork_review 嵌入、scaffold 删除全部由脚本完成，Agent 不再 Read+Edit 整份大 plan。scrape.py 在 dry-run 与落盘一律拒绝仍含 `scaffold` 标记的 plan；骨架与合并只是省力工具，不改变任何审查义务。细则见 `plan-contract.md` §1。
- 图片遵循 4.7a：识图请求 JSON 由 `scripts/artwork_review.py --build-request --snapshot <快照.json> --output <request.json> --series-name <系列名> [--season-group TV_ID:SEASON:GROUP_ID[=显示名]] [--movie-group MOVIE_ID:GROUP_ID[=显示名]] [--specials-group TV_ID:MAIN_SEASON:GROUP_ID[=显示名]]` 从快照确定性生成，禁止手工誊写候选池；快照需先带 `--tmdb-season-images`/`--tmdb-tv-images`/`--tmdb-movie-images` 图片池。主海报审查完成后，必须单独完成 Specials 三段选择并写入 plan，缺少该决策不得进入 dry-run。`config.artwork.multimodal_review=true` 时才由 Agent 识图，Agent 不能可靠看图时立即停止，不得猜选或静默回退。人工原图缓存由 `config.artwork.artwork_cache` 独立控制，缺失时默认关闭；开启后才把主/季/Specials 的合格竖版 TMDB poster 原图保存到本次作品缓存目录，只保留 `vote_count > 0` 且语言为中/日/英的图。关闭缓存不影响自动选图、识图候选池或源侧图片落盘。
- plan 必填 `plan_schema: anime-scraper-plan`，并快照最终 `library_projection`，随后执行：

```text
python scripts/bootstrap.py --run scripts/scrape.py --plan <plan.json> --dry-run --report-file <本机报告.json>
```

审查 warnings、ID、正片/SP/跳过/空 plot 数、TMDB 三态、图片分布和全部验证状态。sorttitle 的系列前缀由 Agent 语义判断，脚本只校验格式、日期和最终 title 后缀；前缀不在最终 title 中时必须复核报告中的语义审查提醒，无法可靠判断时使用完整 title。工程 preflight 不能替代标题、匹配和图片的语义审查。

### 阶段三：落盘

全部单元 plan 通过 dry-run 与审查后才进入。先写源视频旁 NFO、在源目录实体化图片，再按 4.7b 投影硬链接库；逐单元执行，任一单元失败立即停止后续单元。既有作品的增量维护可先用 `scrape.py --source-only` 按原 plan/config 校验并落盘源侧 NFO/图片，再由现有库侧 change set 完成结构维护；该选项不修改 plan 中的硬链接配置，也不表示关闭建库。只有范围、目标、写入类型或外部权限超出已审查内容时才暂停请求用户决定。

图片实体化统一走 `scripts/images.py` 的有限并发入口；并发、重试、`.part` 续传与原子替换细则见 `artwork-library.md` §9。TMDB API 的 width/height 只是候选排序提示，最终尺寸以 Pillow 解码结果为准；不得把并发扩成无限，或把图片下载塞进 AniDB/Bangumi 的 API `RateLimiter`。

### 阶段四：只读验证

检查视频/NFO/字幕/thumb/作品图数量、空 plot、Specials/extras 日期、源/库 NFO 一致性；建库时抽查视频、字幕、NFO、图片均为硬链接。验证不得顺手补写；普通刮削跳过 `library_audit.py`。

### 增量变更模式

已有 manifest/plan/库条目且目标可精确枚举的少量维护（新增或修改媒体条目 ≤10、写入类型可枚举、有物理实体预算、不改作品身份/单元切分/库目录名/库根拓扑、无需重新识别未盘点媒体或追溯整包）缩小为“只读差异 → change set 与预算 → 白名单落盘 → 分级验证”。单个媒体的源侧 NFO/thumb 加库侧硬链接超过 10 个物理实体时，不得仅据此升级全量。

增量默认不重生成 plan、不跑整单元 dry-run，已有 plan 只同步目标字段并解析校验；范围扩展时先盘点新增部分，并从库侧确认的最大 `S00E` 后连续编排。超预算、新写入类型、白名单外路径或无法维持全局约束时立即停止并重新估算，不得自动升级全量重刮；细则见 `references/incremental.md`。

#### 已有缓存海报极速替换

用户只要求把已有海报换成同一 artwork cache 中的候选时，直接按 `references/incremental.md` §2 的快捷入口执行；候选、旧图、目标或 inode 不符合预期时停止，不升级为整单元重刮。

## 3. 盘点与切单元

### 3.1 ffprobe（规则 ID 4.0）

默认不 probe 正片：先无时长扫描，再只 probe 特殊集与异常正片。仅集数不一致、集号不可信、正片/SP 边界不清、多版本需消歧、用户要求准确 runtime 或校验完整性时，才补 probe 涉事正片；禁止整包无差别全量 probe。细则：`workflow.md` §4。

### 3.2 Agent 切单元（规则 ID 4.1）

`scan_tree()` 只提供目录树、`by_dir`、相对路径和 hint；顶层目录不是单元铁律，flat 包靠集号重置与标题变化切分，hint 可被 Agent 推翻。

库条目必须先满足在线独立性：

| Bangumi 独立 subject | AniDB 独立 aid | 库处理 |
|---|---|---|
| 有 | 有 | **才可**单开 TV/Movie 库条目 |
| 有 | 无(仅挂父作 S/C/O 或查无 aid) | **直接当 SP**/extras，禁止单开 |
| 无 | 有 | **直接当 SP**/extras，禁止单开 |
| 无 | 无 | **直接当 SP**/extras；文件名再像新作也禁止单开 |

B+A 双独立只是允许单开的必要条件，不是充分条件；明确属于附属内容时仍归母作品 Specials/extras。缺少所依附的正片时，SP 也不得单开；TMDB 独立条目不能放宽门槛。细则：`workflow.md` §5–§6。

## 4. 数据、匹配与命名（规则 ID 摘要）

细则以 `metadata-rules.md` / `special-rules.md` 为 canonical；下列 ID 仅作执行路径锚点。

### 4.2 逐单元元数据

认番、标题、sorttitle、评分、简介、人员、制作公司与歌名按 `metadata-rules.md`；一个 Bangumi 条目可能对应多个 AniDB aid，必须查齐相关 episode 表并确认本地内容一致。

#### 4.3.1 正片正名与匹配

按集号对齐 Bangumi；只有异常、重复或边界不清的文件才逐个判断或补 probe。详见 `metadata-rules.md` §5。

#### 4.3.2 特殊集匹配与 Season 0 排序

特殊集由 Agent 按 `special-rules.md` §3 语义匹配，再由脚本校验字段完整性与 S00E 顺序；AniDB 没有对应条目时也不跳过 NCOP/NCED。

#### 4.3.3 分集简介与 TMDB 状态

`tmdb_match_status` 只写真实的 `matched`/`not_found`/`unknown`；未成功检查不得伪造 `not_found`。正片 plot 按四级回退，仍为空时附完整 `plot_evidence` 并省略 `<plot>`。详见 `metadata-rules.md` §5 与 `special-rules.md` §4。

#### 4.3.4 特殊集 airdate

所有入库 Season 0 / movie extras 必须有非空 `airdate`，由 `validate_special_airdates` 保护。

#### 4.3.5 六类无观看价值特典

Menu、CM/SPOT、Logo/版权警告/标版、次回预告合集、PV/特報/Teaser/Trailer、音频试音/声道测试不进 plan；NCOP/NCED、OVA、总集篇、序章、访谈、花絮、Event/Live、MV、Drama 不属于这组六类。详见 `special-rules.md` §3。

#### 4.3.6 时长小于 60 秒的最后兜底

只有所有前置匹配方法全部失败后，才可略过 probe 时长 <60 秒的特殊件；同系列已有兄弟条目时不得仅凭时长略过。

#### 4.3.7 兜底命名（真实标题优先）

非六类、匹配失败且 ≥60 秒的有观看价值内容：文件名或字幕携带明确内容标题（活动名、公演名、特典节目名等）时以该标题入库（可按 `special-rules.md` §3 翻译成自然中文，无法可靠翻译时保留原文）；完全没有标题依据时才以“特典 N”连续编号兜底；无可靠简介时省略 plot。

### 4.4 人员

按 `metadata-rules.md` §7 写入 actors、crew、staff_status、staff_audit 与 staff_note；TV/Movie 每条最多 20 位唯一声优，Actor 位于 crew 前。

### 4.5 按集 staff

带集数标注的 staff 用 `parse_episode_credits` 与 `episode_writers` 按集拆分，不能把括号内逗号当作人员分隔符。

### 4.6 OP/ED

先完成一次有界查证；仍不明才用裸 OP/ED。入库 credit 必填 `song_evidence`，绝不凭记忆猜歌名。

## 5. 图片、plan 与落盘

### 4.7a 图片

任何图片操作前读取 `artwork-library.md`。主海报候选池（含单正式季时的季池+系列池合并）、TMDB 排序、pHash/aHash/wHash 去重、分辨率检查与 Specials 回退按其 §6–§8 执行，且始终先于多模态审查；多模态仅在 config 开启且 Agent 能可靠识图时使用，否则不得猜选。每个有视频的条目必须有同 stem thumb，图片只在源侧实体化。
主海报选定后必须独立完成 Specials 三段选择：`season_zero` → `main_pool_alternative` → `series_specials_reuse` → `none`；选择结果与候选池必须进入同一次 `artwork_review`，缺失决策不得进入 dry-run。详见 `artwork-library.md` §6–§8。
单元独占片源根时必须直接用该根作为 `output_dir`；不要只为这些元数据再建立一层 `meta` / `metadata` 子目录，且 `output_dir` 末级目录恰为 `meta` 或 `metadata` 时必须拒绝 plan。多个单元共用同一片源根时，各单元的元数据目录必须统一收纳在同一 `meta/` 父目录下（如 `meta/s1/`、`meta/movie1/`），不得把 `s1-meta/`、`movie-meta/` 一类目录散落在片源根。
多候选 contact sheet 单张 `320×480`；多模态与人工原图缓存均由 config 独立控制。缓存候选、语言/票数过滤和失败语义详见 `artwork-library.md` §7。

### 4.7b plan、dry-run 与落盘

任何 plan/落盘操作前读取 `plan-contract.md`；完整 plan 与 dry-run 报告留盘，终端默认只输出摘要。已有成果的少量维护改读 `references/incremental.md`，不把增量章节带入首次完整刮削。

- `library.hardlinks.enabled=true`：建立硬链接树；`root` 非空时必须是绝对路径，为空时使用 `<paths.source_root>/_Jellyfin`。
- `library.hardlinks.enabled=false`：只写源侧 NFO/图片。
- 只有用户本次明确要求不建硬链接时才加 `--no-hardlinks`；`--link-root DIR` 只覆盖本次目标根，不能与其同用，也不启用已关闭的总开关。

plan 的 `library_projection.hardlinks_enabled/link_root` 必须与 config 和 CLI 合成状态一致；源与库必须同卷，先源侧落盘再投影硬链接。重刮 NFO 全量替换，图片默认增量，`--refresh-artwork` 才强制重下。

## 6. 完成检查

落盘后逐项核对 `plan-contract.md` §6；六类略过项和待人工项只记录在内部验收产物中，不进入最终回复。若 `artwork_review.original_cache_dir` 存在，最终回复必须逐字报告该缓存目录；若不存在则不要臆造路径。验收阶段不得补写。

### 最终显性输出

最终回复只保留以下结果：

- 作品/单元名称。
- 规模：正片、Specials/extras；图片总数及 `tmdb/frame` 分布。
- 提醒：`warnings`、按规则略过清单、待人工清单；不得把两类清单合并。
- 缓存：存在 `original_cache_dir` 时逐字输出目录；缓存失败或 `cache_candidates` 缺失时明确提示。
- 增量替换：更新/删除/恢复数量；有缓存候选时输出 `candidate_id`、新 URL 和 `cache_path`。

不要求固定标题、表格、分隔线或其他排版；保持简短，避免重复字段。

完整 JSON 不直接倾倒到终端；仅输出上述有界摘要，完整内容以本机报告为准。

## 7. 修改入口

- 扫描/hint/摘要/manifest：`scripts/identify.py`、`references/workflow.md`
- 数据源快照：`scripts/metadata_snapshot.py`（显式 ID、复用模块缓存/限流、原子写本机 JSON）
- plan 骨架与合并：`scripts/plan_scaffold.py`（快照+manifest → 确定性字段草稿；`--apply-todo` 把 answers sidecar 合并成最终 plan 并删除 scaffold 块；scrape.py 拒绝未去除草稿标记的 plan）
- 识图请求组装：`scripts/artwork_review.py --build-request`（快照 → 候选池请求 JSON，禁止手工誊写）
- 标题/评分/人员/简介/OP-ED：`references/metadata-rules.md`（规则 ID 4.4–4.6）
- 图片与库结构：`references/artwork-library.md`、`scripts/artwork_review.py`、`scripts/tmdb.py`、`scripts/images.py`、`scripts/link_library.py`（规则 ID 4.7a）
- 增量图片替换：`scripts/update_artwork.py --cached-replace`（细则 `references/incremental.md` §2）。
- plan/dry-run/NFO：`references/plan-contract.md`、`scripts/scrape.py`、`scripts/match.py`、`scripts/nfo.py`（规则 ID 4.7b）
- 触发描述与边界：`tests/trigger_cases.json`；修改 description 时同步维护正例与近邻负例。
- 测试路由：`tests/smoke_routing.md`；完整主流程验证运行 `python scripts/bootstrap.py --run tests/smoke_test.py`，按修改范围选 `tests/smoke_core.py` / `tests/smoke_integration.py` / `tests/smoke_contract.py`；音频指纹/全库审计等低频工具单独运行 `tests/smoke_optional.py`。`smoke_support.py` 仅为公共辅助模块，不单独运行。

# 图片实体、选择与硬链接库规则

## 目录

- [1. 唯一实体原则](#1-唯一实体原则)
- [2. 分集 thumb](#2-分集-thumb)
- [3. 源侧落点与库侧投影](#3-源侧落点与库侧投影)
- [4. 文件名与 preflight 护栏](#4-文件名与-preflight-护栏)
- [5. 多单元 output_dir](#5-多单元-output_dir)
- [6. 主图与 Specials 海报](#6-主图与-specials-海报)
- [7. Agent 识图审查](#7-agent-识图审查)
- [8. TMDB 多图排序](#8-tmdb-多图排序)
- [9. 刷新与验证](#9-刷新与验证)

## 1. 唯一实体原则

图片物理实体只落片源目录；Jellyfin 库树中只建立硬链接。禁止把 poster、fanart、thumb 直接下载到硬链接库根。

有 `plan.artwork` 时，无论最终是否建立硬链接，阶段三都必须先在源侧 materialize。建库只是把已有源侧实体投影到库树。同一 `source_path` 可在同一 plan 内映射到多个 `library_relpath`。

## 2. 分集 thumb

每个有 `video_path` 的正片、Special 或 movie extra，源视频旁都必须存在 `视频stem-thumb.jpg`。没有本地 thumb 时 Jellyfin 容易按 S00E 编号联网拉错图。

图片来源只有两类：

1. TMDB still：只用于已经通过标题、日期、时长等交叉认证的正片或特殊集。
2. ffmpeg 截帧：用于 TMDB 没有对应 still 的 BD 独占内容，例如 NCOP/NCED、Storyboard、访谈等。通常从 5 秒处截取。

特殊集流程：先读取 TMDB Season 0 列表，逐项与本地内容按标题、日期、时长认证；只有 `tmdb_identity.status=verified` 且首播年份一致、确有对应 still 的条目使用 `method=tmdb`，其余使用 `method=frame`。三态语义、错误处理与"先查 TMDB 再回退"的完整规则以 `special-rules.md` §4 为 canonical。

## 3. 源侧落点与库侧投影

| 类型 | 源侧唯一实体 | 库侧相对位置示例 |
|---|---|---|
| 分集/extras thumb | 视频旁 `stem-thumb.jpg` | `Season 01/<干净stem>-thumb.jpg`、`Specials/...`、`extras/...` |
| 作品/电影海报 | `output_dir/poster.jpg` | `poster.jpg`、可同时投影到 `Season 01/poster.jpg` |
| 背景 | `output_dir/fanart.jpg` | `fanart.jpg` |
| Banner/方图 | `output_dir/banner.jpg` / `thumb.jpg` | 同名 |
| Logo | `output_dir/clearlogo.png` 或 `.svg` | 同名 |
| Specials 独立海报 | `output_dir/specials-poster.jpg` | `Specials/poster.jpg` |
| 其它季海报 | `output_dir/seasonNN-poster.jpg` | `Season NN/poster.jpg` |

Specials 不得复用主 poster：找不到独立候选时完全不生成 `Specials/poster.jpg`，不能以同图填充。

## 4. 文件名与 preflight 护栏

TV 库侧视频/NFO stem 由 `link_library._episode_base` 生成：

```text
{show.title} S{season:02d}E{episode:02d}
```

TV 分集 stem 不含年份，因此 thumb 必须是：

```text
Season 01/死亡笔记 S01E01-thumb.jpg
Specials/死亡笔记 S00E01-thumb.jpg
```

禁止写成 `死亡笔记 (2006) S01E01-thumb.jpg`。多出的年份会使图片 stem 与视频不一致，Jellyfin 随后可能回退到作品海报。

电影主片 stem 等于带年份的电影文件夹名，例如 `GRIDMAN UNIVERSE (2023)-thumb.jpg`；movie extras 仍用无年份的 episode base。

工程护栏：

- source-only 与 link-library 共用源侧 preflight：所有非空视频路径必须存在，且 plan 必须声明视频旁同 stem `-thumb.jpg`；
- `method=tmdb` 必须提供 URL，`method=frame` 必须提供存在的 `fallback_video_path`，dry-run 不得把缺字段的 artwork 标为通过；
- episode/movie 记录存在 `tmdb_still_url` 时，dry-run 必须核对对应 thumb 使用完全相同的 TMDB URL；有 still 证据却使用截帧属于 plan 错误，必须在落盘前停止；
- `validate_thumb_library_relpaths` 检查 thumb stem；
- `validate_thumb_completeness` 要求每个有视频的 episode/extra 都有 artwork；
- `library_relpath` 必须含合法图片扩展名 `.jpg/.jpeg/.png/.webp/.svg`；
- `library_relpath` 不可绝对、不可包含 `..`、不可重复；
- `clearlogo.png` 不能装 SVG 字节。只有 SVG 时要么栅格化为 PNG，要么路径与扩展名都改为 `.svg` 并确认客户端支持。

## 5. 多单元 output_dir

单个 TV/Movie 单元独占一个片源根目录时，必须直接把该片源根作为 `output_dir`。把 `tvshow.nfo` / `movie.nfo`、作品级图片和媒体放在同一根目录；不要只为这些元数据再建立一层 `meta` / `metadata` 子目录，且 `output_dir` 末级目录恰为 `meta` 或 `metadata` 必须拒绝。例如：

```text
作品片源根/
├── tvshow.nfo
├── poster.jpg
├── fanart.jpg
├── clearlogo.png
├── 正片视频...
└── SPs/
```

只有多个 TV/Movie 单元确实共用同一片源根目录、会使固定文件名冲突时，才为各单元使用独立 `output_dir`；各单元目录必须统一放在同一 `meta/` 父目录下，目录名必须能标识单元（末级仍不得恰为 `meta`/`metadata`），不得散落成 `<根>/s1-meta/` 一类目录，也不得使用通用 `meta` / `metadata`，例如：

```text
共享片源根/
└── meta/
    ├── s1/
    ├── s2/
    └── movie1/
```

`tvshow.nfo`、`movie.nfo`、`poster.jpg`、`fanart.jpg`、`clearlogo.*` 都使用固定文件名；多个单元共享同一 `output_dir` 会让后一个单元覆盖前一个。

分集 NFO/thumb 仍写在视频旁，不受 output_dir 影响。不同 TV/Movie 单元不得把作品级 artwork 指向同一个 `source_path`；同一实体多处投影只允许发生在同一个 plan 内。

## 6. 主图与 Specials 海报

主图：

- TV 使用对应 TMDB 季自己的 season poster；
- 先统计正式季数量（Season 0 不计入）：全作只有一个正式季时，必须把该季 poster 池与 TMDB 系列整体 poster 池合并为同一主海报候选池，不能只使用季级海报；合并池统一执行本节及 §7/§8 的排序、去重、分辨率检查和多模态审查；
- 多季度作品的季度主 poster 之间也必须按同一组 pHash/aHash/wHash 阈值去重；
- movie 使用该 movie 的 poster 池；
- fanart 使用 backdrop；
- clearlogo 中文优先，有位图时排除 SVG；
- 分集使用认证后的 still，否则截帧。

Specials 海报必须调用 `tmdb.select_specials_poster`，不能按 API 数组下标手取“第二张”。受限入口：`python scripts/bootstrap.py --run scripts/tmdb.py --specials <tv_id> [--main-season N] [--json <FILE>]`——它按正式季数量自动取主海报候选池（单季合并季池+系列池）与 Season 0 池后执行三段回退。输入为当前季主 poster、完整主 poster 候选池、`get_optional_season_images(tv_id, 0)` 的 Season 0 poster，以及本系列全部季度主 poster 组成的 `reference_main_posters`。已有其它季度 Specials 时，同时作为 `fallback_specials_poster` 传入。只有明确 404 可视为 Season 0 无图；认证、限流、超时和 5xx 必须报错。

三段选择：

1. `season_zero`：Season 0 中选择排序最高、且相对本系列每张季度主 poster 均满足 pHash > 16、aHash > 16、wHash > 16 的竖图。
2. `main_pool_alternative`：Season 0 无合格独立图时，从完整主池选择最高排名且 `file_path` 不同，并相对全部季度主 poster 同时满足上述三项距离阈值的备用图。
3. `series_specials_reuse`：没有合格新图时，复用其它季度已认证的 Specials poster；不同季度 Specials 允许相同。
4. `none`：没有独立候选时，不生成 `Specials/poster.jpg`；不得拿主图、fanart、logo 或无关条目凑数。

`Specials/poster.jpg` 必须与作品/季度主海报使用不同 source、URL 和视觉内容。出现相同 source、URL 或内容即为审查失败；没有独立候选时缺省该文件才是正确结果。若使用 `season_zero`、`main_pool_alternative` 或 `series_specials_reuse`，对应 artwork 项必须写 `specials_selection`，其 URL 仍必须来自同一次 `artwork_review` 的候选池；不能借此引入未审查海报。`season_zero` 候选来自 Season 0 池，天然不在主海报候选池内：必须在 artwork_review 请求 JSON 中为 Specials 另建一个 group（candidates/cache_candidates 放 Season 0 池，deterministic_selection 填三段选择结果；关闭多模态时该组与主海报组一样只保留确定性候选），否则 `Specials/poster.jpg` 的 URL 无法命中候选池，dry-run 直接拒绝。多季度作品的每季度 Specials 同样各建一个 group 并入同一次请求。

每张图只下载、解码一次，再同时计算三个 64 位 hash；同一次选择中的主图 hash 会复用。三项必须全部严格大于阈值才认定为视觉不同，以拦截同底图只更换 logo 或少量文字的变体。任一 hash 下载、解码或计算失败时按未知跳过，不能把失败当作“视觉不同”。缺少 imagehash 时退化为仅比较 `file_path`。

同一系列各季度的 poster 均参与跨单元去重：季度主 poster 之间不能重复，Specials poster 也必须与本系列所有季度主 poster 去重。只有不同季度的 Specials poster 允许彼此相同，这是唯一例外。若复用 SP，仍要在各单元独立 output_dir 中各实体化一份，不能跨 plan 共用 `source_path`。

## 7. Agent 识图审查

多模态开关只控制本节的预览和 Agent 审美复核，不控制 §6/§8：TMDB 排序、pHash/aHash/wHash 去重、跨季度主图比较及 Specials 三段回退始终先执行。`artwork.multimodal_review` 缺失时默认 `false`；关闭时把原流程结果作为每组 `deterministic_selection` 传入脚本，脚本不改选并生成 `status=disabled`、`reason=config_disabled`、`selection_method=deterministic_existing_pipeline`。开启时，多候选组才进入识图；全部组单候选时仍不生成预览。fanart、logo、分集 still 与 ffmpeg 截帧不进入本流程。

人工原图缓存额外只保留 `vote_count > 0` 且语言为 `zh`/`zh-CN`/`zh-TW`、`ja` 或 `en` 的候选；这条规则只作用于缓存，不改变自动选图和识图候选池。

原图缓存是上述流程完成后的人工备用，不是刮削门槛，由 `artwork.artwork_cache` 独立控制且缺失时默认关闭。开启时才缓存主海报、季海报和 Specials 海报候选池中的合格竖图；当前确定性/Agent 选图继续直接落盘，不等待人工复核。关闭时不创建原图缓存目录、不下载人工备用原图。缓存根为 `cache/artwork-originals/`，每次作品/识图请求建立 `<系列名>-<UTC时间戳>` 目录，目录名只取一个系列名，不把多个季度/单元的组标签拼接进去；图片全部平铺，固定包含 `manifest.json`。请求必须提供顶层 `series_name` 和组级 `work_name`。缓存阶段只按 `file_path` 去重，保留 pHash/aHash/wHash 仅相似但路径不同的候选。文件名使用紧凑的作品名和候选编号，例如 `咒术回战 - s1 - G01-C07.jpg`；季/单元统一缩写为 `s1`、`s2`，剧场版统一为 `movie`，并去掉“主海报”等功能性后缀，当前已成功选中的文件加 ` - CURRENT`。URL、尺寸和缓存路径只放 manifest。

缓存目录在后续运行开始时扫描；以各目录 `manifest.json.created_at` 为准，超过 7 天才删除，重新运行不会续期。manifest 损坏、缺少创建时间或带 `.active` 的目录保留。原图下载是 best-effort，失败写入 `partial/failed` 和错误记录，不暂停或回滚刮削。开启缓存时最终摘要只显示一个 `original_cache_dir`，且必须从最终 plan/report 读取后原样输出；plan 的 `artwork_review` 仅在缓存目录成功建立时同步保存该目录。每张候选的具体 `cache_path` 只从缓存目录的 `manifest.json` 获取，不在摘要中展开。

确定性准备步骤：

1. 识图请求 JSON 由受限入口从元数据快照直接组装，**禁止手工誊写候选池**：

   ```text
   python scripts/bootstrap.py --run scripts/artwork_review.py --build-request --snapshot <快照.json> --output <request.json> --series-name <系列名> [--season-group TV_ID:SEASON:GROUP_ID[=显示名]] [--movie-group MOVIE_ID:GROUP_ID[=显示名]] [--specials-group TV_ID:MAIN_SEASON:GROUP_ID[=显示名]]
   ```

   候选席位（排序头部、最高分辨率中文代表、最高分辨率日文代表、全池最高分辨率代表；§8 排序、竖图过滤与 `file_path` 去重）、`deterministic_selection`（排序头部；Specials 组为三段选择结果）与不截断的 `cache_candidates` 完整池全部由脚本确定；供识图的 `candidates` 每组最多 **5** 张。单正式季自动合并季池与系列池，Specials 三段选择为 `none` 时自动跳过该组。排序只用于建立有代表性的候选集合，不是最终选图；同图只占一席，不拿横图、重复图或其它来源凑数。缓存候选再由脚本独立执行 `vote_count > 0`、语言、竖图和 `file_path` 去重过滤。快照需先带 `--tmdb-tv-id` 详情与 `--tmdb-season-images`/`--tmdb-tv-images`/`--tmdb-movie-images` 图片池，缺失时报错并提示需补的参数。请求 JSON 字段（顶层 `series_name`；每组 `group_id`/`label`/`work_name`/`deterministic_selection`/`candidates`≤5/`cache_candidates` 完整池）由脚本生成；只有脚本无法表达的组（如跨单元复用候选）才允许手写该文件。
2. 再运行：

   ```text
   python scripts/bootstrap.py --run scripts/artwork_review.py --input <request.json> --output-dir <本机临时目录>
   ```

3. 脚本每次先读取开关；关闭时要求每组存在 `deterministic_selection`，只记录原流程结果，不下载多模态预览。注意此前的感知哈希可能已经下载小图，这是原流程成本，不受开关影响。开启时请求 TMDB `w500` 预览，并按规范化预览 URL 的 SHA-256 持久缓存原始字节；命中项先解码校验，损坏时失效重下，重审只下载新增项。随后按 EXIF 纠正方向，再用 `ImageOps.pad` + LANCZOS 等比缩放到 **320×480**，比例不符时居中补深灰边。禁止拉伸或裁切。
4. contact sheet 固定五列，每行一个候选组、每张视觉内容 320×480；下方标签必须显示候选编号、语言、票数、原始 `宽×高` 与分辨率等级。最多三组一张 sheet，超过时分多张。Agent 必须在同一轮以原始 detail 查看全部 sheet，不能只读文件名、票数或 manifest 猜图。

同一系列即使因 Bangumi 独立条目拆成多个 plan，也应把各季度/续作候选放进同一次 review 请求并在同一轮查看；随后把这份 completed manifest 生成的同一紧凑记录嵌入相关 plan，各 plan 仍只使用自身所选 URL 和独立 source_path。

开关开启后，Agent 必须先为每张候选标记 `visible_text_role`：`primary_title`（可读的本作品主标题）、`other_text`（副标题、发行/宣传文案、角色名等）或 `none`；若为 `primary_title`，再标记 `primary_title_prominence`：`1`（小或斜置）、`2`（清晰可读）或 `3`（完整醒目），其它两类固定为 `0`。同时填写 `visual_issues` 列表；无问题用空列表，错作品、第三方水印、严重裁切或损坏分别记 `wrong_work`、`third_party_watermark`、`severe_crop`、`damaged`，其它宣传覆盖或构图问题可据实记录。第三方水印、站点角标、压制组标识和版权角标一律不算标题。

原始分辨率按固定等级记录：`preferred` 为至少 1000×1500，`acceptable` 为至少 800×1200，低于此值为 `low`。先排除错作品、第三方水印、严重裁切和损坏图；其余候选由 Agent 综合判断中文/日文的本地化价值、原始清晰度、主标题是否完整醒目、构图、宣传文字干扰和跨季度一致性。中文、日文、高分辨率、完整标题都是高优先级正向证据，但任何单项都不能直接决定胜负；票数只作弱参考。selection 必须填写 `decision_factors.language/resolution/title/visual_quality` 四项和总结 reason，dry-run 只验证审查覆盖与致命缺陷，不用固定权重替 Agent 排名。仅剩低清正确候选时如实采用，不换无关图片。当前 Agent 不能可靠查看图片时必须停止，禁止仅根据 manifest 文本伪造 `completed`。

识图输出必须把 manifest `status` 改为 `completed`，每张候选必须保留 `language`、`width`、`height`、`resolution_class` 并填写上述文字与视觉问题标记；每组恰有一个 `selection`，记录 `group_id`、可见 `candidate_id`、`confidence=high|medium|low`、非空 `reason`、`flags` 与四项 `decision_factors`。这里的 `width/height` 是候选元数据，用于排序和分辨率等级，不是最终落盘尺寸。所有 sheet 下载/解码完整后才能选择；任一候选失败时停止本次审查，不能把未显示候选当作审美淘汰。完成后运行：

```text
python scripts/bootstrap.py --run scripts/artwork_review.py --compact-review <artwork-review.json> --plan-review-out <artwork-review-plan.json>
```

完整 manifest 留在本机作审查证据；plan 的紧凑记录必须保留候选原始尺寸与等级。`scrape.py` 会再读取当前 config：开关变化、关闭时未沿用 `deterministic_selection`、开启时没有完成识图或无说明降级，任一情况都在写入前拒绝。最终仍下载 TMDB `original` 并解码核对实际尺寸，绝不把预览当成最终海报。

## 8. TMDB 多图排序

票差很大时以 `vote_count` 为主。票差很小时不要让 1 票压过明显更清晰的图。以下情况视为票差小：

- 任一候选 `vote_count <= 2` 且票差 <= 2；
- 冷门作品整个候选池都只有 0–2 票。

票差小时按以下顺序：

1. poster/logo 语言：`zh`/`zh-CN`/`zh-TW` > `ja` > `en` > 无语言/其他；
2. 分辨率面积更大；同面积 width 更大；
3. 再比较 `vote_average`、`vote_count`。

下载统一使用 TMDB `original` 或等价最大尺寸。候选只有低清图时如实采用并记账，不要换成无关作品图片。

## 9. 刷新与验证

默认已有有效源图时跳过下载/截帧。需要重下时使用 `--refresh-artwork` 或 plan `"refresh_artwork": true`。NFO 仍按全量替换，图片默认增量。

少量既有海报更新、缓存候选、白名单和中断恢复均按 `references/incremental.md` §2 执行；图片替换不属于首次完整刮削的默认路径。

图片落盘使用有限并发和可恢复下载：`artwork.tmdb_workers` 控制 TMDB/CDN 并发，默认 6、上限 16；`artwork.ffmpeg_workers` 控制 ffmpeg 截帧并发，默认 3、上限 8。同一 `source_path` 先去重，避免 poster/Season poster 或重复 thumb 同时写同一个实体。下载默认最多 4 次，单次 I/O 超时 45 秒，退避为 0.75/1.5/3 秒并带轻微抖动，429/5xx/超时/断流遵守 `Retry-After` 并重试。每个源图都先写同目录固定 URL 对应的 `.part` 文件，服务端支持 Range 时续传；Range 被忽略、响应范围不一致或 HTTP 416 时丢弃残片并安全重启。Pillow 完整解码成功后才原子替换源图，失败重试耗尽时保留可恢复残片；库侧仍只建硬链接。

这组并发只针对图片 CDN 和本地 ffmpeg，不改变 AniDB/Bangumi 的 API 限流；不要用提高图片并发的方式规避 API `RateLimiter`。默认值已经偏积极，继续上调会增加 CDN 429、出口带宽争用、NAS I/O 队列和 ffmpeg CPU/内存峰值，必须在失败率和本机资源可接受时才调整。

阶段四图片专项检查（通用验收清单见 `plan-contract.md` §6）：

- 不存在孤立的带错误年份分集 thumb；
- 根 poster、季 poster、Specials poster、fanart/logo 数量与 plan 一致；
- link-library 模式下库侧图片与源侧图片是同一硬链接实体，不是复制。

# 工作流、盘点与作品分类

## 目录

- [1. 四阶段边界](#1-四阶段边界)
- [2. 首次配置与 Windows 约束](#2-首次配置与-windows-约束)
- [3. 扫描 manifest 与摘要](#3-扫描-manifest-与摘要)
- [4. ffprobe 按需策略](#4-ffprobe-按需策略)
- [5. Agent 切单元](#5-agent-切单元)
- [6. 作品类型与库分类](#6-作品类型与库分类)
- [7. 数据源与脚本入口](#7-数据源与脚本入口)
- [8. 限流、缓存与恢复](#8-限流缓存与恢复)

## 1. 四阶段边界

四阶段的执行门与审查要求以主 `SKILL.md` §2 为 canonical；本节只补充各阶段的读写边界，不要把只读盘点、联网、删除和写入塞进一个黑盒命令。

- **阶段一（只读盘点）：**不得联网，不得修改片源或媒体库。除 manifest 外同时清点视频、字幕、旧 NFO、现有图片及目标库结构；用户本次显式指定源目录时才覆盖 config 的 `paths.source_root`。
- **阶段二（元数据与计划）：**允许写入本地 API 缓存、plan 与 dry-run 报告；不得删除旧 NFO、重建库树或修改源媒体。审查 dry-run 摘要中的 warnings、TMDB unknown、空 plot、跳过项和验证结果，异常时定向读取报告对应对象，禁止只看“通过”二字。
- **阶段三（落盘）：**按既定全量替换规则清理旧 NFO；逐单元执行，任一单元失败停止后续单元并保留已完成记录，禁止无提示重试破坏性操作。
- **阶段四（只读验证）：**报告只写本机，不改媒体；普通刮削只做本次作品定向验证。用户明确要求全库体检时再读取 `references/optional-tools.md` 并调用 `library_audit.py`。最后分别输出“按规则略过”和“待人工”清单。

## 2. 首次配置与 Windows 约束

### Step 0：首次本机预检

仅当主 `SKILL.md` 的 `--step-zero-status` 未返回“Step 0 已完成（本机）”时执行。完成后该电脑不再重复本步骤。

1. 运行 `python scripts/bootstrap.py --check`，确认 Python 3.10+、skill 本地虚拟环境和 Python 依赖可用；分别确认 `ffmpeg`、`ffprobe` 可执行。环境不完整时停止，并报告缺少项。缺依赖时，在获得网络和本机写入授权后运行 `python scripts/bootstrap.py --install`。
2. 运行 `python scripts/bootstrap.py --config-path` 定位配置。必须返回 skill 根目录下的 `config.json`，把命令实际返回的绝对路径记为 `<config_path>`。配置不存在时停止，提示在 `<config_path>` 创建并填写 `config.json`；存在时确认 JSON 可解析。检查过程中只报告配置路径、字段名与通过/缺失状态，禁止输出 token、API key 或完整配置内容。
3. 逐项确认数据源配置完整且不是模板占位值：
   - AniDB：`anidb.http.client` 非空，`anidb.http.clientver` 为正整数；存在有效 client 即启用 AniDB HTTP，不使用额外开关。AniDB HTTP client 是自行登记的客户端标识，不是账号密码。
   - Bangumi：`bangumi.access_token` 与可识别调用方的 `bangumi.user_agent` 均非空。
   - TMDB：`tmdb.access_token` 或 `tmdb.api_key` 至少一项非空。
   - 图片：`artwork.multimodal_review` 与 `artwork.artwork_cache` 缺失时均默认 `false`；存在时必须是 JSON 布尔值。只有 `multimodal_review=true` 才允许 Agent 识图，只有 `artwork_cache=true` 才保存人工原图缓存。
   - 图片并发：`artwork.tmdb_workers` 缺失时默认 6，`artwork.ffmpeg_workers` 缺失时默认 3；存在时必须是整数，取值范围分别为 1..16 和 1..8。并发上限只保护图片/CDN 与 ffmpeg，不是 API 限流参数。
   - 硬链接：`library.hardlinks.enabled` 缺失时默认 `true`，存在时必须是 JSON 布尔值；开启且 `library.hardlinks.root` 非空时必须是绝对路径，为空时使用 `<paths.source_root>/_Jellyfin`。
4. 用户本次未显式给源目录时，另确认 `paths.source_root` 非空且可访问；除非用户本次显式要求不建库，否则确认硬链接目标可访问、与源文件同卷且可写。
5. 任一必填项缺失、仍为模板文字或格式错误时，**立即停止，不得开始扫描或联网请求**。必须使用下列格式提示用户；`<config_path>` 替换为第 2 步取得的绝对路径，`<missing_fields>` 只列缺失或无效字段，配置文件不存在时列出全部必填字段：

   ```text
   配置未完成，流程已暂停。
   请编辑配置文件：<config_path>
   需要填写或修正：<missing_fields>
   填写完成后请回复“已填写”，我会重新执行 Step 0 检查。
   ```

   字段齐全只表示预检通过，不表示凭证一定有效；阶段二首次请求若返回鉴权、限流或网络错误，仍须停止并如实报告，不得把失败伪装成缓存命中或 `not_found`。
6. 仅在上述检查全部通过后，运行 `python scripts/bootstrap.py --mark-step-zero-complete` 写入本机完成标记。标记位于 `<skill-root>/.runtime/step-zero-complete.json`，只保存机器标识摘要，不保存配置或凭证；它随 `.runtime/` 删除且不会随 skill 迁移。

后续脚本统一经 `python scripts/bootstrap.py --run scripts/<脚本>.py <参数>` 执行。公开的根目录 `config.json` 只保留空值、空对象和默认选项；把个人路径、token 与 user agent 直接填写到本机这份文件中，真实凭证不得提交到公开仓库。`cache/` API 缓存随 skill 目录一起迁移；配置中的空 `cache_dir` 默认使用 `<skill-root>/cache`，相对路径也以 skill 根目录为基准，只有绝对路径才覆盖到外部位置。Python 虚拟环境位于 `<skill-root>/.runtime/venvs/`、pip 下载缓存位于 `<skill-root>/.runtime/pip-cache/`，删除 skill 目录时一并删除；`.runtime/` 已被 `.gitignore` 排除且不得放进迁移包，新机器必须运行 `python scripts/bootstrap.py --install` 重建。离线自测：`python scripts/bootstrap.py --run tests/smoke_test.py`，必须得到 `ALL PASSED`。
需要按名称确认 Bangumi subject 时，使用受限只读入口：`python scripts/bootstrap.py --run scripts/bangumi_search.py <关键词> [--limit N] [--json]`；搜索结果只用于候选确认，不替代本地内容与其它元数据源的交叉认证。

Windows 约束：

1. skill 脚本导入 `_common` 后会把 stdout/stderr 调整为 UTF-8。裸 Python 调试使用 `python -X utf8`。
2. NAS/UNC 路径统一传 `//server/share` 正斜杠形式，或先调用 `_common.normalize_path()`。不要把 `\\server\share` 直接交给临时代码中的 `Path.resolve()`。
3. 自动化或重定向终端调用 PowerShell 时使用 `pwsh.exe -NoLogo -NoProfile -NonInteractive -File <script.ps1>`，避免加载用户 profile；若仍出现 `predictive suggestion`/`virtual terminal` 提示，只算终端噪声，不改变刮削结果。

## 3. 扫描 manifest 与摘要

推荐：

```text
python scripts/bootstrap.py --run scripts/identify.py <源目录> --manifest <本机路径.json>
```

省略 `--manifest` 时，完整 manifest 写入系统临时目录 `anime-scraper/manifests/`。摘要应包含：

- 根目录、视频总数；
- 顶层目录候选及正片集号范围；
- SP/未知 hint 数量；
- 重复集号、缺号、标题变化、无法识别文件；
- 完整 manifest 路径。

目录候选只是结构线索，不是最终单元。遇到异常时按 manifest 的 `summary.anomalies` 定向读取 `scan.files`，不要把整个 JSON 再次倾倒到终端。只有显式诊断才用 `--verbose`。

CLI 默认不 probe。需要对机械初判的特殊件读时长时显式加 `--probe-specials`；Agent 已经确定精确文件列表时，优先直接调用 `probe_duration.py`，避免误 probe 正片。

## 4. ffprobe 按需策略

`ffprobe` 只读时长，不认番、不写 NFO。

| 文件类别 | 默认 | 说明 |
|---|---|---|
| SP/OVA/NCOP/NCED/特典/extras | probe | AniDB 语义匹配与时长护栏需要本地秒数 |
| 连续、规范命名的正片 | 不 probe | 依靠集号对齐 Bangumi；runtime 可取 AniDB/TMDB/常见时长或省略 |
| 单文件剧场版/独立 OVA | 建议 probe | 成本低且有助交叉认证 |
| 文件名已明确属于六类略过项 | 不为入库而 probe | token 已决定略过 |

正片只有以下情况才补 probe 涉事文件：集数不一致、集号不可信、正片/SP 边界不清、多版本需要时长消歧、用户要求准确 runtime 或校验完整性。

runtime 优先级：特殊集为本地 probe > AniDB > TMDB > 省略；未 probe 正片为 AniDB/TMDB > 常规时长 > 省略。

## 5. Agent 切单元

脚本只提供结构素材。Agent 根据目录树、`by_dir`、相对路径、标题变化和 hint 判断每个单元是一部作品、一季还是一部剧场版。

- 顶层子目录是线索，不是铁律。
- flat 大包按集号重置点与标题变化切分。
- 深层 SP、混放 OVA、跨语言命名要结合上下文归位。
- hint 是正则初判，可被推翻；番名含 ` - `、罗马数字、`SxxExx` 与方括号冲突时尤其要复核。
- 必须先看摘要异常，再定向读取相关文件；不要因为摘要没列出逐文件详情就跳过复核。

## 6. 作品类型与库分类

是否允许单开条目严格遵守主 `SKILL.md` 的 B+A 双独立门槛。TMDB 有无独立条目不能放宽门槛。

- 一个 Bangumi 条目对应一个库条目；多季、续作、多卷全部拆开，不建“一部剧多季”的合并树。
- TV、连续多集 OVA/OAD/ONA/Web、迷你剧使用 `tv`，正片进入 `Season 01`。
- 单部独立长片使用 `movie`；多部各有正式标题/独立条目的电影分别建条目。
- 附属于 TV 的单集 OVA/OAD/特典进入对应 `Specials/`。若片源中没有该母作正片，SP 仍不得独立建 `tv` 条目：先按 Bangumi/AniDB 的明确父作或关联关系选择已有正片；没有直接父作记录时，仅在同一已确认系列内按标题、人物/世界观、制作时期和首播日期选择证据最强且时间最接近的正片单元。证据不足或并列时停止并要求人工指定，禁止凭目录名猜测。合并后只保留母作的 `tvshow.nfo` 与一个库目录，SP 在其 `Specials/` 中从既有最大 `S00E` 连续编号；源侧只写各 SP 同 stem NFO，不在纯 SP 目录创建 `tvshow.nfo`。
- 剧场版附带特典进入 movie `extras/`。
- 原始片源不移动、不改名；分类只作用于 plan 和硬链接树。

硬链接库使用扁平共同根，作品目录直接位于 link root；`tv` 与 `movie` 是 plan 类型，不建立 `TV Shows/` / `Movies/` 容器。既有未知目录会拒绝写入；1.0 不提供既有库结构迁移。

多个单元共用源目录时，每个单元必须有独立 `output_dir`，且全部收纳在同一 `meta/` 父目录下，例如 `meta/s1/`、`meta/s2/`、`meta/movie1/`：既防止固定文件名 `tvshow.nfo`、`movie.nfo`、`poster.jpg` 相互覆盖，也避免 `xx-meta` 目录散落片源根；`output_dir` 末级仍不得恰为 `meta`/`metadata`。

## 7. 数据源与脚本入口

| 维度 | 主源 |
|---|---|
| 正片结构、标题、简介、日期 | Bangumi |
| 特殊集结构、类型、时长 | AniDB |
| 特殊集 AniDB 缺失时的可选结构兜底 | TMDB Season 0 |
| 角色/声优、制作人员 | Bangumi |
| 作品社区评分 | Bangumi |
| 海报、分集 still、简介兜底 | TMDB |

主要脚本：

- `identify.py`：结构 manifest 与文件名 hint；
- `probe_duration.py`：按需时长；
- `bangumi.py`、`anidb_titles.py`、`anidb_episodes.py`、`tmdb.py`：数据源与缓存；
- `metadata_snapshot.py`：把已确认的 Bangumi subject、AniDB aid、TMDB TV/movie 与指定季/图片候选一次性写入本机元数据快照；不负责认番或选择字段；
- `plan_scaffold.py`：快照+manifest → plan 骨架草稿；`--apply-todo` 把 answers sidecar 合并成最终 plan（S00E、thumb artwork、library_relpath、作品级海报、artwork_review 嵌入全部脚本化）；
- `artwork_review.py --build-request`：从快照确定性组装识图请求 JSON（候选席位、deterministic、完整缓存池），替代手工誊写；
- `match.py`：plan 组装、人员、按集 staff、OP/ED 格式化；特殊集语义匹配仍由 Agent 完成；
- `scrape.py`：dry-run、源侧落盘、可选硬链接库；
- `nfo.py`、`images.py`、`link_library.py`：NFO、图片与硬链接实现；
- 低频工具（音频指纹、全库审计、作品级 staff 修复）：仅在用户明确要求时读取 `references/optional-tools.md`。

需要给 Agent 留存多源原始/规范化数据时，先由 Agent 确认 ID，再使用受限入口：

```text
python scripts/bootstrap.py --run scripts/metadata_snapshot.py --output <快照.json> \
  --bgm-id <subject_id> --anidb-aid <aid> --tmdb-tv-id <tv_id> \
  --tmdb-season <tv_id:season>
```

`--bgm-id`、`--anidb-aid`、`--tmdb-tv-id`、`--tmdb-movie-id` 可重复；`--tmdb-season`、`--tmdb-season-images` 使用 `TV_ID:SEASON`，图片候选使用对应 `--*-images` 参数。命令只读取显式资源，统一复用各数据源模块的缓存与模块级 `RateLimiter`，原子写出 `anime-scraper-metadata-v1` JSON；禁止为一次刮削另写临时 Python/PowerShell 脚本，也禁止把搜索结果当作已确认匹配。

## 8. 限流、缓存与恢复

- AniDB HTTP ban 约 12 小时；复用模块级 `RateLimiter`，不要每次请求重新实例化。
- Bangumi 同样走缓存与限流。
- API 结果落 skill 根目录的 `cache/` JSON 缓存；调试优先读缓存和 dry-run 报告，不要失败后反复真打。
- 多源审查快照统一使用 `metadata_snapshot.py`；它在同一进程内调用各模块，天然复用缓存与限流，快照只写本机指定路径，不修改媒体或 plan。
- 角色/人物 detail 只查询最终入选 ID，禁止预先全量逐人请求。
- 失败后从 manifest、缓存、plan 和 dry-run report 恢复。
- `ffprobe` 与图片下载限制并发，长任务按单元报告进度；并发默认值、重试、`.part` 续传与原子替换细则见 `artwork-library.md` §9。该策略只覆盖图片/ffmpeg，不得削弱 AniDB/Bangumi 的模块级 `RateLimiter`。

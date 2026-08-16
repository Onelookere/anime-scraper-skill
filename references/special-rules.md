# 特殊集判定与匹配规则

> 这是整个 skill 里**最需要人读懂、也最常调整**的地方。

## 目录

- [1. AniDB 剧集类型](#1anidb-剧集类型结构来源)
- [2. 本地文件名归类](#2本地文件名--归类)
- [3. 特殊集语义匹配](#3特殊集匹配核心--agent-判断无脚本函数)
- [3-a. Season 0 展示排序](#3-aseason-0-展示排序)
- [4. TMDB 状态与简介](#4tmdb-对应状态与简介输出)
- [4-a. 特殊集日期](#4-a特殊集-airdate-必填规则-id-434)
- [5. OP/ED 命名](#5oped-特殊集命名)
- [6. aid 解析](#6aid-解析不靠文件哈希)
- [7. 跳过与兜底](#7跳过记账与兜底入库)
- [8. 按集 Staff](#8按集-staff-拆分)

## 1、AniDB 剧集类型(结构来源)

AniDB HTTP API 的 `<episode>/<epno>` 节点带 `type` 数字属性，同时 `epno` 文本带字母前缀；两者一致，判定以 **type 数字**为准，前缀作辅助校验（API 定义：https://wiki.anidb.net/HTTP_API_Definition ）。

| type | epno 前缀 | 含义 | 归类(match.py) | 落库 |
|---|---|---|---|---|
| 1 | (无) | 正片 | `normal` | Season 01 |
| 2 | `S` | 特典/总集篇/OVA | `special` | Specials |
| 3 | `C` | OP/ED(含 NCOP/NCED) | `credit` | Specials |
| 4 | `T` | 预告/PV/CM | `trailer` | Specials 或忽略 |
| 5 | `P` | 恶搞 | `other` | Specials 或忽略 |
| 6 | `O` | 其他 | `other` | Specials 或忽略 |

取分集:`anidb_episodes.get_episodes(aid)` — HTTP API 一次取全并缓存。type→分类映射见 `scripts/anidb_episodes.py` 的 `EPISODE_TYPES`。AniDB 把片头片尾统归 type=3(Credit)；是否"无字幕版(NC)"要结合集标题文本(常含 "Clean"/"Creditless"/"NCOP"/"NCED")判断。

## 2、本地文件名 → 归类

集号 token 归一化(`identify.py` 的 `parse_bdrip_filename` → `hint`)——**这是正则初判(建议),非最终判据;归类由 agent 复核 hint + 目录上下文后定,有权推翻**:
- `12` / `12 END` / `12v2` → **正片**(剥完结词/版本后缀)
- `12.5` → **特殊**(幕间/总集篇)
- `NCOP / NCED / OP / ED / SP / Menu / OVA / PV …` → **特殊**(关键词)
- 子文件夹 `SPs / OVA / Specials …` → 特殊集提示

## 3、特殊集匹配(核心 · agent 判断,无脚本函数)

> **特殊集匹配由 agent 运行时语义完成，无脚本函数**——禁止用脚本按时长+epno 顺序贪心硬配。下面是**给 agent 的判断规则**,不是脚本流程。

**输入两张表**:
- 本地特殊集:文件名(`stem` / `hint`)+ **按需** `probe_duration` 时长(秒)。
- AniDB 特殊集:`anidb_episodes.get_episodes(aid)` → 每条 `epno`(前缀 S/C/T/P/O)、`type`、`length`(分钟)、`title`、`aired`。

> **probe 范围(与规则 ID 4.0 一致):** 默认只给**特殊集**读时长;正片默认不 probe。仅当集数对不上、集号不可信、正片/SP 边界不清、多版本需时长消歧、或用户要求准 runtime 时,才对涉事正片补 probe。禁止对整包所有 mkv 无差别全量 probe。

**判断方法**:先按父作品和非技术文件名 token 聚类，再综合**字幕内容 + 标题语义 + 时长 + 播出日期**把每个本地件对到唯一 AniDB 条目。同系列兄弟条目共享明确 token（例如同一特典系列名）是正向匹配证据；已在该系列入库的条目，禁止仅凭短时长跳过。分类规则如下:

### 前置：时长 < 60 秒兜底略过

先按父作品和同系列 token 与已匹配兄弟条目交叉确认；确认后即使字幕/AniDB 条目不完整或时长 <60s，也按该系列正常入库，并补本地截帧。

**此规则是最后一道兜底，优先级最低。** 仅当所有前置匹配方法（字幕辅助、文件名 token、AniDB 语义+时长交叉校验）**全部失败**后，才对 probe 时长 **< 60 秒**的特殊集视频执行自动略过。

- 若字幕或 AniDB 已明确匹配到条目（即使时长 < 60s），**正常入库**，不略过。
- 若属于规则 ID 4.3.5 六类（Menu/CM/PV 等），不论时长直接由 4.3.5 规则略过，不走此条。
- 仅「非六类 + 全部匹配失败 + < 60s」→ 判定为无观看价值短片（logo/版权页/测试等），直接略过不入库，归入"按规则略过"。
- ≥ 60s 且全部匹配失败 → 不略过，先取文件名/字幕中的真实标题入库，完全无标题依据才走「特典 N」兜底。

### 字幕辅助识别（优先级最高的语义证据）

若特殊集视频旁有同名 `.ass`/`.srt` 等字幕文件，**读取字幕文件前 30~50 行**即可获得关键语义线索：

- **`Title:` / `[Script Info]` 段**：字幕组常在此标注内容名（如 `GO!GO!5 FES'08 Part2`、`劇場預告篇`、`Tenshi Tachi No Kiseki` 等）。
- **前几句 Dialogue**：开场对白通常揭示时代背景/角色/事件（如"在那之后4年"→ 第二季序章；"西历2313年"→ 剧场版前传）。

**不需要也不应读取整个字幕文件**——前几十行足以定性，读完整文件浪费上下文。字幕语义是**决定性证据**：当字幕确认内容身份时，即使本地时长与 AniDB `length` 偏差较大也可匹配（AniDB 标注可能包含片头尾、或同一条目对应不同剪辑版本）。

### OP/ED(credit,epno 前缀 C)

- AniDB 常把一个 OP 拆成多版本(`op1a/1b/1c` = C1/C2/C3…)。**本地只有 1 个 NCOP/NCED → 认该类主版本(epno 最小者)**,哪怕时长缺失也照配(一个 NCOP 本就该对主 OP)。
- 本地标了 `Full/Long` → 优先配 AniDB 长版(标题含 Full/Long,或 `length` 明显更长者)。
- 本地有多个(如另收 Full 版)→ 按时长把它们分别对到不同变体。
- AniDB 无对应 OP/ED 类别 → **不硬配也不跳过**。按本地 `NCOP`/`NCED` token 保留为 Season 0，使用裸 `OP`/`ED` 标题（多个时按 `OP1`/`ED1` 编号）；`anidb_epno`/`anidb_type` 留空，`tmdb_match_status` 按实际 TMDB 结果填写（未检查或请求失败为 `unknown`），无可靠简介时 plot 留空并省略 NFO 节点，日期按 §4-a 回退。只有文件损坏、重复或明确不属本作品时才 `video_path=null` 记账。

### 其余(SP/特典/Menu/OVA/総集編/PV…,epno 前缀 S/T/P/O)

- **标题优先**:AniDB 标题(総集編/映像特典/Menu/PV…)与本地文件名/内容语义对得上再配。
- **特殊集标题本地化**:对已匹配的 `special`/`other`/`trailer` 及 movie `extras`，若 AniDB `title` 是有明确语义的英文短语/句子，结合字幕、文件名和已查到的 Bangumi/官方名称翻译成自然简洁的中文，并把中文写入 plan 的 `title`。不要逐词硬译专有名词；优先使用 Bangumi 简体中文名或可靠官方译名。
- **英文标题保留条件**:AniDB `title` 主要由英文缩写、集号/代码、通用占位词(`Episode`/`Special`/`Extra`/`Unknown` 等)或无实际语义的标签组成时，保留原标题，不为凑中文强行翻译。混合标题保留必要的代码/缩写，只翻译其中有明确语义的部分；翻译含义无法可靠确定时保留原标题并记账。OP/ED 歌名不适用本条，继续按 §5 处理。
- **字幕辅助**:文件名不明确时，读字幕前 30~50 行获取语义线索（见上"字幕辅助识别"）。
- **时长交叉校验**:本地时长应与 AniDB `length` 大致相符;**差得离谱(经验 ~3 分钟以上)且无字幕佐证 = 不是同一内容 → 别硬配**。但若字幕已确认内容身份，时长偏差可放宽（AniDB length 不总准确）。
- 时长缺失(没装 ffmpeg / AniDB 老条目无 `length`)→ 无法用时长证伪时靠标题/字幕/日期判断。
- **OVA 多版本**：不同 `Staff Credit Ver.` 分别保留为独立 S00E，标题注明版本；可共享同一 AniDB epno。仅确认内容等同才作为重复项跳过。
- **BD 独占特典**(NCOP/NCED 之外的 Menu、Storyboard、映像特典等)AniDB 常根本没有对应条目。
- **规则 ID 4.3.5 六类与时长护栏的关系：** 时长反向护栏只用于 **AniDB 匹配**场景（防 1 分钟 Menu 硬配 70 分钟総集編）。**判定本地文件是否属于 4.3.5 六类时，文件名 token 是唯一判据**——Menu 就是 Menu、CM 就是 CM、PV 就是 PV，不因时长超出预期就「存疑保留」。时长超常的六类内容仍然不入库；时长护栏不能推翻 token 的主判据地位。

### 兜底命名：真实标题优先，「特典 N」最后

文件名 token、字幕、时长三者均无法对到任何 AniDB 条目的特殊集（且不属于规则 ID 4.3.5 六类、且时长 ≥ 60s）→ 按本地证据分级命名直接入库：

1. **文件名/字幕携带明确内容标题时（常态）**：活动名、公演名、LIVE/音乐会名、特典节目名等真实标题就是本地证据，直接以该标题命名；可按上节「特殊集标题本地化」翻译成自然简洁的中文，无法可靠翻译时保留原文。禁止把有明确标题的内容压成「特典 N」。
2. **完全没有可依据的标题时（最后手段）**：才以 `特典 N`（N 从 1 起按入库顺序编号）命名。

两级命名都要求 `tmdb_match_status` 按实际 TMDB 结果填写，未检查、请求失败或拿不准时用 `unknown`；无可靠简介时 plot 留空并省略 NFO 节点，日期按 §4-a 优先级回退。

- **不再跳过记账**——能入库就入库，先用真实标题，实在无名再用「特典 N」。Jellyfin 显示真实标题总比"特典 1/没有这个视频"好。
- 已跳过记账的 `video_path=null` 语义仍保留给**确实不应入库**的场景（如文件损坏、重复、明确不是本作品内容）。
- "特典 N"编号在同一单元内连续；不与已有 AniDB 匹配的特殊集编号冲突（S00E 编号在所有匹配项之后顺延）。

## 3-a、Season 0 展示排序

全部特殊集完成语义匹配、规则性略过项剔除后，Agent 再按标题/内容判断重要度和系列，填写 `special_order`，最后分配 `S00E`；禁止在匹配未结束时直接沿用扫描或文件名顺序编号。

优先级只是 Agent 的参考分层，数值越小越靠前，脚本不按标题自行分类：

| priority | 参考内容 |
|---:|---|
| 10 | 叙事剧集类：OVA/OAD/ONA、剧场版/电影特别篇、特别篇、总集篇、序章/续章等 |
| 20 | OP/ED/NCOP/NCED 等片头片尾附加媒体 |
| 30 | 番外、小剧场、短篇、剧中剧、Drama 等附属叙事内容 |
| 40 | 制作/真人内容：花絮、录音/配音、声优节目、访谈、外景、Event/Live 等 |
| 90 | 仍需入库但无法可靠归类的其它内容，例如“特典 N” |

每个实际入库的 Season 0 条目都要填写：

```json
"special_order": {
  "priority": 10,
  "series_key": "OVA",
  "series_order": 1,
  "item_order": 1,
  "source_index": 0
}
```

`priority`、`series_key` 和两个序号是 Agent 的语义判断；`source_index` 只用于同序号时稳定决胜和追溯原始位置。相同 priority 内，同系列必须连续；`series_order` 决定系列顺序，`item_order` 按标题明确数字或语义分段顺序排列，无可靠序号才保持该系列原相对顺序。排序键固定为 `(priority, series_order, item_order, source_index)`；OP 在同级通常先于 ED，但仍以 Agent 对标题和来源的判断为准。

排序完成后，Season 0 的 `episode` 必须连续递增，`AniDB epno` 只保留为匹配证据，不覆盖上述展示顺序。dry-run 必须输出排序校验；缺少 `special_order`、同系列未连续、排序键乱序、编号重复/断号时直接拒绝落盘，不自动替 Agent 重排。

纯 SP 目录没有可依附的正片时，不得把它伪装成独立 TV：归入 `workflow.md` §6 选定的最近已有正片条目，并从该条目现有 Season 0 最大编号继续编号。若无法用父作关系或同系列证据唯一确定目标，保留待人工，不建库条目。

同一 OVA 的不同 `Staff Credit Ver.` 必须相邻；剧中剧的 OVA、OP、ED 归入同一连续组，不与母作 OP/ED 混排。

## 4、TMDB 对应状态与简介输出

所有会落入 Season 0 的本地内容，都要在 agent 组 plan 时记录 `tmdb_match_status`。图片选择顺序固定为：

1. **先查 TMDB**：读取候选 TV 的 Season 0，按标题、日期、时长和内容语义逐项交叉认证，并保存 `tmdb_identity` 快照。
2. **验证通过才用远程 still**：父条目 `tmdb_identity.status=verified`、首播年份与当前 Bangumi/AniDB 单元一致，且该条有对应 `still_url` 时，plan 使用 `method=tmdb`。
3. **其余情况回退本地截帧**：身份为 `ambiguous/unknown`、年份不一致、明确没有对应 still，或 TMDB 查询未得到可用认证时，去掉 `tmdb_still_url`，使用 `method=frame` 和对应 `fallback_video_path`；状态保留为 `unknown` 或 `not_found`，不把失败伪装成 `not_found`。

TMDB 可能根本没有该 TV 的 Season 0；调用脚本的可选探测接口在明确 404 时返回空表即可，这表示“无该季度”，不表示所有请求错误都可忽略。认证失败、限流、超时和 5xx 等仍必须记录并处理；它们不能产生远程 still，但在内容本身已确认应入库时可以走本地截帧回退：

- `matched`：通过标题关键词、日期、时长等交叉认证，确认 TMDB Season 0 有对应条目；可使用已认证的 TMDB 简介。
- `not_found`：已检查并确认 TMDB 没有对应内容；只表示不能使用 TMDB 对应条目的简介或 still，不能清空已经从 Bangumi、官方资料、字幕或其它可靠来源确认的真实简介。
- `unknown`：未检查、请求失败或证据不足；不得猜成 `not_found`，也不得因此跳过本应入库的视频。字段缺失也按此状态处理。

TMDB 可能把同一系列的独立作品合并为一个 TV 条目。只要 Season 0 出现 `matched` 或 still，plan 还必须记录父条目的 `tmdb_identity` 快照（`id`、`name`、`original_name`、`first_air_date`、`status`）。只有 `status=verified` 且父条目首播年份与当前 Bangumi/AniDB 单元一致时才允许使用远程 still；合并、跨季或年份不一致一律使用本地 `frame`，并将状态保留为 `unknown` 或 `not_found`。下载成功不等于作品归属认证。也就是说，`frame` 是 TMDB 认证失败后的回退，不是跳过 TMDB 查询的默认路径。

`not_found` 可覆盖 AniDB 的 `special`、`credit`、`trailer`、`other` 等类别，例如 BD 特典短篇、NCOP/NCED、Menu、Storyboard；共同前提是最终 `season == 0` 且 TMDB 明确无对应内容。**不能从 AniDB 未匹配或 `thumb` 为空推断**：TMDB 条目可能没有 still，图片也可能尚未下载。`plot` 与状态完全解耦：有经交叉认证的真实简介就写，没有或最终为空才完全省略 `<plot>`；任何状态都不得输出 `<plot />`。

## 4-a、特殊集 `airdate` 必填(规则 ID 4.3.4)

入库的 Season 0 / movie extras **每一条** plan 都要有非空 `airdate`,NFO 写出 `<aired>`。缺日期时 Jellyfin 会按 S00E 编号联网乱补年份(同名真人剧/衍生很常见)。

**取值优先级:**

1. AniDB 该条 `aired`;
2. TMDB S0 交叉认证条目的 `air_date`(`matched` 时);
3. 文件名/AnidB 标明的覆盖区间 → **区间最小正片集**的 Bangumi/TMDB 播出日(如 NCOP `EP.08~11` → 第 8 集 aired);
4. 本单元正片第一集 `airdate`(与正片日期保持一致);
5. `show.premiered`(再不行至少锁年)。

`not_found`、`unknown` 或无 plot 的 NCOP/NCED **同样要写日期**。`scrape.py` 在组装后调用 `match.validate_special_airdates`；有 `video_path` 的 Season 0 / movie extras 缺日期时，`--dry-run` 和实际落盘都会直接拒绝，只有 `video_path=null` 的跳过项允许为空。阶段四仍抽查 Specials NFO 不得缺 `<aired>`。

## 5、OP/ED 特殊集命名

格式:`OP - 歌名` / `OP1 - 歌名`(单个不带号、多个带号 1 基);官方中文名优先、否则日文原名。

### 歌名取法(兜底链)

以 `metadata-rules.md` §8 为 canonical（规则 ID 4.6）：Bangumi 关联条目是首选线索；官网、官方发布或对应关系无歧义的可靠维基可直接采用；普通歌词站、个人整理页和论坛只能作为线索，须与 Bangumi 条目或片源 CD 曲目表交叉印证。拿不准时使用裸 `OP`/`ED` 并记账。

> **硬规则:绝不凭记忆猜歌名。**证据不足时使用裸 `OP`/`ED` 并记账。

> **注意:歌名不在 Bangumi 作品 infobox**(那只有主题歌 staff:作词/作曲/演出),而在**关联条目**(type=3 音乐)。

## 6、aid 解析(不靠文件哈希)

国内压制组大量不在 AniDB 哈希库,故**不用 FILE 哈希识别**。
`anidb_titles.py` 下载并**持久缓存** AniDB 静态标题库(`cache/anidb/anime-titles.xml.gz`,>7 天才重下),按番名子串搜 aid → agent 跨语言判定。

## 7、跳过记账与兜底入库

跳过记账（`video_path=null`，仅限文件损坏、重复、明确不属本作品）、「特典 N」兜底入库与时长 <60s 自动略过的完整判定规则均见 §3，本节不再重复。每个跳过项必须保留原文件路径、判定理由和使用过的证据；同系列兄弟条目已入库时不得只以时长作为理由。

**硬规则:跳过/入库的文件绝不创建空 nfo / 占位 nfo。** 要么写一份有真实内容的 nfo,要么什么都不写。

## 8、按集 Staff 拆分

**适用于所有带集数标注的 staff 字段(不限脚本)**。
Bangumi infobox 值如 `A(1,3,7,13)、B(2,6,10)`:
- `parse_episode_credits(值)` → `(per_ep, defaults)`;
- `episode_writers(集号, per_ep, 默认)` → 该集的 staff 列表;
- 未标注的集回退默认(系列构成/无标注者)。
- 解析时**先剥括注再拆分**(括注内逗号 `(1,3,7)` 不能被当分隔符拆碎)。

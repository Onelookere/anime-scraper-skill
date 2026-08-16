# 元数据、标题、简介与人员规则

## 目录

- [1. 逐单元取数](#1-逐单元取数)
- [2. 作品显示标题](#2-作品显示标题)
- [3. Jellyfin sorttitle](#3-jellyfin-sorttitle)
- [4. Bangumi 社区评分](#4-bangumi-社区评分)
- [5. 正片匹配与简介](#5-正片匹配与简介)
- [6. TMDB Season 0 状态](#6-tmdb-season-0-状态)
- [7. 人员与按集 staff](#7-人员与按集-staff)
- [8. OP/ED 歌名](#8-oped-歌名)

## 1. 逐单元取数

1. 用 `bangumi.search_subjects` 确认 `bgm_id`，再取 subject、episodes、characters、persons、theme songs。
2. 用 `anidb_titles.search` 跨语言确认 aid，再取完整 episode 表。一个 Bangumi 条目可能对应多个 AniDB aid；出现 AniDB 拆分时必须查齐相关 aid。
3. 用 TMDB 查 TV/movie、季度分集、still、海报与可选 Season 0。特殊集在线条目必须先交叉认证，禁止按 S00E 编号盲取。
4. 所有请求优先复用缓存。角色/人员 detail 只请求最终入选项。

三源编号可能全部错位。标题、日期、时长、字幕与作品关系至少组合出可靠证据后，才能把在线条目的标题、简介或图片写入本地条目。

制作公司字段不固定：每个 Bangumi 作品的 infobox 可能使用不同键名（如 `动画制作`、`動畫製作`、`动画公司`、`製作公司`、`アニメーション制作`）或缺失。优先从 `persons` 找 `kind=2`、关系为动画制作的公司实体 ID，再用公司详情的 `简体中文名` 作为 `<studio>`；同一实体的中/日/英名称和别名统一显示。`製作`委员会只作佐证，不整段写入 studio；没有唯一可确认的公司时留空并记账。

## 2. 作品显示标题

`<title>` 使用中文互联网最通用的叫法，不机械取 Bangumi `name_cn`。

1. 英文/日文原名在中文社区更通用时使用原名，例如 RWBY、K-ON!、JOJO、EVA、Fate/stay night、CLANNAD。
2. 中文名更通用时使用中文名，例如“进击的巨人”“鬼灭之刃”。
3. 冷门或新番拿不准时向用户确认。
4. 不写 `<originaltitle>`，plan 不再填 `title_original`。
5. 后续季采用统一季度后缀时，可给无印首季补同格式后缀，例如 `RWBY Volume 1`；AFTER STORY 一类独立名称不处理。
6. 剧场版、总集篇、特别篇等类型词：当 sorttitle 需要按系列聚合、且类型词位于 title 开头时，语义重排为「去掉开头的类型词，其余成分保持原顺序与完整性，再把类型词移到末尾」——「剧场版 白箱」→「白箱 剧场版」，「剧场版 咒术回战 0」→「咒术回战 0 剧场版」。编号/子标题是作品名固有部分时不得拆散（「咒术回战 0」是整体，0 不能与「咒术回战」分开）；类型词本就在系列名之后且自然（如「孤独摇滚！ 剧场总集篇 Re：」）时保持原名；无法分离出类型词时保持原名并在 plan 注明原因。

禁止在脚本中用正则批量移动类型词。Agent 逐条理解候选与作品关系，把最终值直接写入 plan。

## 3. Jellyfin sorttitle

新 TV/Movie plan 必填：

```text
{Agent 确认的系列主标题} {首播日期 YYYY-MM-DD} {最终显示标题}
```

- Agent 先根据已查的 Bangumi/AniDB 作品关系、标题资料和可靠知识判断是否存在可靠的共同系列主标题；系列名判断属于语义工作，不在脚本中用正则、最长公共前缀或固定词表强行提取。
- 能确认系列主标题时，直接把该名称写入 sorttitle 前缀；它可以是最终 title 中不存在的上位系列名，例如不同标题的同一 franchise。所有同系列条目使用完全相同的前缀，并保持所选中文/原文写法一致。
- 排序前缀的文字系统必须与最终 title 首字符一致：中文 title 用中文系列前缀，罗马字 title 用罗马字系列前缀，禁止跨语言错位（如 title「白箱」不得以 SHIROBAKO 作排序前缀——Jellyfin 会按 SortName 排进字母区，与显示首字错位）。`scrape.py` 在 dry-run 与落盘前按 title/前缀首字符 ASCII 一致性强制拒绝；剧场版/续作跟随母作前缀属正常聚合，不受此限。
- 排序前缀通常就是系列主标题，因此 title 中的剧场版/总集篇/特别篇类型词应按 §2.6 后缀化（系列主标题在前）；把类型词放在 title 开头（如「剧场版 白箱」）而前缀用系列名，会造成显示首字「剧」、排序落「白」区的错位。
- 仅凭一个“的”、标点、相似 token 或模型猜测不足以确认系列关系。无法可靠确认时，使用完整 title 作为前缀，不猜测、不改写、不为了聚合加入别名、罗马字、拼音或机器翻译。
- 日期取真实 `premiered`。只有年份时用 `YYYY-01-01` 作为排序日期；完全未知时用 `9999-12-31` 作为仅用于 sorttitle 的“未知最后”哨兵，禁止写入 `<premiered>`。
- 同日需要指定顺序时，在日期后增加两位序号。
- 只写 `<sorttitle>`，不同时写 `<sortname>`。

plan 仍只有一个 `sorttitle` 字段，不增加 `series_title` 或 `franchise_group`。
脚本从 sorttitle 末尾反向拆出前缀，校验前缀、排序日期和最终 title 的结构；前缀
在 title 中时属于低风险的原文基底，前缀不在 title 中时必须由 Agent 负责系列语义
审查。脚本不联网、不替 Agent 证明系列关系；无法可靠判断时使用完整 title。

`scrape.py` 对 plan 从既有 `sorttitle` 末尾反向拆出前缀并在任何媒体写入前
校验；不增加 plan 字段或联网请求。示例：

```text
title=真盖塔 世界最后之日
正确：真盖塔 1998-08-25 真盖塔 世界最后之日
安全回退：真盖塔 世界最后之日 1998-08-25 真盖塔 世界最后之日
Agent 已确认系列时也正确：盖塔机器人 1998-08-25 真盖塔 世界最后之日
无法确认系列时不要使用上面的外部前缀，回退为安全回退写法。
```

## 4. Bangumi 社区评分

从 `bangumi.get_subject(bgm_id)["rating"]` 读取作品级 `score`，写入 `show.rating` / `movie.rating`，NFO 使用 `<rating>`。

- 只有 `rating.total > 0` 且 score 是有限数值、`0 < score <= 10` 时才写。
- 无投票、缺失、0、NaN、越界或非法值省略节点。
- 不写 `<criticrating>` 或 `<customrating>`。
- 不与 AniDB/TMDB 分数求平均、择高或堆多来源。
- 不给单集写作品评分；`comment`、rank、投票分布不映射为评分。
- 评分随 subject 缓存读取，不额外重复请求。

## 5. 正片匹配与简介

正片先整体确认：读取 manifest 中的目录候选、`by_dir`、异常线索及相关文件对象，核对连续性、跳号、重复号和标题变化。规范命名可整体采信；合并盘、v2、小数集、冲突集号等异常才逐文件细判。

正片文件集号对齐 Bangumi episode；标题、中文文字优先 Bangumi。默认不为定名全量 probe，runtime 按工作流 reference 的优先级填写。

最终 plot 取第一个可靠的非空结果：

1. Bangumi 中文简介；
2. TMDB 中文简介；
3. Bangumi 日文简介；
4. TMDB 英文简介。

组 plan 后逐集核对：已查询来源存在简介时，正片条目不得省略 `plot`。正片 `plot` 为空时，必须在同一条目写入 `plot_evidence`，固定包含 `bangumi_zh`、`tmdb_zh`、`bangumi_ja`、`tmdb_en`，且四项都明确为 `empty`；缺字段、未检查或任一项为 `present` 都表示来源链未穷尽。dry-run 会把缺失集号和证据错误写入报告，留报告后拒绝落盘。Specials/extras 的空 plot 不要求该字段。

Bangumi desc 由 Agent 语义清洗：只保留剧情叙事，剥离 staff 表、播出元数据、集数统计等。不要用正则假装覆盖所有格式。`nfo.py` 负责折叠多余空行，并在写作品级 NFO 时把独立的 `staff_note` 追加为简介末尾 staff 行。

组 plan 后，任何 plot 含 `脚本：`、`分镜：`、`演出：`、`作画监督：` 等明确 staff 行都会被 preflight 拒绝；清洗后为空时继续按上述优先级取 TMDB 等来源，不得因原始 desc 非空而停止。正片最终仍为空会在 dry-run 单独告警。

plan 的 `plot` 与 `staff_note` 必须分字段：`plot` 只保留剧情，`staff_note` 只保留制作人员；写 NFO 时将 `staff_note` 作为简介末尾独立行输出，不能把它提前混入来源简介。

任何在线简介都必须先确认本地集与在线集是同一内容。最终 plot 为空时完全省略 `<plot>`，禁止输出 `<plot />`。

## 6. TMDB Season 0 状态

canonical：`special-rules.md` §4。`matched` / `not_found` / `unknown` 三态语义、错误处理与 plot 节点解耦规则均以该节为准，本文件不再重复。

## 7. 人员与按集 staff

声优：`build_actors(chars)` 生成 name=声优、role=角色、thumb=头像、type=Actor 的卡片。组装 TV/Movie 时按 Bangumi 人物 ID（缺失时按姓名）合并同一声优的角色，最多保留 20 位并全部置于 crew 前；crew 不占额度。

显示名优先级为 Bangumi detail `简体中文名` > 原名。不要用 OpenCC 把日文机械转简体。

制作人员：`build_crew(persons)` 返回 cards 与 note。

- 可映射为 Jellyfin PersonKind 的职位写入带头像的 actor 卡片；`type` 使用 Director/Writer/Composer/Producer 等真实类型，`role` 保留 Bangumi 原职位名。只有无干净类型的职位进入 `staff_note`，并在作品级 NFO 简介末尾单独显示；发现导演、脚本、音乐等可映射职位标签时，preflight 拒绝。
- 公司 `kind == 2` 不进人员卡片。
- plan 不填 show/movie 的 `directors`/`writers`；crew 统一进入 actors。
- 需要审计中文名缺失时给 `build_actors` / `build_crew` 传 `unresolved=[]`。请求失败不能伪装成“无中文名”。
- 有 `bgm_id` 的 plan 必填 `show/movie.staff_status`：有 crew 卡或 `staff_note` 时为 `present`，两者都没有且已检查 persons 时才为 `empty`。只有 `staff_note` 或 `empty` 状态必须同时带 `staff_audit.persons_checked=true` 与 `mappable_crew_count=0`，防止可映射 crew 被静默漏掉。缺失或矛盾不得落盘。

按集 staff：Bangumi infobox 值如 `A(1,3,7)、B(2,6)` 时，调用 `parse_episode_credits`，再按集号使用 `episode_writers`。该规则适用于所有带集数标注的 staff 字段，不限于脚本；未标注集回退系列构成或无标注默认人员。

## 8. OP/ED 歌名

显示格式：`OP - 歌名` / `OP1 - 歌名`，单个不带号，多个从 1 编号。官方中文歌名优先，否则用日文原名。

Bangumi 对应不明 ≠ 证据链已穷尽；必须完成一次有界的官网/可靠维基查证并把结果记入 `song_evidence`，仍不明才使用裸 `OP`/`ED`。

- 编号按所属作品和不同歌曲计算；剧中剧/OVA 自带 OP/ED 不接母作序号，在同一库中用 `《作品名》OP/ED - 歌名` 区分。同一歌曲的不同画面版本共享编号。
- 文件名中的 `EP12`、`EP24`、`Ver.1`、`Ver.2`、`Full`、`Long` 等有效内容提示必须保留为标题后缀；不得用无差别连续序号抹掉版本语义。

证据优先级：

1. 作品/制作方/唱片公司官网、官方发布、官方流媒体或官方 YouTube；
2. 对应关系明确的可靠维基；
3. Bangumi 关联音乐条目；
4. 片源 CD 曲目表；
5. 普通歌词站、个人页、论坛只能作线索，并需与 Bangumi 或 CD 曲目表交叉印证。

来源冲突时按优先级取值；已有可靠对应即停止，否则只查官网/官方发布和一个可靠维基。仍不明才用裸 `OP`/`ED`，并按 `plan-contract.md` 填 `song_evidence`：已对应为 `resolved`，裸标题为 `exhausted`；后者至少记录 `official/wiki/web` 或 `network_error` 及原因。只查 Bangumi 即停止、或没有记账，均视为审查失败。

音频指纹是低频可选工具，只有在上述证据链穷尽且用户明确要求时才读取 `references/optional-tools.md`；默认刮削不加载、不安装其依赖。

Bangumi 关联条目可能用全角 `／` 合并 OP 与 ED，例如 `メグメル／だんご大家族`；前项对应 OP，后项对应 ED，拆分后分别命名。

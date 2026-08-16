"""把 agent 产出的"单元 plan"组装成统一中间结构(dataclass),供 nfo.py 渲染。

分工:
- **agent**(运行时)负责识别、认番、文件↔分集匹配、判置信、决定跳过谁,产出一份
  完整的 unit **plan**(JSON);正片文字取 Bangumi、特殊集结构取 AniDB,agent 已填好。
- **本模块**只做确定性的"plan → dataclass"组装,不含任何识别/匹配智能。

plan 结构(TV):
  {
    "type": "tv",
    "output_dir": "<单元目录>",
    "show": {title, sorttitle, rating, plot, premiered, studio, staff{}, anidb_aid, bgm_id},
    "episodes": [
      {category, season, episode, title, plot, plot_evidence, airdate,
       runtime, anidb_epno, anidb_type, tmdb_match_status, video_path(可为 null=跳过)}
    ]
  }
plan 结构(剧场版):type="movie",用 "movie"(或 "show")承载单部电影字段,可带 video_path。
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict


# 归一化分类
CATEGORY_NORMAL = "normal"
CATEGORY_SPECIAL = "special"
CATEGORY_CREDIT = "credit"      # OP/ED/NCOP/NCED
CATEGORY_TRAILER = "trailer"
CATEGORY_OTHER = "other"

# TMDB Season 0 交叉认证状态。不能用 thumb 是否为空推断：条目可能没有 still，
# 图片也可能尚未处理。非法值统一按 unknown 保守处理。
TMDB_MATCHED = "matched"
TMDB_NOT_FOUND = "not_found"
TMDB_UNKNOWN = "unknown"
_TMDB_MATCH_STATUSES = {TMDB_MATCHED, TMDB_NOT_FOUND, TMDB_UNKNOWN}

MAX_VOICE_ACTORS = 20


def _tmdb_match_status(value) -> str:
    """归一化 plan 中的 TMDB 匹配状态；未知输入不触发特殊行为。"""
    return value if value in _TMDB_MATCH_STATUSES else TMDB_UNKNOWN


def _community_rating(value) -> float | None:
    """归一化 Jellyfin 社区评分；Bangumi score 为 0~10，0 表示无有效评分。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) and 0 < score <= 10 else None


@dataclass
class MergedEpisode:
    category: str
    season: int
    episode: int
    title: str
    plot: str = ""
    airdate: str | None = None
    runtime: int | None = None
    # 每集人员(Jellyfin 单集页支持):脚本按集不同 → 各集自己的 <credits>
    directors: list = field(default_factory=list)
    writers: list = field(default_factory=list)
    thumb: str = ""
    # 溯源,便于排查
    anidb_epno: str = ""
    anidb_type: str = ""
    # TMDB Season 0 是否有经交叉认证的对应条目
    tmdb_match_status: str = TMDB_UNKNOWN


@dataclass
class MergedShow:
    title: str
    plot: str = ""
    premiered: str | None = None
    studio: str = ""
    # 声优与 crew 统一走 actor 卡片(唯一能带 <thumb> 头像的标签)
    actors: list = field(default_factory=list)        # [{name,role,thumb,type}] → <actor>
    staff_note: str = ""                              # 人设/音乐/美术… 无对应槽 → 作品级 NFO 简介末尾 staff 行，并供 plan 审计
    staff_status: str = ""                             # present/empty;由 plan 显式审查
    anidb_aid: int | None = None
    bgm_id: int | None = None
    lockdata: bool = False
    episodes: list = field(default_factory=list)
    # Jellyfin 按“标题/名称”排序时使用；不改变页面显示的 title。
    sorttitle: str = ""
    # Bangumi 作品级社区评分(0~10)；写入 Jellyfin <rating>。
    rating: float | None = None


def _normalize_actor_cards(cards: list) -> list:
    """合并并前置最多 20 位唯一声优；crew 保持原顺序且不占额度。"""
    actors: list = []
    crew: list = []
    seen: dict = {}
    for index, raw in enumerate(cards or []):
        if not isinstance(raw, dict):
            continue
        card = dict(raw)
        if (card.get("type") or "Actor") != "Actor":
            crew.append(card)
            continue
        person_id = card.get("bangumi_person_id")
        key = (("id", str(person_id)) if person_id not in (None, "") else
               ("name", card.get("name")) if card.get("name") else ("row", index))
        if key not in seen:
            if len(actors) == MAX_VOICE_ACTORS:
                continue
            card["type"] = "Actor"
            seen[key] = card
            actors.append(card)
            continue
        roles = [part.strip() for value in (seen[key].get("role"), card.get("role"))
                 for part in str(value or "").split("、") if part.strip()]
        seen[key]["role"] = "、".join(dict.fromkeys(roles))
    return actors + crew


def _show_from(d: dict) -> MergedShow:
    return MergedShow(
        title=d.get("title", ""),
        sorttitle=d.get("sorttitle", ""),
        rating=_community_rating(d.get("rating")),
        plot=d.get("plot", ""),
        premiered=d.get("premiered"),
        studio=d.get("studio", ""),
        actors=_normalize_actor_cards(list(d.get("actors") or [])),
        staff_note=d.get("staff_note") or "",
        staff_status=d.get("staff_status") or "",
        anidb_aid=d.get("anidb_aid"),
        bgm_id=d.get("bgm_id"),
        lockdata=bool(d.get("lockdata", False)),
    )


def assemble(plan: dict) -> tuple[MergedShow, list]:
    """TV 单元:plan → (MergedShow, 每集视频路径列表[与 episodes 一一对应,None=跳过])。"""
    show = _show_from(plan.get("show", {}))
    video_paths: list = []
    for e in plan.get("episodes", []):
        cat = e.get("category", CATEGORY_NORMAL)
        show.episodes.append(MergedEpisode(
            category=cat,
            season=e.get("season", 1 if cat == CATEGORY_NORMAL else 0),
            episode=e.get("episode", 0),
            title=e.get("title", ""),
            plot=e.get("plot", ""),
            airdate=e.get("airdate"),
            runtime=e.get("runtime"),
            directors=e.get("directors", []) or [],
            writers=e.get("writers", []) or [],
            thumb=e.get("thumb", ""),
            anidb_epno=e.get("anidb_epno", ""),
            anidb_type=e.get("anidb_type", ""),
            tmdb_match_status=_tmdb_match_status(e.get("tmdb_match_status")),
        ))
        video_paths.append(e.get("video_path"))
    return show, video_paths


def assemble_movie(plan: dict) -> tuple[MergedShow, list]:
    """剧场版单元:plan → (MergedShow, extras 视频路径列表)。

    extras 结构与 TV episodes 一致(category/season=0/episode/title/video_path…),
    放入 show.episodes 供 nfo/link_library 使用。
    """
    show = _show_from(plan.get("movie") or plan.get("show", {}))
    extras_paths: list = []
    for e in plan.get("extras", []):
        cat = e.get("category", CATEGORY_SPECIAL)
        show.episodes.append(MergedEpisode(
            category=cat,
            season=e.get("season", 0),
            episode=e.get("episode", 0),
            title=e.get("title", ""),
            plot=e.get("plot", ""),
            airdate=e.get("airdate"),
            runtime=e.get("runtime"),
            directors=e.get("directors", []) or [],
            writers=e.get("writers", []) or [],
            thumb=e.get("thumb", ""),
            anidb_epno=e.get("anidb_epno", ""),
            anidb_type=e.get("anidb_type", ""),
            tmdb_match_status=_tmdb_match_status(e.get("tmdb_match_status")),
        ))
        extras_paths.append(e.get("video_path"))
    return show, extras_paths


def validate_special_airdates(show: MergedShow, video_paths: list) -> None:
    """拒绝有源视频却缺少播出日期的 Season 0 / movie extras。

    若 Season 0 NFO 缺少 ``<aired>``，Jellyfin 会按 S00E 编号联网猜测，可能把
    同名真人剧、续作或衍生作的年份写进来。只有实际入库的项目才需要拦截：
    ``video_path=None`` 表示 agent 已跳过、不会生成 NFO，允许其日期为空。

    此函数刻意只校验非空性，不代替 agent 判断具体日期应取 AniDB、TMDB 或正片
    锚点的哪一个；取值优先级见规则 ID 4.3.4（special-rules.md §4-a）。
    """
    errors: list[str] = []
    for index, episode in enumerate(show.episodes):
        video_path = video_paths[index] if index < len(video_paths) else None
        if not video_path or episode.season != 0:
            continue
        if isinstance(episode.airdate, str) and episode.airdate.strip():
            continue
        label = episode.title or episode.anidb_epno or f"S00E{episode.episode:02d}"
        errors.append(f"第 {index + 1} 项 {label!r}")
    if errors:
        raise ValueError(
            "入库的 Season 0 / movie extras 必须提供非空 airdate（否则 Jellyfin "
            "可能联网误匹配年份）: " + "；".join(errors)
        )


def extract_staff(infobox: dict) -> dict:
    """从 Bangumi infobox 抽常用 staff(供 agent 组 plan 时调用)。"""
    keys = ["原作", "导演", "监督", "脚本", "系列构成", "人物设定",
            "音乐", "动画制作", "美术监督"]
    return {k: infobox[k] for k in keys if k in infobox}


# ── 人员结构化(修 Jellyfin"全归编剧/无头像"问题)────────────────
#
# Jellyfin/Kodi 只有 <actor> 能带 <thumb> 头像,因此声优与 crew 统一走 actor 卡片
# (crew 用真实 PersonKind)。音乐/人设/美术等无干净类型的职位拆成文字附注,不塞
# 人物卡,以免全变假"编剧"。人名再做拆分 + 剥集数/工作室括注。

# 拆多人的分隔符(全/半角顿号、逗号、分号、斜杠)
_PEOPLE_SEP = re.compile(r"[、,，;；/]+")
# 括注(集数标注 (1,3,7)、工作室 (Elements Garden)…)
_PAREN = re.compile(r"[(（【\[].*?[)）】\]]")


def clean_people(value) -> list:
    """把 infobox 里 'A(1,3,7)、B(2,6)' 这种多人值拆成干净单名列表。

    注意:必须**先剥括注、再拆分**——集数标注 `(1,3,7)` 内部含逗号,
    若先按逗号拆会把它拆碎。
    """
    if not value:
        return []
    s = _PAREN.sub("", str(value))                 # 先剥集数/工作室括注(内部可能含逗号)
    out: list = []
    for part in _PEOPLE_SEP.split(s):
        name = part.replace("　", " ").strip(" :：")
        if name:
            out.append(name)
    return out


def build_actors(characters: list, limit: int | None = MAX_VOICE_ACTORS,
                 main_only: bool = True,
                 cfg: dict | None = None, localize: bool = True,
                 unresolved: list | None = None) -> list:
    """把 bangumi.get_characters() 结果转成 nfo <actor> 列表。

    name=声优、role=角色、thumb=声优头像(符合 <actor> 语义)。
    main_only=True 时只留主角/配角(去客串/路人)，主角在前。取每角色第一位声优。
    最终 TV/Movie 组装会合并同一声优、限制为 20 位并前置于 crew。
    """
    if not isinstance(characters, list):
        raise TypeError("characters 必须是数组；请传入 characters 缓存，不要传 subject")
    if any(not isinstance(item, dict) for item in characters):
        raise TypeError("characters 条目必须是对象")

    rank = {"主角": 0, "主人公": 0, "配角": 1}
    ordered = sorted(characters, key=lambda c: rank.get(c.get("relation"), 2))
    selected = [c for c in ordered
                if (not main_only or rank.get(c.get("relation"), 2) < 2)
                and (c.get("actors") or [{}])[0].get("name")]
    selected = selected[:min(int(limit or MAX_VOICE_ACTORS), MAX_VOICE_ACTORS)]

    resolver = None
    effective_cfg = cfg
    if localize and any(
        c.get("id") or (c.get("actors") or [{}])[0].get("id") for c in selected
    ):
        import bangumi
        resolver = bangumi.resolve_display_name
        effective_cfg = cfg if cfg is not None else bangumi.load_config()

    out: list = []
    for c in selected:
        cv = (c.get("actors") or [{}])[0]
        actor_name = cv.get("name") or ""
        if resolver:
            actor_name = resolver("persons", cv.get("id"), actor_name,
                                  effective_cfg, unresolved)
        role_name = c.get("name") or ""
        if resolver:
            role_name = resolver("characters", c.get("id"), role_name,
                                 effective_cfg, unresolved)
        card = {
            "name": actor_name,               # 声优
            "role": role_name,                # 角色
            "thumb": cv.get("image") or "",  # 声优头像
            "type": "Actor",
        }
        if cv.get("id") not in (None, ""):
            card["bangumi_person_id"] = cv.get("id")
        out.append(card)
    return out


# ── 每集脚本(按集不同)──────────────────────────────────────────
#
# Bangumi infobox 的"脚本"值形如 'A(1,3,7,13)、B(2,6,10)…',括注就是"负责的集号"。
# 解析后写进各集 nfo 的 <credits>(Jellyfin 单集页支持),未标注的集回退默认脚本家。

# 只按顶层顿号/分号拆条目;**不含逗号**——逗号在括注 (1,3,7) 内部,拆了会碎。
_CREDIT_ENTRY_SEP = re.compile(r"[、；;]")


def _parse_ep_numbers(spec: str) -> list:
    """'1,3,7,13' / '1-3' / '1,3-5' → [1,3,7,13] / [1,2,3] / [1,3,4,5]。"""
    out: list = []
    for tok in re.split(r"[,，]", spec or ""):
        tok = tok.strip()
        m = re.match(r"^(\d+)\s*[-–~〜]\s*(\d+)$", tok)
        if m:
            out.extend(range(int(m.group(1)), int(m.group(2)) + 1))
        elif tok.isdigit():
            out.append(int(tok))
    return out


def parse_episode_credits(value) -> tuple:
    """把'A(1,3,7,13)、B(2,6,10)'解析成 (per_ep, defaults)。

    per_ep: {集号:int -> [脚本名,...]};defaults: 无集号标注的名字(视为不限集/默认)。
    """
    per_ep: dict = defaultdict(list)
    defaults: list = []
    if not value:
        return per_ep, defaults
    for part in _CREDIT_ENTRY_SEP.split(str(value)):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.*?)[(（【\[](.*?)[)）】\]]\s*$", part)
        if m:
            name = m.group(1).replace("　", " ").strip(" :：")
            eps = _parse_ep_numbers(m.group(2))
            if name and eps:
                for e in eps:
                    per_ep[e].append(name)
            elif name:                       # 有括注但不是集号(如工作室)→ 当默认
                defaults.append(name)
        else:
            name = part.replace("　", " ").strip(" :：")
            if name:
                defaults.append(name)
    return per_ep, defaults


def episode_writers(ep_num: int, per_ep: dict, fallback: list | None = None) -> list:
    """取某集的脚本:有按集标注用之,否则回退默认脚本家(系列构成/未标注者)。"""
    return list(per_ep.get(ep_num) or (fallback or []))


# ── crew 也做成带头像卡片(用 Bangumi 原职位名当 role)────────────
#
# 决策:不迁就 Jellyfin 的固定人物类型,`role` 直接写 Bangumi 原职位名(导演/脚本/音乐…);
# `type` 只设一个非 Actor 的兜底值(避免声优那种"饰演 XX"前缀),显示以 role 为准。

# 想收进卡片的主创职位(过滤掉歌曲 credit / 公司 / 助理等长尾),并作展示排序
_CREW_ORDER = [
    "导演", "監督", "総監督", "总监督",
    "系列构成", "系列構成", "脚本", "原作", "原案", "人物原案",
    "音乐", "音楽", "制片人", "动画制片人",
    # 以下无干净类型 → 进简介附注,不做卡片
    "人物设定", "人物設定", "总作画监督", "總作画監督",
    "美术监督", "美術監督", "音响监督", "音響監督", "摄影监督", "色彩设计",
]
_CREW_MAIN = set(_CREW_ORDER)

# 能干净映射成中文类型 label 的职位 → PersonKind(这些做成带头像的卡片)
_CREW_KIND = {
    "导演": "Director", "監督": "Director", "総監督": "Director", "总监督": "Director",
    "系列构成": "Writer", "系列構成": "Writer", "脚本": "Writer", "原作": "Writer",
    "原案": "Writer", "人物原案": "Writer",
    "音乐": "Composer", "音楽": "Composer",
    "制片人": "Producer", "动画制片人": "Producer",
}


def build_crew(persons: list, individuals_only: bool = True,
               cfg: dict | None = None, localize: bool = True,
               unresolved: list | None = None) -> tuple:
    """把 bangumi.get_persons() 结果拆成 (cards, note)。

    - cards: 能映射成干净 Jellyfin 类型的职位 → 带头像的 <actor>
             (type=真实类型 Director/Writer/Composer/Producer;role=Bangumi 原职位名。
              Jellyfin 会显示成"类型/职位"双层,如"编剧/脚本"——顶行类型、次行准确职位)。
    - note:  无干净类型的职位(人设/美术/音响/摄影/色彩/总作画监督)→ 保留为 plan 审计附注，
             并由作品级 NFO 生成器输出到简介末尾的独立 staff 行。
    同一人多职位合并;默认跳过公司(kind==2)。
    """
    if not isinstance(persons, list):
        raise TypeError("persons 必须是数组；请传入 persons 缓存，不要传 subject")
    if any(not isinstance(item, dict) for item in persons):
        raise TypeError("persons 条目必须是对象")

    selected: list = []
    for p in persons:
        rel = p.get("relation")
        if rel not in _CREW_MAIN:
            continue
        if individuals_only and p.get("kind") == 2:
            continue
        if p.get("name"):
            selected.append(p)

    resolver = None
    effective_cfg = cfg
    if localize and any(p.get("id") for p in selected):
        import bangumi
        resolver = bangumi.resolve_display_name
        effective_cfg = cfg if cfg is not None else bangumi.load_config()

    resolved_names: dict = {}
    by_name: dict = {}
    order: list = []
    for p in selected:
        rel = p.get("relation")
        nm = p.get("name")
        if resolver and p.get("id"):
            cache_key = (p.get("id"), nm)
            if cache_key not in resolved_names:
                resolved_names[cache_key] = resolver(
                    "persons", p.get("id"), nm, effective_cfg, unresolved)
            nm = resolved_names[cache_key]
        if nm not in by_name:
            by_name[nm] = {"positions": [], "thumb": p.get("image") or ""}
            order.append(nm)
        if rel not in by_name[nm]["positions"]:
            by_name[nm]["positions"].append(rel)
        if not by_name[nm]["thumb"] and p.get("image"):
            by_name[nm]["thumb"] = p.get("image")

    def _rank(r: str) -> int:
        return _CREW_ORDER.index(r) if r in _CREW_ORDER else len(_CREW_ORDER)

    cards: list = []
    note_map: dict = {}                                   # 职位 -> [人名]
    for nm in order:
        positions = sorted(by_name[nm]["positions"], key=_rank)
        mappable = [r for r in positions if r in _CREW_KIND]
        if mappable:                                      # 有可映射职位 → 做卡片
            cards.append({
                "name": nm,
                "role": "、".join(positions),             # 原职位名(含随附的无类型职位)
                "thumb": by_name[nm]["thumb"],
                "type": _CREW_KIND[mappable[0]],          # 真实类型(顶行 label)
                "_rank": _rank(positions[0]),
            })
        else:                                             # 全是无类型职位 → 进附注
            for r in positions:
                note_map.setdefault(r, []).append(nm)

    cards.sort(key=lambda c: c["_rank"])
    for c in cards:
        del c["_rank"]

    parts: list = []                                      # 附注按职位顺序拼(人名去重)
    for pos in _CREW_ORDER:
        if pos in note_map:
            names = list(dict.fromkeys(note_map[pos]))
            parts.append(f"{pos}:{'、'.join(names)}")
    return cards, "　".join(parts)


def populate_show_staff(show: dict, persons: list, *, characters: list | None = None,
                         cfg: dict | None = None, localize: bool = True,
                         unresolved: list | None = None) -> dict:
    """用已取到的 Bangumi persons 填充作品级 crew/staff_note。

    只保留 plan 里原有的声优卡片，crew 每次从人员源重新生成，避免修复任务
    重复追加旧 crew。返回的字典就是可写回 plan 的 show 对象。
    """
    if not isinstance(show, dict):
        raise ValueError("show 必须是对象")
    if characters is None:
        voice_cards = [
            dict(card) for card in (show.get("actors") or [])
            if isinstance(card, dict) and (card.get("type") or "Actor") == "Actor"
        ]
    else:
        voice_cards = build_actors(
            characters, cfg=cfg, localize=localize, unresolved=unresolved
        )
    crew_cards, note = build_crew(
        persons, cfg=cfg, localize=localize, unresolved=unresolved
    )
    show["actors"] = _normalize_actor_cards(voice_cards + crew_cards)
    show["staff_note"] = note
    show["staff_status"] = "present" if (crew_cards or note) else "empty"
    show["staff_audit"] = {
        "persons_checked": True,
        "source_person_count": len(persons),
        "mappable_crew_count": len(crew_cards),
    }
    return show


def theme_special_title(kind: str, songs: list, index: int = 0, total: int = 1) -> str:
    """按规则拼 OP/ED 特殊集标题:`OP` / `OP1` / `OP2 - 歌名`。

    kind: 'op' | 'ed';songs: `bangumi.get_theme_songs()[kind]`(可为空);
    index: 0 基序号;total: 该类本地件总数。
    规则:单个不带号、多个带号(OP1/OP2…,1 基);有歌名则接 ` - 歌名`
    (歌名用 songs[index]['display'] = 官方中文优先、否则日文原名);
    Bangumi 无该类主题歌(如 MyGO 无片尾曲)→ 只出裸 `OP`/`ED`,由 agent 另补。
    """
    prefix = "OP" if kind == "op" else "ED"
    if total > 1:
        prefix = f"{prefix}{index + 1}"
    song = ""
    if songs and 0 <= index < len(songs):
        song = (songs[index].get("display") or "").strip()
    return f"{prefix} - {song}" if song else prefix


# 制作公司(studio)在 Bangumi 各条目里键名不一,甚至缺失。宽容匹配多个变体;
# 都没有时返回 ""，由 agent 从 Copyright 行 / tags / 已知知识兜底(见 SKILL.md)。
_STUDIO_KEYS = ["动画制作", "動畫製作", "动画公司", "製作公司", "アニメーション制作", "动画・制作"]


def pick_studio(infobox: dict) -> str:
    """从 infobox 宽容取制作公司;取不到返回 ""(交给 agent 兜底)。"""
    for k in _STUDIO_KEYS:
        v = infobox.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def to_dict(show: MergedShow) -> dict:
    """便于 --dry-run 打印 / 调试。"""
    return asdict(show)


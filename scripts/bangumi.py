"""Bangumi API 抓取器(主文字源)。

职责:取 subject(作品)与 episodes(分集),做本地缓存,
提供中文标题 / 简介 / staff / 分集标题等文字字段。
新增 search_subjects() 用于按名称搜索番剧,支持 auto-identify 流程。

API 文档:https://bangumi.github.io/api/
- POST /v0/search/subjects
- GET  /v0/subjects/{id}
- GET  /v0/episodes?subject_id={id}&limit=100&offset=..
需要 Authorization: Bearer 与规范 User-Agent。
"""
from __future__ import annotations

import json

import requests

from _common import (
    RateLimiter,
    atomic_write_json,
    cache_dir,
    load_config,
)

API_BASE = "https://api.bgm.tv"

# Bangumi 分集 type,详见 special-rules.md;与 AniDB 交叉印证
EPISODE_TYPE = {
    0: "normal",   # 本篇(正片)
    1: "special",  # SP
    2: "op",       # 片头
    3: "ed",       # 片尾
    4: "trailer",  # 预告 / CM
    6: "other",    # 其他
}

# 模块级共享限流器:同 anidb.py,跨请求复用同一实例
_LIMITER: RateLimiter | None = None


def _get_limiter(cfg: dict) -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter.from_config(cfg)
    return _LIMITER


def _session(cfg: dict) -> requests.Session:
    bgm = cfg["bangumi"]
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bgm['access_token']}",
        "User-Agent": bgm.get("user_agent", "anime-scraper"),
        "Accept": "application/json",
    })
    return s


def _get_json(url: str, cfg: dict, cache_key: str, session: requests.Session,
              limiter: RateLimiter) -> dict:
    """带缓存的 GET JSON。"""
    cache = cache_dir(cfg, "bangumi")
    cached = cache / f"{cache_key}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    limiter.wait()
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    atomic_write_json(cached, data)
    return data


def _post_json(url: str, cfg: dict, cache_key: str, session: requests.Session,
               limiter: RateLimiter, body: dict) -> dict:
    """带缓存的 POST JSON。"""
    cache = cache_dir(cfg, "bangumi")
    cached = cache / f"{cache_key}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    limiter.wait()
    resp = session.post(url, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    atomic_write_json(cached, data)
    return data


# ── 公开数据获取 ────────────────────────────────────────────────

def get_subject(subject_id: int, cfg: dict | None = None) -> dict:
    """返回作品级文字字段(已抽取常用项,原始数据在 raw)。"""
    cfg = cfg if cfg is not None else load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/v0/subjects/{subject_id}", cfg,
                    f"subject_{subject_id}", session, limiter)

    infobox = {item.get("key"): item.get("value") for item in raw.get("infobox", [])}
    return {
        "id": subject_id,
        "name": raw.get("name"),               # 原名(通常日文)
        "name_cn": raw.get("name_cn"),         # 中文名
        "summary": raw.get("summary"),         # 简介
        "date": raw.get("date"),
        "images": raw.get("images"),
        "infobox": infobox,                    # 含 staff、制作等
        "rating": raw.get("rating"),
        "raw": raw,
    }


def get_episodes(subject_id: int, cfg: dict | None = None) -> list[dict]:
    """返回分集列表(翻页取全),抽取常用字段。"""
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)

    episodes: list[dict] = []
    offset = 0
    limit = 100
    while True:
        url = f"{API_BASE}/v0/episodes?subject_id={subject_id}&limit={limit}&offset={offset}"
        data = _get_json(url, cfg, f"episodes_{subject_id}_{offset}", session, limiter)
        items = data.get("data", [])
        for it in items:
            episodes.append({
                "type": EPISODE_TYPE.get(it.get("type"), "other"),
                "type_id": it.get("type"),
                "sort": it.get("sort"),        # 排序号(可能含小数)
                "ep": it.get("ep"),            # 本篇集号
                "name": it.get("name"),        # 原名标题
                "name_cn": it.get("name_cn"),  # 中文标题
                "airdate": it.get("airdate") or None,
                "desc": it.get("desc"),
                "duration": it.get("duration"),
            })
        total = data.get("total", len(episodes))
        offset += limit
        if offset >= total or not items:
            break

    return episodes


# ── 搜索 ──────────────────────────────────────────────────────────

def search_subjects(keyword: str, cfg: dict | None = None,
                    limit: int = 10) -> list[dict]:
    """搜索 Bangumi 番剧(Anime type=2)。

    返回精简匹配结果列表，每项含:
      id, name, name_cn, date, score, rank, short_summary, images, type
    """
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    # type=[2] = 仅动画; sort=match 按匹配度
    body = {"keyword": keyword, "sort": "match", "filter": {"type": [2]}}
    cache_key = f"search_{keyword.lower().replace(' ', '_')[:60]}"
    raw = _post_json(f"{API_BASE}/v0/search/subjects", cfg, cache_key,
                     session, limiter, body)
    results = []
    for item in (raw.get("data") or [])[:limit]:
        results.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "name_cn": item.get("name_cn"),
            "date": item.get("date"),
            "score": (item.get("rating") or {}).get("score"),
            "rank": (item.get("rating") or {}).get("rank"),
            "short_summary": item.get("short_summary"),
            "images": item.get("images"),
            "type": item.get("type"),
        })
    return results


def normalize_characters(raw) -> list[dict]:
    """Normalize a cached/raw ``characters`` response for match.py."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        items = raw["data"]
    else:
        raise ValueError("characters 缓存必须是数组或包含 data 数组的对象")

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("characters 缓存条目必须是对象")
        actors = []
        for actor in (item.get("actors") or []):
            if not isinstance(actor, dict):
                raise ValueError("characters 缓存中的 actor 条目必须是对象")
            images = actor.get("images") or {}
            actors.append({
                "id": actor.get("id"),
                "name": actor.get("name"),
                "image": actor.get("image") or images.get("large")
                         or images.get("medium") or images.get("grid") or "",
            })
        images = item.get("images") or {}
        out.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "relation": item.get("relation"),
            "image": item.get("image") or images.get("large")
                     or images.get("medium") or images.get("grid") or "",
            "actors": actors,
        })
    return out


def get_characters(subject_id: int, cfg: dict | None = None) -> list[dict]:
    """取作品的角色 + 声优(含头像),供 nfo 生成 <actor>。

    调 GET /v0/subjects/{id}/characters(一次返回全部角色,声优内嵌,无需逐个请求)。
    返回精简结构,每项:
      {id, name(角色名), relation(主角/配角/客串), images(角色图),
       actors:[{id, name(声优), image(声优头像URL)}]}
    """
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/v0/subjects/{subject_id}/characters", cfg,
                    f"characters_{subject_id}", session, limiter)
    return normalize_characters(raw)


def normalize_persons(raw) -> list[dict]:
    """Normalize a cached/raw ``persons`` response for match.py."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
        items = raw["data"]
    else:
        raise ValueError("persons 缓存必须是数组或包含 data 数组的对象")

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("persons 缓存条目必须是对象")
        images = item.get("images") or {}
        out.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "relation": item.get("relation"),
            "image": item.get("image") or images.get("medium")
                     or images.get("grid") or images.get("large") or "",
            "kind": item.get("kind", item.get("type")),
        })
    return out


def get_persons(subject_id: int, cfg: dict | None = None) -> list[dict]:
    """取作品的制作人员(staff),用于给 crew 也配头像卡片。

    调 GET /v0/subjects/{id}/persons。返回每项:
      {id, name(职员名), relation(**Bangumi 原职位名**:导演/脚本/系列构成/音乐/美术监督…),
       image(头像URL), kind(1=个人 / 2=公司 / 3=组合)}
    """
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/v0/subjects/{subject_id}/persons", cfg,
                    f"persons_{subject_id}", session, limiter)
    return normalize_persons(raw)


# ── 人名 / 角色名中文显示名 ─────────────────────────────────────

def get_character_detail(character_id: int, cfg: dict | None = None) -> dict:
    """取单个角色详情；响应落 bangumi/character_{id}.json 缓存。"""
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    return _get_json(f"{API_BASE}/v0/characters/{character_id}", cfg,
                     f"character_{character_id}", session, limiter)


def get_person_detail(person_id: int, cfg: dict | None = None) -> dict:
    """取单个人物详情；响应落 bangumi/person_{id}.json 缓存。"""
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    return _get_json(f"{API_BASE}/v0/persons/{person_id}", cfg,
                     f"person_{person_id}", session, limiter)


def _simplified_name_from_infobox(infobox) -> str:
    """从 Bangumi detail infobox 取结构化“简体中文名”。"""
    if isinstance(infobox, dict):
        value = infobox.get("简体中文名")
    else:
        value = None
        for item in infobox or []:
            if isinstance(item, dict) and item.get("key") == "简体中文名":
                value = item.get("value")
                break
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), "")
    if isinstance(value, dict):
        value = value.get("v") or value.get("value") or ""
    return value.strip() if isinstance(value, str) else ""


def resolve_display_name(kind: str, entity_id, original_name: str | None,
                         cfg: dict | None = None,
                         unresolved: list | None = None) -> str:
    """按 detail 简体中文名 → 原名解析显示名。

    kind 仅接受 ``characters`` / ``persons``。没有 ID 的数据直接返回原名，
    因而不会加载配置或触网。detail 缺少简体中文名时，可通过 unresolved 列表
    记账。请求失败不吞掉，避免把认证/限流错误误判为“无中文名”。
    """
    original = (original_name or "").strip()
    if entity_id in (None, ""):
        return original
    if kind not in {"characters", "persons"}:
        raise ValueError(f"未知名字类型: {kind!r}")

    cfg = cfg if cfg is not None else load_config()
    detail = (get_character_detail(entity_id, cfg) if kind == "characters"
              else get_person_detail(entity_id, cfg))
    simplified = _simplified_name_from_infobox(detail.get("infobox"))
    if simplified:
        return simplified

    if unresolved is not None:
        unresolved.append({"kind": kind, "id": entity_id, "name": original})
    return original


# 关联条目里"音乐"类(type=3)的 relation → OP/ED/插入歌 归类
_THEME_REL = {
    "片头曲": "op", "片頭曲": "op", "OP": "op", "オープニング": "op",
    "片尾曲": "ed", "片尾": "ed", "ED": "ed", "エンディング": "ed",
    "插入歌": "insert", "挿入歌": "insert",
}


def get_theme_songs(subject_id: int, cfg: dict | None = None) -> dict:
    """从 Bangumi 关联条目取主题歌信息(OP/ED/插入歌),供特殊集 OP/ED 命名。

    调 GET /v0/subjects/{id}/subjects,筛音乐类(type==3)按 relation 归类。
    结果缓存(related_{id}.json),"取数据"时拉一次、重命名时复用。
    返回:{"op":[{name,name_cn,display}], "ed":[...], "insert":[...]}
      display = 官方中文名(name_cn)优先,否则日文原名(name)。
    """
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/v0/subjects/{subject_id}/subjects", cfg,
                    f"related_{subject_id}", session, limiter)
    items = raw if isinstance(raw, list) else (raw.get("data") or [])
    result: dict = {"op": [], "ed": [], "insert": []}
    for it in items:
        if it.get("type") != 3:                          # 3 = 音乐
            continue
        slot = _THEME_REL.get(it.get("relation"))
        if not slot:
            continue
        name, name_cn = it.get("name"), it.get("name_cn")
        result[slot].append({
            "name": name,
            "name_cn": name_cn,
            "display": (name_cn or name or "").strip(),  # 官方中文优先,否则日文
        })
    return result

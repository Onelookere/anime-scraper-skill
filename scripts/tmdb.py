"""TMDB API 抓取器：搜剧、取分集封面 + Season 0 特殊集列表。

用途：
  1. 所有番的所有分集（正片+特殊）→ 封面 still URL 写进 nfo <thumb>
  2. AniDB 无数据时（如美国动画）→ 回退 TMDB Season 0 做特殊集编号对齐

API 文档：https://developer.themoviedb.org/reference
需要 skill 配置的 tmdb.api_key 或 tmdb.access_token。
"""
from __future__ import annotations

import argparse
import json
import sys

import requests
from requests import HTTPError

from _common import RateLimiter, atomic_write_json, cache_dir, load_config

API_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/original"

_LIMITER: RateLimiter | None = None


def _get_limiter(cfg: dict) -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter.from_config(cfg)
    return _LIMITER


def _session(cfg: dict) -> requests.Session:
    tmdb = cfg.get("tmdb", {}) or {}
    s = requests.Session()
    token = tmdb.get("access_token", "")
    api_key = tmdb.get("api_key", "")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        s.params = {"api_key": api_key}
    else:
        raise ValueError("skill 配置需要 tmdb.access_token 或 tmdb.api_key")
    s.headers["Accept"] = "application/json"
    return s


def _get_json(url: str, cfg: dict, cache_key: str, session: requests.Session,
              limiter: RateLimiter, params: dict | None = None) -> dict | list:
    cache = cache_dir(cfg, "tmdb")
    cached = cache / f"{cache_key}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    limiter.wait()
    resp = session.get(url, params=params or {}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    atomic_write_json(cached, data)
    return data


def search_tv(keyword: str, cfg: dict | None = None, limit: int = 10) -> list[dict]:
    """搜索 TMDB 电视剧（动画）。返回精简结果。"""
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    safe_kw = keyword.lower().replace(" ", "_")[:60]
    raw = _get_json(f"{API_BASE}/search/tv", cfg, f"search_tv_{safe_kw}",
                    session, limiter, params={"query": keyword, "language": "zh-CN"})
    results = []
    for item in (raw.get("results") or [])[:limit]:
        results.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "original_name": item.get("original_name"),
            "first_air_date": item.get("first_air_date"),
            "overview": (item.get("overview") or "")[:100],
        })
    return results


def _image_candidates(raw: dict, kind: str) -> list[dict]:
    """归一 TMDB images 响应；选图仍由 agent 根据语言/票数/作品认证决定。"""
    results = []
    for item in raw.get(kind, []) or []:
        file_path = item.get("file_path") or ""
        if not file_path:
            continue
        results.append({
            "kind": kind.rstrip("s"), "file_path": file_path,
            "url": f"{IMG_BASE}{file_path}", "language": item.get("iso_639_1") or "",
            "vote_count": item.get("vote_count") or 0,
            "vote_average": item.get("vote_average") or 0,
            "width": item.get("width") or 0, "height": item.get("height") or 0,
        })
    return results


_POSTER_LANGUAGE_RANK = {
    "zh": 0, "zh-cn": 0, "zh-tw": 0,
    "ja": 1,
    "en": 2,
}

POSTER_PREFERRED_MIN = (1000, 1500)
POSTER_ACCEPTABLE_MIN = (800, 1200)
POSTER_RESOLUTION_RANK = {"unknown": -1, "low": 0, "acceptable": 1, "preferred": 2}


def poster_resolution_class(candidate: dict) -> str:
    """按原始像素把竖版海报分为 preferred/acceptable/low/unknown。"""
    try:
        width = int(candidate.get("width") or 0)
        height = int(candidate.get("height") or 0)
    except (TypeError, ValueError):
        return "unknown"
    if width <= 0 or height <= 0:
        return "unknown"
    if width >= POSTER_PREFERRED_MIN[0] and height >= POSTER_PREFERRED_MIN[1]:
        return "preferred"
    if width >= POSTER_ACCEPTABLE_MIN[0] and height >= POSTER_ACCEPTABLE_MIN[1]:
        return "acceptable"
    return "low"


def _poster_quality_key(candidate: dict) -> tuple:
    """小票差竞争时的 poster 排序键：语言、分辨率、评分、票数、路径。"""
    language = str(candidate.get("language") or "").lower()
    language_rank = _POSTER_LANGUAGE_RANK.get(language, 3)
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    return (language_rank, -(width * height), -width,
            -float(candidate.get("vote_average") or 0),
            -int(candidate.get("vote_count") or 0),
            str(candidate.get("file_path") or ""))


def rank_image_candidates(candidates: list[dict]) -> list[dict]:
    """按本 skill 的 TMDB 多图竞选规则稳定排序候选，不修改输入列表。

    票数全在 0--2 的冷门池，或最高票与候选满足「至少一方 <=2 且票差 <=2」
    的小票差竞争组，均以语言、分辨率、评分、票数排序；其余候选以票数为主。
    这把规则 ID 4.7a（artwork-library.md）的 agent 规则固化为可复用、可离线测试的机械步骤，
    不负责条目认证、下载或写入 plan。
    """
    valid = [dict(item) for item in candidates if item.get("file_path")]
    if not valid:
        return []

    votes = [int(item.get("vote_count") or 0) for item in valid]
    if max(votes) <= 2:
        return sorted(valid, key=_poster_quality_key)

    top_votes = max(votes)
    low_competitors = [
        item for item in valid
        if min(top_votes, int(item.get("vote_count") or 0)) <= 2
        and top_votes - int(item.get("vote_count") or 0) <= 2
    ]
    # 竞争组包含最高票候选本身；否则 3 vs 1 这类小票差会只把 1 票图放入组，
    # 反而不能按语言/分辨率与 3 票主图比较。
    if low_competitors:
        small_gap = [item for item in valid
                     if int(item.get("vote_count") or 0) == top_votes]
        small_gap.extend(item for item in low_competitors if item not in small_gap)
        small_ids = {id(item) for item in small_gap}
        return [*sorted(small_gap, key=_poster_quality_key), *sorted(
            (item for item in valid if id(item) not in small_ids),
            key=lambda item: (-int(item.get("vote_count") or 0), *_poster_quality_key(item)),
        )]

    return sorted(valid, key=lambda item: (
        -int(item.get("vote_count") or 0), *_poster_quality_key(item)))


def _portrait_posters(candidates: list[dict]) -> list[dict]:
    """只保留有明确竖向尺寸的 poster 候选。"""
    return [item for item in candidates
            if int(item.get("height") or 0) > int(item.get("width") or 0)]


def poster_review_candidates(candidates: list[dict], limit: int = 5) -> list[dict]:
    """返回供 Agent 识图的竖版 poster 候选，默认最多五张。

    排序只决定候选覆盖，不决定最终胜负。除排序头部外，分别为中文、日文和全池
    最高分辨率代表保留席位，防止本地化高质量海报被票数或另一语种挤出 contact
    sheet。水印、标题、构图与跨季度风格仍由 Agent 综合识图判断。输入列表不会修改。
    """
    if limit < 1:
        raise ValueError("poster review limit 必须 >= 1")
    ranked: list[dict] = []
    seen_paths: set[str] = set()
    for item in rank_image_candidates(_portrait_posters(candidates)):
        path = str(item.get("file_path") or "")
        if not path or path in seen_paths:
            continue
        ranked.append(item)
        seen_paths.add(path)
    if len(ranked) <= limit:
        return ranked

    def area(item: dict) -> int:
        return int(item.get("width") or 0) * int(item.get("height") or 0)

    def localized_pool(languages: set[str]) -> list[dict]:
        return [
            item for item in ranked
            if str(item.get("language") or "").lower() in languages
        ]

    challengers = []
    for languages in ({"zh", "zh-cn", "zh-tw"}, {"ja"}):
        pool = localized_pool(languages)
        if pool:
            challengers.append(pool[0])
            challengers.append(max(pool, key=area))
    challengers.append(max(ranked, key=area))

    unique_challengers = []
    for item in challengers:
        if item not in unique_challengers:
            unique_challengers.append(item)
    selected = ranked[:max(1, limit - len(unique_challengers))]
    for item in [*unique_challengers, *ranked]:
        if item not in selected:
            selected.append(item)
        if len(selected) >= limit:
            break
    return selected


try:
    import imagehash
    from PIL import Image as _PILImage
    _HAS_IMAGEHASH = True
except ImportError:
    _HAS_IMAGEHASH = False


def _image_hash_distances(
        url_a: str, url_b: str, *, cfg: dict | None = None,
        cache: dict[str, tuple[object, object, object] | None] | None = None,
) -> tuple[int, int, int] | None:
    """一次下载/解码每张图，返回 pHash、aHash、wHash 汉明距离。

    imagehash 未安装时直接报错（属于 requirements.txt 声明的必装依赖）。
    下载、解码或任一 hash 计算失败时返回 None。
    """
    if not _HAS_IMAGEHASH:
        raise ImportError(
            "imagehash 未安装，请运行 pip install -r requirements.txt")
    from io import BytesIO
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    hash_cache = cache if cache is not None else {}

    def load_hashes(url: str) -> tuple[object, object, object] | None:
        if url in hash_cache:
            return hash_cache[url]
        try:
            limiter.wait()
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            with _PILImage.open(BytesIO(resp.content)) as source:
                image = source.convert("RGB")
                hashes = (
                    imagehash.phash(image),
                    imagehash.average_hash(image),
                    imagehash.whash(image),
                )
        except Exception:
            hashes = None
        hash_cache[url] = hashes
        return hashes

    hashes_a = load_hashes(url_a)
    hashes_b = load_hashes(url_b)
    if hashes_a is None or hashes_b is None:
        return None
    return tuple(int(a - b) for a, b in zip(hashes_a, hashes_b))


_PHASH_SAME_THRESHOLD = 16
_AHASH_SAME_THRESHOLD = 16
_WHASH_SAME_THRESHOLD = 16


def select_specials_poster(*, main_poster: dict, main_poster_candidates: list[dict],
                           season_zero_poster_candidates: list[dict],
                           reference_main_posters: list[dict] | None = None,
                           fallback_specials_poster: dict | None = None) -> dict:
    """按固定三段回退选择 Specials 海报，返回候选及其选择来源。

    1. Season 0 中与本系列全部季度主 poster 视觉不同的最高优先竖版 poster；
    2. 主 poster 同一候选池中、排序最高且 file_path 不同，并且 pHash、
       aHash、wHash 相对全部季度主 poster 都超过各自阈值的竖版 poster；
    3. 没有视觉上不同的候选时，可复用其它季度的 Specials poster；
    4. 没有提供独立回退图时不生成 Specials poster。

    ``reference_main_posters`` 只放本系列各季度主图，不放其它季度 Specials，
    因而季度间 Specials 可以相同，但不能与任一季度主图相同或高度相似。
    """
    season_zero = rank_image_candidates(_portrait_posters(season_zero_poster_candidates))
    main_path = main_poster.get("file_path")
    references = []
    seen_reference_paths = set()
    for reference in [main_poster, *(reference_main_posters or [])]:
        path = reference.get("file_path")
        if path and path not in seen_reference_paths:
            references.append(reference)
            seen_reference_paths.add(path)
    hash_cache: dict[str, tuple[object, object, object] | None] = {}

    def is_distinct(item: dict) -> bool:
        """判断候选是否能安全地作为 Specials 海报。

        任一 hash 无法计算时按“未知”处理并跳过，不能把失败当成“视觉不同”，
        否则下载/解码失败会再次绕过同图护栏。imagehash 不可用时保留文档约定，
        退回 file_path 去重。
        """
        path = item.get("file_path")
        if not path or path in seen_reference_paths:
            return False
        if not _HAS_IMAGEHASH:
            return True
        candidate_url = f"{IMG_BASE}{path}"
        for reference in references:
            distances = _image_hash_distances(
                f"{IMG_BASE}{reference['file_path']}", candidate_url,
                cache=hash_cache)
            if distances is None:
                return False
            phash, ahash, whash = distances
            if not (phash > _PHASH_SAME_THRESHOLD
                    and ahash > _AHASH_SAME_THRESHOLD
                    and whash > _WHASH_SAME_THRESHOLD):
                return False
        return True

    for item in season_zero:
        if is_distinct(item):
            return {"candidate": item, "selection": "season_zero"}

    ranked = rank_image_candidates(_portrait_posters(main_poster_candidates))
    for item in ranked:
        if is_distinct(item):
            return {"candidate": item, "selection": "main_pool_alternative"}
    if fallback_specials_poster and is_distinct(fallback_specials_poster):
        return {"candidate": dict(fallback_specials_poster),
                "selection": "series_specials_reuse"}
    return {"candidate": None, "selection": "none"}


def get_tv_images(tv_id: int, cfg: dict | None = None) -> dict[str, list[dict]]:
    """返回 TV poster/backdrop/logo 图片候选；用 rank_image_candidates 选择。"""
    cfg = cfg or load_config()
    session, limiter = _session(cfg), _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/tv/{tv_id}/images", cfg, f"tv_{tv_id}_images",
                    session, limiter, params={})
    return {"posters": _image_candidates(raw, "posters"),
            "backdrops": _image_candidates(raw, "backdrops"),
            "logos": _image_candidates(raw, "logos")}


def get_season_images(tv_id: int, season_number: int, cfg: dict | None = None) -> dict[str, list[dict]]:
    """返回一个 TV season 的 poster/backdrop/logo 图片候选。"""
    cfg = cfg or load_config()
    session, limiter = _session(cfg), _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/tv/{tv_id}/season/{season_number}/images", cfg,
                    f"tv_{tv_id}_season_{season_number}_images", session, limiter, params={})
    return {"posters": _image_candidates(raw, "posters"),
            "backdrops": _image_candidates(raw, "backdrops"),
            "logos": _image_candidates(raw, "logos")}


def get_optional_season_images(tv_id: int, season_number: int,
                               cfg: dict | None = None) -> dict[str, list[dict]]:
    """读取可选季度的图片；明确 404 时返回空候选池，其它错误继续抛出。

    只用于 Season 0 等可不存在的季度。不能将认证、限流、超时或服务器故障
    误当作「没有 Specials 海报」。
    """
    try:
        return get_season_images(tv_id, season_number, cfg)
    except HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return {"posters": [], "backdrops": [], "logos": []}
        raise


def search_movie(keyword: str, cfg: dict | None = None, limit: int = 10) -> list[dict]:
    """搜索 TMDB 电影，供 agent 交叉认证剧场版。"""
    cfg = cfg or load_config()
    session, limiter = _session(cfg), _get_limiter(cfg)
    safe_kw = keyword.lower().replace(" ", "_")[:60]
    raw = _get_json(f"{API_BASE}/search/movie", cfg, f"search_movie_{safe_kw}",
                    session, limiter, params={"query": keyword, "language": "zh-CN"})
    return [{"id": item.get("id"), "title": item.get("title"),
             "original_title": item.get("original_title"),
             "release_date": item.get("release_date"),
             "overview": (item.get("overview") or "")[:100]}
            for item in (raw.get("results") or [])[:limit]]


def get_movie_detail(movie_id: int, cfg: dict | None = None) -> dict:
    """取电影详情：标题/日期/简介/片长 + 默认海报/背景路径。

    图片候选列表请用 get_movie_images；overview/runtime 供 plan 写 plot 与
    <runtime>，避免 agent 再打一遍 API。
    """
    cfg = cfg or load_config()
    session, limiter = _session(cfg), _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/movie/{movie_id}", cfg, f"movie_{movie_id}",
                    session, limiter, params={"language": "zh-CN"})
    return {
        "id": movie_id,
        "title": raw.get("title"),
        "original_title": raw.get("original_title"),
        "release_date": raw.get("release_date"),
        "overview": raw.get("overview") or "",
        "runtime": raw.get("runtime"),
        "poster_path": raw.get("poster_path") or "",
        "backdrop_path": raw.get("backdrop_path") or "",
    }


def get_movie_images(movie_id: int, cfg: dict | None = None) -> dict[str, list[dict]]:
    """返回电影 poster/backdrop/logo 图片候选，不在此处自动选择。"""
    cfg = cfg or load_config()
    session, limiter = _session(cfg), _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/movie/{movie_id}/images", cfg, f"movie_{movie_id}_images",
                    session, limiter, params={})
    return {"posters": _image_candidates(raw, "posters"),
            "backdrops": _image_candidates(raw, "backdrops"),
            "logos": _image_candidates(raw, "logos")}


def get_tv_detail(tv_id: int, cfg: dict | None = None) -> dict:
    """取剧集详情（含 seasons 列表，用来确认有哪些 season）。"""
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/tv/{tv_id}", cfg, f"tv_{tv_id}",
                    session, limiter, params={"language": "zh-CN"})
    seasons = []
    for s in (raw.get("seasons") or []):
        seasons.append({
            "season_number": s.get("season_number"),
            "name": s.get("name"),
            "episode_count": s.get("episode_count"),
        })
    return {
        "id": tv_id,
        "name": raw.get("name"),
        "original_name": raw.get("original_name"),
        "first_air_date": raw.get("first_air_date"),
        "seasons": seasons,
    }


def get_season_episodes(tv_id: int, season_number: int,
                        cfg: dict | None = None) -> list[dict]:
    """取某季的全部分集（含封面 still）。

    返回每项：
      {episode_number, name, overview, air_date, still_path, still_url, runtime}
    对不存在的季仍抛出 HTTPError；调用方若要探测可选 Season 0，使用
    :func:`get_optional_season_episodes`，不要把普通请求错误静默吞掉。
    """
    cfg = cfg or load_config()
    session = _session(cfg)
    limiter = _get_limiter(cfg)
    raw = _get_json(f"{API_BASE}/tv/{tv_id}/season/{season_number}", cfg,
                    f"tv_{tv_id}_season_{season_number}",
                    session, limiter, params={"language": "zh-CN"})
    out: list[dict] = []
    for ep in (raw.get("episodes") or []):
        still = ep.get("still_path") or ""
        out.append({
            "episode_number": ep.get("episode_number"),
            "name": ep.get("name"),
            "overview": ep.get("overview") or "",
            "air_date": ep.get("air_date") or "",
            "still_path": still,
            "still_url": f"{IMG_BASE}{still}" if still else "",
            "runtime": ep.get("runtime"),
        })
    return out


def get_optional_season_episodes(tv_id: int, season_number: int,
                                 cfg: dict | None = None) -> list[dict]:
    """读取可选季度；服务端明确返回 404 时按「无该季度」返回空表。

    只用于 Season 0 等可不存在的季度。网络超时、401/403、429、5xx 等
    其它错误继续抛出，避免把认证、限流或服务故障误判为没有特殊集。
    """
    try:
        return get_season_episodes(tv_id, season_number, cfg)
    except HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return []
        raise


def get_all_episode_stills(tv_id: int, cfg: dict | None = None) -> dict:
    """取一部剧所有季的分集封面。

    返回 {season_number: [episode dict, ...]}，每集含 still_url。
    """
    cfg = cfg or load_config()
    detail = get_tv_detail(tv_id, cfg)
    result: dict = {}
    for s in detail.get("seasons", []):
        sn = s["season_number"]
        eps = get_season_episodes(tv_id, sn, cfg)
        if eps:
            result[sn] = eps
    return result


def run_specials_cli(tv_id: int, main_season: int | None = None,
                     cfg: dict | None = None) -> dict:
    """TV 的 Specials 三段海报选择：取正式季海报池与 Season 0 池后调用
    ``select_specials_poster``。单正式季时按 §6 把季池与系列池合并为主池。
    只读 TMDB 候选并复用模块缓存/限流；不修改媒体或 plan。
    """
    cfg = cfg or load_config()
    detail = get_tv_detail(tv_id, cfg)
    seasons = sorted(
        (s["season_number"] for s in (detail.get("seasons") or [])
         if isinstance(s, dict) and isinstance(s.get("season_number"), int)
         and s["season_number"] != 0)
    )
    if not seasons:
        raise ValueError(f"TV {tv_id} 没有正式季，无法选择 Specials 海报")
    target = main_season or seasons[0]
    if target not in seasons:
        raise ValueError(f"--main-season {target} 不在正式季列表 {seasons} 中")
    main_pool = get_season_images(tv_id, target, cfg)["posters"]
    if len(seasons) == 1:
        main_pool = main_pool + get_tv_images(tv_id, cfg)["posters"]
    main_pool = rank_image_candidates(main_pool)
    if not main_pool:
        raise ValueError(f"TV {tv_id} 主海报候选池为空")
    reference_main_posters = []
    for season_number in seasons:
        ranked = rank_image_candidates(
            get_season_images(tv_id, season_number, cfg)["posters"])
        if ranked:
            reference_main_posters.append(ranked[0])
    season_zero = get_optional_season_images(tv_id, 0, cfg)["posters"]
    return select_specials_poster(
        main_poster=main_pool[0],
        main_poster_candidates=main_pool,
        season_zero_poster_candidates=season_zero,
        reference_main_posters=reference_main_posters,
        fallback_specials_poster=None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="*", help="title keyword, or tv_id [season]")
    parser.add_argument("--movie", action="store_true",
                        help="按电影搜索关键词（search_movie）")
    parser.add_argument("--specials", type=int, metavar="TV_ID",
                        help="运行 TV 的 Specials 三段海报选择并打印结果")
    parser.add_argument("--main-season", type=int, metavar="N",
                        help="Specials 选择使用的主正式季号；默认取第一个非 0 季")
    parser.add_argument("--json", metavar="FILE",
                        help="把 --specials 选择结果写入本机 JSON 文件")
    parsed = parser.parse_args(argv)
    if parsed.specials is not None:
        result = run_specials_cli(parsed.specials, parsed.main_season)
        selection = result.get("selection")
        print(f"Specials 海报选择: {selection}")
        candidate = result.get("candidate")
        if candidate:
            print(f"  候选: {candidate.get('file_path')}  "
                  f"{candidate.get('width')}x{candidate.get('height')}  "
                  f"lang={candidate.get('language') or '-'}  "
                  f"votes={candidate.get('vote_count')}")
            print(f"  url: {candidate.get('url')}")
        else:
            print("  无独立候选；省略 Specials/poster.jpg")
        if parsed.json:
            atomic_write_json(parsed.json, result)
            print(f"选择结果: {parsed.json}")
        return 0
    query = parsed.query
    if not query:
        parser.error("需要搜索关键词，或使用 --specials TV_ID")
    if parsed.movie:
        kw = " ".join(query)
        for r in search_movie(kw):
            print(f"  [{r['id']}] {r['title']} / {r['original_title']}  "
                  f"{r.get('release_date','')}")
        return 0
    if query[0].isdigit():
        tv_id = int(query[0])
        if len(query) > 1 and query[1].lstrip("-").isdigit():
            sn = int(query[1])
            eps = get_season_episodes(tv_id, sn)
            for e in eps:
                still = "✓" if e["still_url"] else "✗"
                print(f"  E{e['episode_number']:>2}  {still}  {e['name']}")
        else:
            d = get_tv_detail(tv_id)
            print(f"[{d['id']}] {d['name']} / {d['original_name']}")
            for s in d["seasons"]:
                print(f"  Season {s['season_number']}: {s['episode_count']} eps - {s['name']}")
    else:
        kw = " ".join(query)
        for r in search_tv(kw):
            print(f"  [{r['id']}] {r['name']} / {r['original_name']}  {r.get('first_air_date','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

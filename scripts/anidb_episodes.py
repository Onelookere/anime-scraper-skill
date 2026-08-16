"""AniDB 分集枚举:按 aid 取完整分集(含 OP/ED/特典等特殊集),供 match / agent 使用。

使用 HTTP anime XML(`request=anime`)一次返回整份 `<episodes>`(正片+特殊集)。
需在 config 的 `anidb.http` 配一个已注册且可用的 HTTP client，否则明确失败。

结果缓存到 `cache/anidb/episodes_{aid}.json`。
网络路由由外部环境负责,本模块不设任何代理开关。

统一分集结构(每项):
  {eid, aid, epno, type_id, type(normal/special/credit/trailer/parody/other),
   length(分钟|None), title, title_romaji, title_kanji, airdate('YYYY-MM-DD'|None)}
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import xml.etree.ElementTree as ET

import requests

from _common import RateLimiter, atomic_write_json, cache_dir, load_config

HTTP_API_URL = "http://api.anidb.net:9001/httpapi"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
EPISODE_TYPES = {
    1: "normal",
    2: "special",
    3: "credit",
    4: "trailer",
    5: "parody",
    6: "other",
}

# HTTP 路径的模块级共享限流器。
_LIMITER: RateLimiter | None = None


def _get_limiter(cfg: dict) -> RateLimiter:
    global _LIMITER
    if _LIMITER is None:
        _LIMITER = RateLimiter.from_config(cfg)
    return _LIMITER


# ── 对外主入口 ──────────────────────────────────────────────────

def get_episodes(aid: int, cfg: dict | None = None) -> list[dict]:
    """取 aid 的完整分集列表，优先缓存，未命中时使用 HTTP API。"""
    cfg = cfg or load_config()
    cache = cache_dir(cfg, "anidb")
    cached = cache / f"episodes_{aid}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    if not _http_available(cfg):
        raise RuntimeError(
            "AniDB HTTP API 未配置：请填写已注册的 client 和 clientver。"
        )

    eps = _fetch_http(aid, cfg)
    print(f"  [AniDB HTTP] aid={aid} 取到 {len(eps)} 集", file=sys.stderr)

    # 只在成功拿到数据时缓存(空结果不缓存,便于下次重试)
    if eps:
        atomic_write_json(cached, eps)
    return eps


def _http_available(cfg: dict) -> bool:
    """配了 client 名即启用，避免拿空 client 白打（会 302）。"""
    http = (cfg.get("anidb", {}) or {}).get("http", {}) or {}
    return bool(str(http.get("client") or "").strip())


def _fetch_http(aid: int, cfg: dict) -> list[dict]:
    http = cfg["anidb"]["http"]
    params = {
        "request": "anime",
        "aid": aid,
        "client": http["client"],
        "clientver": http.get("clientver", 1),
        "protover": 1,
    }
    headers = {
        # 文档未强制 UA(身份靠 client 参数);带真实 UA 对某些代理/网关更兼容。
        "User-Agent": f"{params['client']}/{params['clientver']} anime-scraper",
        "Accept-Encoding": "gzip",   # 文档:内容 gzip 压缩,下方按魔数解压
    }
    # 首次 + 对 5xx 网关抖动再试一次；两次都失败则明确抛错。
    # 重试间隔由限流器保证。
    resp = None
    for _attempt in range(2):
        _get_limiter(cfg).wait()
        resp = requests.get(HTTP_API_URL, params=params, headers=headers, timeout=30)
        if resp.status_code < 500:
            break
    resp.raise_for_status()
    data = resp.content
    if data[:2] == b"\x1f\x8b":            # 兜底解 gzip
        data = gzip.decompress(data)
    if b"<error" in data[:200].lower():
        raise RuntimeError(data[:200].decode("utf-8", "replace"))

    root = ET.fromstring(data)
    out: list[dict] = []
    for ep in root.findall("./episodes/episode"):
        e = ep.find("epno")
        epno = e.text if e is not None else ""
        type_id = int(e.get("type")) if e is not None and e.get("type") else 1
        length_el = ep.find("length")
        air_el = ep.find("airdate")
        out.append({
            "eid": int(ep.get("id")) if ep.get("id") and ep.get("id").isdigit() else None,
            "aid": aid,
            "epno": epno,
            "type_id": type_id,
            "type": EPISODE_TYPES.get(type_id, "other"),
            "length": int(length_el.text) if length_el is not None and length_el.text else None,
            "title": _pick_http_title(ep),
            "title_romaji": "",
            "title_kanji": "",
            "airdate": air_el.text if air_el is not None else None,
        })
    return _sort_eps(out)


def _pick_http_title(ep: ET.Element) -> str:
    """HTTP XML 集标题兜底:日→罗马音→英,仅作 fallback(文字最终以 Bangumi 为准)。"""
    titles = {t.get(XML_LANG): t.text for t in ep.findall("title") if t.text}
    for lang in ("ja", "x-jat", "en"):
        if titles.get(lang):
            return titles[lang]
    return next(iter(titles.values()), "")


# ── 公用 ───────────────────────────────────────────────────────

def _sort_eps(eps: list[dict]) -> list[dict]:
    """排序:正片在前按集号,特殊集按类型再按序号。"""
    def key(e: dict):
        t = e.get("type_id", 1)
        digits = "".join(c for c in (e.get("epno") or "") if c.isdigit())
        n = int(digits) if digits else 0
        return (t != 1, t, n)
    return sorted(eps, key=key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aid", nargs="?", type=int, default=1, help="AniDB anime ID")
    args = parser.parse_args(argv)
    aid = args.aid
    print(f"枚举 aid={aid} ...", file=sys.stderr)
    data = get_episodes(aid)
    print(f"共 {len(data)} 集:", file=sys.stderr)
    for e in data:
        print(f"  {e['epno']:>4}  {e['type']:<8}  {str(e['length']):>3}min  {e['title']}",
              file=sys.stderr)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""AniDB 静态标题库(anime-titles.xml.gz):按番名解析 aid——免 client、免账号、免限流。

AniDB 提供全量 aid↔标题 的静态导出。本模块把它下载并**持久缓存**到
`cache/anidb/anime-titles.xml.gz`,之后按名搜 aid 全走本地;**只有缓存缺失或过期
(默认 >7 天)才重新联网下载**(AniDB 要求该文件每天最多下一次)。

→ 任何会话调用都会自动复用本地缓存,不需要"记得"它存在。

用法(自测):python anidb_titles.py gridman
"""
from __future__ import annotations

import argparse
import gzip
import re
import sys
import time
from pathlib import Path

import requests

from _common import atomic_write_bytes, cache_dir, load_config

TITLES_URL = "http://api.anidb.net/api/anime-titles.xml.gz"
MAX_AGE_DAYS = 7          # 缓存超过这么久才重新下载(标题库更新不频繁)

_XML: str | None = None   # 进程内 memo,避免重复解压


def _dump_path(cfg: dict) -> Path:
    return cache_dir(cfg, "anidb") / "anime-titles.xml.gz"


def ensure_dump(cfg: dict, force: bool = False) -> Path:
    """确保本地有(且不过期)标题库;缺失/过期才下载。返回其路径。"""
    p = _dump_path(cfg)
    if not force and p.exists():
        age_days = (time.time() - p.stat().st_mtime) / 86400
        if age_days < MAX_AGE_DAYS:
            return p                                    # 本地已有且新鲜 → 直接用
    print(f"  [anidb_titles] 下载标题库(缺失或超 {MAX_AGE_DAYS} 天)...", file=sys.stderr)
    ua = (cfg.get("bangumi", {}) or {}).get("user_agent") or "anime-scraper/1.0"
    r = requests.get(TITLES_URL, headers={"User-Agent": ua}, timeout=60)
    r.raise_for_status()
    atomic_write_bytes(p, r.content)
    print(f"  [anidb_titles] 已缓存 {len(r.content)} 字节 -> {p}", file=sys.stderr)
    return p


def _load_xml(cfg: dict) -> str:
    global _XML
    if _XML is None:
        _XML = gzip.decompress(ensure_dump(cfg).read_bytes()).decode("utf-8", "replace")
    return _XML


def search(name: str, cfg: dict | None = None, limit: int = 10) -> list[dict]:
    """按名搜候选番剧(子串、大小写不敏感)。返回 [{aid, main, titles}]。

    aname 精确匹配很挑(大小写/写法),所以这里用子串宽松搜,由 agent 再判定。
    """
    cfg = cfg or load_config()
    kw = name.lower().strip()
    out: list[dict] = []
    for aid, body in re.findall(r'<anime aid="(\d+)">(.*?)</anime>', _load_xml(cfg), re.S):
        titles = re.findall(r'<title[^>]*>([^<]*)</title>', body)
        if any(kw in t.lower() for t in titles):
            main = re.search(r'type="main"[^>]*>([^<]*)<', body)
            out.append({
                "aid": int(aid),
                "main": main.group(1) if main else (titles[0] if titles else ""),
                "titles": titles[:6],
            })
            if len(out) >= limit:
                break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", nargs="*", help="title keyword (default: gridman)")
    args = parser.parse_args(argv)
    kw = " ".join(args.keyword) or "gridman"
    print(f"搜: {kw}", file=sys.stderr)
    for r in search(kw):
        print(f"  aid={r['aid']:>6}  {r['main']}   | {' / '.join(r['titles'][:4])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

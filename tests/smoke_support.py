"""Shared imports and helpers for the offline test suites."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_CONFIG_PATH = ROOT / "tests" / "fixtures" / "config.test.json"
sys.path.insert(0, str(ROOT / "scripts"))

import identify  # noqa: E402
import bangumi  # noqa: E402
import bangumi_search  # noqa: E402
import metadata_snapshot  # noqa: E402
import plan_scaffold  # noqa: E402
import match  # noqa: E402
import nfo  # noqa: E402
import link_library  # noqa: E402
import scrape  # noqa: E402
import images  # noqa: E402
import artwork_review  # noqa: E402
import artwork_cache  # noqa: E402
import update_artwork  # noqa: E402
import tmdb  # noqa: E402
import bootstrap  # noqa: E402
import _common  # noqa: E402
import anidb_episodes  # noqa: E402


def _write_test_image(p: Path, *, color: tuple[int, int, int] = (80, 120, 160)) -> Path:
    """写入最小但可由 Pillow 完整解码的图片测试夹具。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    image_formats = {
        ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
    }
    with artwork_review.Image.new("RGB", (2, 2), color) as image:
        image.save(p, format=image_formats[p.suffix.lower()])
    return p


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        return _write_test_image(p)
    else:
        p.write_bytes(b"")
    return p


def load_test_config() -> dict:
    """读取仓库内的无凭据测试配置 fixture。"""
    return json.loads(TEST_CONFIG_PATH.read_text(encoding="utf-8"))


def write_test_config(path: Path, config: dict | None = None) -> Path:
    """把测试 fixture 写入临时配置根，不触碰真实 config.json。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = load_test_config() if config is None else config
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


@contextlib.contextmanager
def config_root(root: Path):
    """将测试配置根临时切换到指定目录，并自动播种测试 fixture。"""
    original_root = _common.ROOT
    root.mkdir(parents=True, exist_ok=True)
    config_file = root / "config.json"
    if not config_file.exists():
        write_test_config(config_file)
    _common.ROOT = root
    try:
        yield root
    finally:
        _common.ROOT = original_root


def _episode_xml(**overrides):
    """构造单集并返回 (dataclass, XML root)，便于检查节点是否存在。"""
    values = {
        "category": "special", "season": 0, "episode": 1,
        "title": "测试 SP", "plot": "测试简介", "runtime": 2,
        "anidb_epno": "S1", "tmdb_match_status": "unknown",
    }
    values.update(overrides)
    ep = match.MergedEpisode(**values)
    return ep, ET.fromstring(nfo.build_episode_nfo(ep))


def _image_candidate(path: str, *, language: str = "", votes: int = 0,
                     rating: float = 0, width: int = 1000, height: int = 1500) -> dict:
    """构造已归一化的 TMDB 图片候选，供纯离线选图测试。"""
    return {"file_path": path, "url": f"https://image.tmdb.org/t/p/original{path}",
            "language": language, "vote_count": votes, "vote_average": rating,
            "width": width, "height": height}


__all__ = [
    "asyncio", "contextlib", "hashlib", "io", "json", "os", "re",
    "subprocess", "sys", "tempfile", "ET", "Path", "ROOT",
    "identify", "bangumi", "bangumi_search", "metadata_snapshot", "plan_scaffold", "match", "nfo", "link_library", "scrape",
    "images", "artwork_review", "artwork_cache", "update_artwork", "tmdb",
    "bootstrap", "_common", "anidb_episodes",
    "TEST_CONFIG_PATH", "load_test_config", "write_test_config",
    "_touch", "_write_test_image",
    "config_root", "_episode_xml", "_image_candidate",
]

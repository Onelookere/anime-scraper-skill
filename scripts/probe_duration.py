"""用 ffprobe 读视频时长(秒),供特殊集"文件名 + 时长"匹配时做时长校验。

- ffprobe 随 ffmpeg 一起安装;缺失时返回 None 并提示,不抛错(降级为只靠文件名)。
- 时长是"粗校验":AniDB 的 length 只精确到分钟,主判据仍是文件名关键词。

用法(自测):python probe_duration.py video1.mkv video2.mkv
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from _common import normalize_path


def probe_duration(path: str | Path) -> float | None:
    """返回视频时长(秒,float);ffprobe 缺失/文件坏/解析失败时返回 None。"""
    if shutil.which("ffprobe") is None:
        print("  [probe_duration] 未找到 ffprobe,请安装 ffmpeg 后重试", file=sys.stderr)
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", normalize_path(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  [probe_duration] {path} 失败: {e}", file=sys.stderr)
        return None
    if out.returncode != 0:
        return None
    try:
        dur = json.loads(out.stdout or "{}").get("format", {}).get("duration")
        return float(dur) if dur else None
    except (ValueError, json.JSONDecodeError):
        return None


def probe_many(paths) -> dict[str, float | None]:
    """批量:返回 {路径str: 时长秒|None}。"""
    return {str(p): probe_duration(p) for p in paths}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="*", help="video path to probe")
    args = parser.parse_args(argv)
    for a in args.path:
        d = probe_duration(a)
        mins = f"{d/60:.1f}min" if d else "N/A"
        print(f"  {mins:>8}  ({d}s)  {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

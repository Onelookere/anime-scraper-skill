"""Optional Shazam fingerprint fallback for ambiguous local OP/ED files."""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Awaitable, Callable


def extract_sample(media: Path, output: Path, *, start: float = 5,
                   seconds: float = 15, runner=subprocess.run) -> None:
    """Decode one bounded mono sample; never copy or upload the source file."""
    runner([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(start), "-i", str(media), "-t", str(seconds), "-vn",
        "-ac", "1", "-ar", "44100", str(output),
    ], check=True, capture_output=True)


async def recognize_sample(sample: Path) -> dict:
    try:
        from shazamio import Shazam
    except ImportError as exc:
        raise RuntimeError(
            "缺少可选依赖 shazamio；请在当前 Python 环境中显式安装后重试"
        ) from exc
    return await Shazam().recognize(str(sample))


def _isrc(track: dict) -> str | None:
    if track.get("isrc"):
        return str(track["isrc"])
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if str(item.get("title", "")).upper() == "ISRC" and item.get("text"):
                return str(item["text"])
    return None


def compact_result(media: Path, response: dict) -> dict:
    """Keep only identity fields; never expose Shazam's large raw response."""
    track = response.get("track") if isinstance(response, dict) else None
    if not isinstance(track, dict) or not track.get("title"):
        return {"input": str(media), "status": "unmatched"}
    share = track.get("share") if isinstance(track.get("share"), dict) else {}
    result = {
        "input": str(media),
        "status": "matched",
        "title": str(track["title"]),
        "artist": str(track.get("subtitle") or ""),
        "isrc": _isrc(track),
        "url": track.get("url") or share.get("href"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


async def fingerprint_files(
    paths: list[Path], *, start: float = 5, seconds: float = 15,
    extractor: Callable[..., None] = extract_sample,
    recognizer: Callable[[Path], Awaitable[dict]] = recognize_sample,
) -> list[dict]:
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="anime-audio-fingerprint-") as raw_tmp:
        temp = Path(raw_tmp)
        for index, media in enumerate(paths):
            if not media.is_file():
                results.append({"input": str(media), "status": "error",
                                "error": "文件不存在"})
                continue
            sample = temp / f"sample-{index}.wav"
            try:
                extractor(media, sample, start=start, seconds=seconds)
                results.append(compact_result(media, await recognizer(sample)))
            except Exception as exc:
                results.append({"input": str(media), "status": "error",
                                "error": str(exc)})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", nargs="+", type=Path)
    parser.add_argument("--start", type=float, default=5)
    parser.add_argument("--seconds", type=float, default=15)
    args = parser.parse_args(argv)
    if args.start < 0 or not 5 <= args.seconds <= 30:
        parser.error("--start 必须 >= 0；--seconds 必须为 5–30")
    results = asyncio.run(fingerprint_files(
        args.media, start=args.start, seconds=args.seconds
    ))
    print(json.dumps({"results": results}, ensure_ascii=False,
                     separators=(",", ":")))
    return 1 if any(item["status"] == "error" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""离线测试低频可选工具；默认 smoke_test 不加载本文件。"""
from __future__ import annotations

from smoke_support import *

import audio_fingerprint  # noqa: E402
import library_audit  # noqa: E402


def test_audio_fingerprint_wrapper(td: Path):
    media = _touch(td / "ending.mkv")
    sample_paths: list[Path] = []

    def fake_extract(_media, output, *, start, seconds):
        assert start == 7 and seconds == 12
        output.write_bytes(b"sample")
        sample_paths.append(output)

    async def fake_recognize(sample):
        assert sample.read_bytes() == b"sample"
        return {"track": {
            "title": "Song", "subtitle": "Artist", "url": "https://shz.am/x",
            "sections": [{"metadata": [{"title": "ISRC", "text": "JP-X"}]}],
            "raw_should_not_escape": {"large": True},
        }}

    results = asyncio.run(audio_fingerprint.fingerprint_files(
        [media], start=7, seconds=12,
        extractor=fake_extract, recognizer=fake_recognize,
    ))
    assert results == [{
        "input": str(media), "status": "matched", "title": "Song",
        "artist": "Artist", "isrc": "JP-X", "url": "https://shz.am/x",
    }]
    assert sample_paths and not sample_paths[0].exists()

    calls = []
    sample = td / "bounded.wav"

    def fake_runner(args, **kwargs):
        calls.append((args, kwargs))
        Path(args[-1]).write_bytes(b"wav")

    audio_fingerprint.extract_sample(media, sample, start=3, seconds=9, runner=fake_runner)
    args, kwargs = calls[0]
    assert args[0] == "ffmpeg" and args[args.index("-t") + 1] == "9"
    assert "-vn" in args and kwargs == {"check": True, "capture_output": True}
    assert audio_fingerprint.compact_result(media, {})["status"] == "unmatched"


def test_library_audit_health_check(td: Path):
    source = td / "source"
    library = source / "_Jellyfin"
    source_video = _touch(source / "Show" / "episode.mkv")
    source_image = _write_test_image(source / "Show" / "episode-thumb.jpg")
    flat = library / "Show (2024)" / "Season 01"
    flat.mkdir(parents=True)
    os.link(source_video, flat / "Show S01E01.mkv")
    os.link(source_image, flat / "Show S01E01-thumb.jpg")
    (flat / "Show S01E01.nfo").write_text(
        "<episodedetails><plot>ok</plot></episodedetails>", encoding="utf-8"
    )

    before = sorted(str(p.relative_to(library)) for p in library.rglob("*"))
    report = library_audit.audit(source, library)
    after = sorted(str(p.relative_to(library)) for p in library.rglob("*"))
    assert before == after
    assert report["schema_version"] == 4
    assert report["health_check_passed"], report["blocking_errors"]
    assert report["counts"]["media"] == 1 and report["counts"]["images"] == 1
    assert all(entry["hardlink_verified"] for entry in report["entries"])

    copied = library / "Copied (2024)" / "Season 01"
    copied.mkdir(parents=True)
    (copied / "Copied S01E01.mkv").write_bytes(source_video.read_bytes())
    broken = library_audit.audit(source, library)
    assert not broken["health_check_passed"]
    assert any("无法回溯为源端硬链接" in error for error in broken["blocking_errors"])


def run() -> None:
    with tempfile.TemporaryDirectory() as raw:
        td = Path(raw)
        test_audio_fingerprint_wrapper(td / "audio_fingerprint")
        test_library_audit_health_check(td / "audit")
    print("[optional] PASSED")


if __name__ == "__main__":
    run()

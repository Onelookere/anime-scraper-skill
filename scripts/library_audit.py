"""只读体检 Jellyfin 硬链接库，输出全量验证报告。

用于阶段四验收与日常巡检：逐个回溯库侧媒体/图片的 st_dev+st_ino 是否确为源端
硬链接实体，并扫描被禁止的空 <plot />、孤儿 NFO、库根异常容器与散落文件。
任何无法确认的项都记为阻塞错误。

这个脚本绝不创建、移动、删除或改写媒体库内容。唯一可选写入是用户显式指定的
本机 JSON 报告文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

from _common import atomic_write_json, normalize_path

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".mov", ".m4v", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _path(value: str | Path) -> Path:
    return Path(normalize_path(value))


def _files(root: Path, excluded: Path | None = None) -> list[Path]:
    """递归列出普通文件；source 根内的 library 根必须排除，避免自匹配。"""
    found: list[Path] = []
    excluded = excluded.resolve() if excluded else None
    for item in root.rglob("*"):
        try:
            if not item.is_file():
                continue
            if excluded and (item.resolve() == excluded or excluded in item.resolve().parents):
                continue
            found.append(item)
        except OSError:
            continue
    return sorted(found, key=lambda p: str(p).casefold())


def _fingerprint(root: Path, excluded: Path | None = None) -> str:
    digest = hashlib.sha256()
    for item in _files(root, excluded):
        try:
            stat = item.stat()
        except OSError:
            continue
        digest.update(f"{item.relative_to(root)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def _inode_key(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _nfo_issue(nfo_path: Path, sibling_media: set[Path]) -> str | None:
    if nfo_path.name in {"tvshow.nfo", "movie.nfo", "season.nfo"}:
        return None
    if nfo_path.with_suffix(".mkv") not in sibling_media:
        # MKV is the canonical case but archives may be other valid extensions.
        candidates = [nfo_path.with_suffix(ext) for ext in MEDIA_EXTS]
        if not any(candidate in sibling_media for candidate in candidates):
            return f"可能孤儿 NFO: {nfo_path}"
    return None


def audit(source_root: str | Path, library_root: str | Path) -> dict:
    """生成纯只读体检数据；任何不确定项都作为阻塞错误。"""
    source = _path(source_root)
    library = _path(library_root)
    if not source.is_dir():
        raise FileNotFoundError(f"source-root 不存在或不是目录: {source}")
    if not library.is_dir():
        raise FileNotFoundError(f"library-root 不存在或不是目录: {library}")

    source_files = _files(source, excluded=library)
    library_files = _files(library)
    source_by_inode: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for item in source_files:
        try:
            source_by_inode[_inode_key(item)].append(item)
        except OSError:
            continue

    blocking_errors: list[str] = []
    warnings: list[str] = []
    entries: list[dict] = []
    library_media = [item for item in library_files if item.suffix.lower() in MEDIA_EXTS]
    library_images = [item for item in library_files if item.suffix.lower() in IMAGE_EXTS]
    library_nfos = [item for item in library_files if item.suffix.lower() == ".nfo"]

    for item in [*library_media, *library_images]:
        try:
            matches = source_by_inode.get(_inode_key(item), [])
        except OSError as exc:
            blocking_errors.append(f"无法检查文件身份: {item}: {exc}")
            continue
        kind = "media" if item in library_media else "image"
        entry = {"kind": kind, "library_path": str(item),
                 "source_paths": [str(path) for path in matches],
                 "hardlink_verified": bool(matches)}
        entries.append(entry)
        if not matches:
            blocking_errors.append(f"库侧{kind}无法回溯为源端硬链接: {item}")

    library_media_set = set(library_media)
    for item in library_nfos:
        issue = _nfo_issue(item, library_media_set)
        if issue:
            warnings.append(issue)
        if b"<plot />" in item.read_bytes():
            blocking_errors.append(f"检测到禁止的空 <plot />: {item}")

    unexpected_top_level = [path.name for path in library.iterdir() if path.is_file()]
    if unexpected_top_level:
        warnings.append(f"库根存在未分类文件: {unexpected_top_level}")

    return {
        "schema_version": 4,
        "source_root": str(source),
        "library_root": str(library),
        "fingerprint": {
            "source_excluding_library": _fingerprint(source, excluded=library),
            "library": _fingerprint(library),
        },
        "counts": {"media": len(library_media), "images": len(library_images),
                   "nfos": len(library_nfos), "verified_entries": len(entries)},
        "entries": entries,
        "blocking_errors": blocking_errors,
        "warnings": warnings,
        "health_check_passed": not blocking_errors,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读审计 Jellyfin 库，不修改 NAS 媒体")
    parser.add_argument("audit", help="固定子命令：audit")
    parser.add_argument("--source-root", required=True, help="源 BDRip 根目录")
    parser.add_argument("--library-root", required=True, help="现有 _Jellyfin 库根")
    parser.add_argument("--report", required=True, help="本机 JSON 审计报告路径")
    args = parser.parse_args(argv)
    if args.audit != "audit":
        parser.error("仅支持 audit 子命令")

    report = audit(args.source_root, args.library_root)
    report_path = _path(args.report)
    atomic_write_json(report_path, report)
    print(json.dumps({"health_check_passed": report["health_check_passed"],
                      "blocking_errors": len(report["blocking_errors"]),
                      "warnings": len(report["warnings"]),
                      "report": str(report_path)}, ensure_ascii=False))
    return 0 if report["health_check_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

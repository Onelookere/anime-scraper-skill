"""Cache original TMDB poster candidates for later human replacement."""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from _common import atomic_write_json, decode_image_size


SCHEMA = "anime-scraper-artwork-original-cache-v1"
TTL = timedelta(days=7)
MANIFEST_NAME = "manifest.json"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
CACHE_ALLOWED_LANGUAGES = frozenset({"zh", "zh-cn", "zh-tw", "ja", "en"})
_ARTWORK_ROLE_SUFFIX = re.compile(
    r"(?:\s*[-–—|/:]\s*)?"
    r"(?:主海报|季海报|海报|主图|季图|"
    r"main[-\s]?poster|specials?[-\s]?poster|poster)"
    r"(?:\s*(?:候选|candidate))?\s*$",
    re.IGNORECASE,
)
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_component(value: object, fallback: str, limit: int = 80) -> str:
    rendered = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value or ""))
    rendered = re.sub(r"\s+", " ", rendered).strip(" .-")
    return (rendered or fallback)[:limit].rstrip(" .-") or fallback


def compact_artwork_label(
        value: object, fallback: str = "Artwork", *, limit: int = 80,
) -> str:
    """Keep the identifying title while removing artwork-role boilerplate."""
    rendered = str(value or "").strip()
    rendered = _ARTWORK_ROLE_SUFFIX.sub("", rendered)
    rendered = re.sub(r"\s*[-–—|/:]\s*", " - ", rendered)
    rendered = re.sub(r"(?:\s*-\s*){2,}", " - ", rendered)
    return _safe_component(rendered, fallback, limit=limit)


def _parse_chinese_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    if not value or any(char not in _CN_DIGITS and char not in _CN_UNITS
                        for char in value):
        return None
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            continue
        unit = _CN_UNITS[char]
        if unit >= 10000:
            section = (section + number) * unit
            total += section
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    return total + section + number


def _short_work_suffix(value: str) -> str:
    if re.fullmatch(r"(?:剧场版|劇場版|电影版|電影版|电影|電影|movie)",
                    value, re.IGNORECASE):
        return "movie"
    match = re.fullmatch(
        r"第([零〇一二两三四五六七八九十百千万0-9]+)(?:季|单元)",
        value,
        re.IGNORECASE,
    )
    if match:
        number = _parse_chinese_number(match.group(1))
        return f"s{number}" if number is not None else ""
    match = re.fullmatch(r"(?:Season|S)\s*0*(\d{1,2})", value,
                         re.IGNORECASE)
    return f"s{int(match.group(1))}" if match else ""


def compact_work_label(value: object, fallback: str = "") -> str:
    """Use sN/movie for the final season or movie component of a title."""
    rendered = compact_artwork_label(value, fallback)
    if not rendered:
        return fallback
    parts = rendered.split(" - ")
    short_suffix = _short_work_suffix(parts[-1])
    if short_suffix:
        parts[-1] = short_suffix
    return compact_artwork_label(" - ".join(parts), fallback)


def artwork_file_label(
        series_name: object, work_name: object, fallback: str = "Artwork",
) -> str:
    """Build a compact, recognizable label for a cached artwork file."""
    series = compact_artwork_label(series_name, "")
    work = compact_work_label(work_name, "")
    if not work:
        return series or fallback
    if not series:
        return work
    if work.casefold() == series.casefold() or work.casefold().startswith(
            f"{series.casefold()} - "):
        return work
    return compact_artwork_label(f"{series} - {work}", fallback)


def _resolve(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _read_manifest(cache_dir: Path) -> dict:
    manifest_path = cache_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"原图缓存 manifest 不存在: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"原图缓存 manifest 无法读取: {manifest_path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError(f"原图缓存 manifest schema 非法: {manifest_path}")
    return manifest


def cleanup_expired(root: str | Path, *, now: datetime | None = None) -> dict:
    """Remove valid cache folders older than seven days.

    Only folders with a valid manifest and creation timestamp are eligible. An
    active or malformed folder is deliberately preserved for manual inspection.
    """
    root_path = _resolve(root)
    root_path.mkdir(parents=True, exist_ok=True)
    current = now or _utc_now()
    removed: list[str] = []
    preserved = 0
    for child in root_path.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        if (child / ".active").exists():
            preserved += 1
            continue
        try:
            manifest = _read_manifest(child)
        except (OSError, ValueError):
            preserved += 1
            continue
        if manifest.get("status") == "active":
            preserved += 1
            continue
        created = _parse_time(manifest.get("created_at"))
        if created is None or current - created <= TTL:
            preserved += 1
            continue
        if child.parent != root_path or not _inside(child, root_path):
            preserved += 1
            continue
        shutil.rmtree(child)
        removed.append(str(child))
    return {"removed": removed, "preserved": preserved}


def create_review_dir(root: str | Path, label: str) -> Path:
    """Clean expired folders and create one folder for this artwork review."""
    root_path = _resolve(root)
    cleanup_expired(root_path)
    root_path.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    base = f"{_safe_component(label, 'artwork')}-{stamp}"
    candidate = root_path / base
    index = 2
    while candidate.exists():
        candidate = root_path / f"{base}-{index:02d}"
        index += 1
    candidate.mkdir(parents=True)
    (candidate / ".active").write_text("active\n", encoding="ascii")
    return candidate


def _image_suffix(candidate: dict) -> str:
    for value in (candidate.get("file_path"), candidate.get("url")):
        suffix = Path(str(value or "").split("?", 1)[0]).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def _candidate_name(label: str, candidate_id: str, suffix: str, current: bool) -> str:
    marker = " - CURRENT" if current else ""
    return f"{_safe_component(label, 'Artwork')} - {candidate_id}{marker}{suffix}"


def _payload_and_size(
        payload: tuple[bytes, tuple[int, int]],
) -> tuple[bytes, tuple[int, int]]:
    """Validate the downloader payload and its decoded dimensions."""
    if (not isinstance(payload, tuple) or len(payload) != 2
            or not isinstance(payload[0], bytes)):
        raise TypeError("图片 loader 必须返回 (bytes, size)")
    data, size = payload
    if (not isinstance(size, tuple) or len(size) != 2
            or any(type(value) is not int or value <= 0 for value in size)):
        raise ValueError("图片 loader 返回的尺寸非法")
    return data, size


def _write_image(
        cache_dir: Path, path: Path,
        payload: tuple[bytes, tuple[int, int]],
) -> tuple[int, int]:
    payload, size = _payload_and_size(payload)
    temporary = cache_dir / f".{path.name}.{uuid.uuid4().hex}.part"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return size


def _candidate_record(candidate: dict, cache_path: Path | None = None) -> dict:
    fields = {
        "candidate_id": candidate.get("candidate_id"),
        "file_path": candidate.get("file_path"),
        "url": candidate.get("url"),
        "language": candidate.get("language") or "",
        "vote_count": candidate.get("vote_count") or 0,
        "vote_average": candidate.get("vote_average") or 0,
        "width": candidate.get("width") or 0,
        "height": candidate.get("height") or 0,
    }
    if cache_path is not None:
        fields["cache_path"] = str(cache_path)
    return fields


def _cache_candidate_allowed(candidate: dict) -> bool:
    """Keep only voted Chinese/Japanese/English poster candidates."""
    language = str(candidate.get("language") or "").strip().casefold()
    if language not in CACHE_ALLOWED_LANGUAGES:
        return False
    try:
        vote_count = int(candidate.get("vote_count") or 0)
    except (TypeError, ValueError):
        return False
    return vote_count > 0


def cache_originals(
        cache_dir: str | Path, groups: list[dict], *,
        loader: Callable[[dict], tuple[bytes, tuple[int, int]]],
        max_workers: int = 6,
        series_name: str | None = None,
) -> dict:
    """Best-effort cache of voted Chinese/Japanese/English candidates.

    Candidates are deduplicated only by TMDB ``file_path``. The caller supplies
    the complete cache pool through ``cache_candidates``; the visual-review pool
    may be smaller and is never used to impose a cache limit.
    """
    output = _resolve(cache_dir)
    output.mkdir(parents=True, exist_ok=True)
    created_at = _utc_now()
    series_label = compact_artwork_label(series_name, "") if series_name else ""
    if not series_label:
        raise ValueError("原图缓存必须提供 series_name")
    unique: dict[str, dict] = {}
    group_records: list[dict] = []
    current_paths: set[str] = set()
    for group in groups:
        group_id = str(group.get("group_id") or "")
        label = str(group.get("label") or group_id or "Artwork")
        work_name = str(group.get("work_name") or label)
        file_label = artwork_file_label(series_label, work_name)
        current_id = str(group.get("current_candidate_id") or "")
        group_candidates = []
        eligible_candidate_ids: set[str] = set()
        if "cache_candidates" not in group:
            raise ValueError(f"缓存组 {group_id} 缺少 cache_candidates")
        source_candidates = group["cache_candidates"]
        if not isinstance(source_candidates, list):
            raise ValueError(f"缓存组 {group_id} 的 cache_candidates 必须是列表")
        for candidate in source_candidates:
            if not _cache_candidate_allowed(candidate):
                continue
            candidate_id = str(candidate.get("candidate_id") or "")
            file_path = str(candidate.get("file_path") or "")
            if not candidate_id or not file_path:
                continue
            eligible_candidate_ids.add(candidate_id)
            key = file_path
            if key not in unique:
                suffix = _image_suffix(candidate)
                unique[key] = {
                    "candidate": candidate,
                    "label": file_label,
                    "candidate_id": candidate_id,
                    "suffix": suffix,
                    "cache_path": None,
                    "error": None,
                }
            group_candidates.append(_candidate_record(candidate))
            if candidate_id == current_id:
                current_paths.add(key)
        current_id = current_id if current_id in eligible_candidate_ids else ""
        group_records.append({
            "group_id": group_id,
            "review_group_id": group.get("review_group_id"),
            "label": label,
            "work_name": work_name,
            "file_label": file_label,
            "current_candidate_id": current_id or None,
            "candidates": group_candidates,
        })

    manifest = {
        "schema": SCHEMA,
        "status": "active",
        "created_at": _iso(created_at),
        "cache_dir": str(output),
        "series_name": series_label,
        "groups": group_records,
        "files": [],
        "errors": [],
    }
    atomic_write_json(output / MANIFEST_NAME, manifest)

    def cache_one(item: dict) -> tuple[Path, tuple[int, int]]:
        """Land one candidate on disk, reusing an intact file when present."""
        path = output / _candidate_name(
            item["label"], item["candidate_id"], item["suffix"],
            item["candidate"]["file_path"] in current_paths,
        )
        if path.is_file():
            return path, decode_image_size(path)
        return path, _write_image(output, path, loader(item["candidate"]))

    jobs = list(unique.values())
    worker_count = max(1, min(int(max_workers or 1), len(jobs) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(cache_one, item): item for item in jobs}
        for future in as_completed(futures):
            item = futures[future]
            try:
                item["cache_path"], item["size"] = future.result()
            except Exception as exc:  # Best effort: keep the scrape moving.
                item["error"] = str(exc)

    files = []
    for key, item in unique.items():
        candidate = item["candidate"]
        if item["cache_path"] is None:
            manifest["errors"].append({
                "file_path": key,
                "url": candidate.get("url"),
                "error": item["error"] or "原图缓存失败",
            })
            continue
        files.append({
            "file_path": key,
            "url": candidate.get("url"),
            "cache_path": str(item["cache_path"]),
            "candidate_ids": [
                entry["candidate_id"]
                for group in group_records
                for entry in group["candidates"]
                if entry.get("file_path") == key
            ],
            "width": item["size"][0],
            "height": item["size"][1],
        })

    for group in group_records:
        for entry in group["candidates"]:
            item = unique.get(entry.get("file_path"))
            if item and item["cache_path"] is not None:
                entry["cache_path"] = str(item["cache_path"])
                entry["cached"] = True
            else:
                entry["cached"] = False

    manifest["files"] = files
    manifest["status"] = "completed" if not manifest["errors"] else "partial"
    manifest["completed_at"] = _iso(_utc_now())
    atomic_write_json(output / MANIFEST_NAME, manifest)
    (output / ".active").unlink(missing_ok=True)
    return manifest


def _candidate_entries(manifest: dict):
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for candidate in group.get("candidates") or []:
            if isinstance(candidate, dict):
                yield group, candidate


def resolve_candidate(
        cache_dir: str | Path | None = None, *,
        candidate_id: str | None = None, cache_path: str | Path | None = None,
) -> dict:
    """Resolve a cached candidate by ID first, then by its cached path."""
    fallback = _resolve(cache_path) if cache_path else None
    directory = _resolve(cache_dir) if cache_dir else (fallback.parent if fallback else None)
    if directory is None:
        raise ValueError("缓存替换必须提供 candidate_id 或 cache_path")
    manifest = _read_manifest(directory)
    selected = None
    if candidate_id:
        matches = [
            candidate
            for _group, candidate in _candidate_entries(manifest)
            if candidate.get("candidate_id") == candidate_id
        ]
        if len({
            str(candidate.get("cache_path") or "")
            for candidate in matches
        }) > 1:
            raise ValueError(f"缓存候选编号不唯一，需改用明确 cache_path: {candidate_id}")
        if matches:
            selected = matches[0]
    if selected is None and fallback is not None:
        for _group, candidate in _candidate_entries(manifest):
            value = candidate.get("cache_path") or ""
            if value and _resolve(value) == fallback:
                selected = candidate
                break
    if selected is None:
        raise ValueError(f"缓存候选不存在: {candidate_id or cache_path}")
    selected_path = _resolve(str(selected.get("cache_path") or ""))
    if not _inside(selected_path, directory) or not selected_path.is_file():
        raise FileNotFoundError(f"缓存候选文件不存在: {selected_path}")
    width, height = decode_image_size(selected_path)
    result = dict(selected)
    result.update({"cache_path": selected_path, "width": width, "height": height})
    result["cache_dir"] = directory
    return result


def _validate_current_markers(manifest: dict, directory: Path) -> None:
    missing: list[str] = []
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        selected_id = str(group.get("current_candidate_id") or "")
        if not selected_id:
            continue
        matches = [
            candidate
            for candidate in group.get("candidates") or []
            if candidate.get("candidate_id") == selected_id
        ]
        if len(matches) != 1:
            missing.append(f"{group.get('group_id')}: {selected_id}")
            continue
        value = matches[0].get("cache_path")
        path = _resolve(value) if value else None
        if (path is None or not _inside(path, directory) or not path.is_file()
                or " - CURRENT" not in path.stem):
            missing.append(f"{group.get('group_id')}: {selected_id}")
    if missing:
        raise ValueError("选中海报缺少 CURRENT 缓存标记: " + ", ".join(missing))


def find_alias_in_group(
        cache_dir: str | Path, *, group_id: str,
        cache_path: str | Path,
) -> str | None:
    """Return the candidate_id in `group_id` backed by the same cached file."""
    directory = _resolve(cache_dir)
    manifest = _read_manifest(directory)
    target = _resolve(cache_path)
    for group in manifest.get("groups") or []:
        if not isinstance(group, dict):
            continue
        if str(group.get("group_id") or "") != str(group_id):
            continue
        for candidate in group.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            value = candidate.get("cache_path")
            if value and _resolve(value) == target:
                alias = str(candidate.get("candidate_id") or "")
                return alias or None
    return None


def sync_current_markers(
        cache_dir: str | Path, updates: list[dict], *,
        fallback_paths: list[str | Path] | None = None,
) -> dict:
    """Mark the installed candidate(s) as CURRENT without changing creation time."""
    directory = _resolve(cache_dir)
    manifest = _read_manifest(directory)
    selected_by_group: dict[str, str] = {}
    fallback = [_resolve(path) for path in (fallback_paths or [])]
    for update in updates:
        candidate_id = str(update.get("candidate_id") or "")
        cache_path = update.get("cache_path")
        if cache_path:
            fallback.append(_resolve(cache_path))
        match = None
        for group, candidate in _candidate_entries(manifest):
            if candidate_id and candidate.get("candidate_id") == candidate_id:
                match = (group, candidate)
                break
        if match is None and fallback:
            path = fallback[-1]
            for group, candidate in _candidate_entries(manifest):
                if candidate.get("cache_path") and _resolve(candidate["cache_path"]) == path:
                    match = (group, candidate)
                    break
        if match is not None:
            group, candidate = match
            selected_by_group[str(group.get("group_id") or "")] = str(
                candidate.get("candidate_id") or ""
            )

    current_paths: set[Path] = set()
    for group, candidate in _candidate_entries(manifest):
        group_id = str(group.get("group_id") or "")
        selected_id = selected_by_group.get(group_id)
        if selected_id:
            group["current_candidate_id"] = selected_id
        if candidate.get("candidate_id") == group.get("current_candidate_id"):
            value = candidate.get("cache_path")
            if value:
                current_paths.add(_resolve(value))

    aliases_by_path: dict[Path, list[dict]] = {}
    for _group, candidate in _candidate_entries(manifest):
        value = candidate.get("cache_path")
        if value:
            aliases_by_path.setdefault(_resolve(value), []).append(candidate)

    # Snapshot aliases before renaming. Shared TMDB file_paths may appear in
    # multiple groups; iterating the live manifest would otherwise rename the
    # same physical file back when the second alias is reached.
    for old_path, aliases in aliases_by_path.items():
        if not old_path.is_file():
            continue
        stem = old_path.stem.replace(" - CURRENT", "")
        is_current = old_path in current_paths
        target = old_path.with_name(f"{stem} - CURRENT{old_path.suffix}") \
            if is_current else old_path.with_name(f"{stem}{old_path.suffix}")
        if target != old_path:
            os.replace(old_path, target)
        for candidate in aliases:
            candidate["cache_path"] = str(target)
    _validate_current_markers(manifest, directory)
    manifest["updated_at"] = _iso(_utc_now())
    atomic_write_json(directory / MANIFEST_NAME, manifest)
    return manifest

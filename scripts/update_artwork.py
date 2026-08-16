"""按增量 change set 原子替换或撤销已建库图片及其硬链接。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from pathlib import PurePosixPath
from typing import Callable

import artwork_cache
import images
from _common import atomic_write_json, decode_image_size, normalize_path

SCHEMA = "anime-scraper-artwork-update-v1"
_SPECIALS_SELECTIONS = {
    "season_zero", "main_pool_alternative", "series_specials_reuse",
}


def _normalize_candidate_id(value: str) -> str:
    """Accept compact human input such as G01C01 and return G01-C01."""
    raw = str(value or "").strip().upper()
    match = re.fullmatch(r"G(\d{1,2})-?C(\d{1,2})", raw)
    if not match:
        raise ValueError(f"候选编号必须是 Gxx-Cyy（也接受 GxxCyy）: {value}")
    return f"G{int(match.group(1)):02d}-C{int(match.group(2)):02d}"


def _safe_library_relpath(value: object) -> str:
    """Normalize a plan-relative artwork path without accepting traversal."""
    rel = str(value or "").replace("\\", "/").strip()
    parsed = PurePosixPath(rel)
    if not rel or parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError(f"非法 artwork library_relpath: {value}")
    return rel


def _normalize_cached_target(value: str) -> str:
    target_key = str(value or "").strip().lower()
    target_aliases = {
        "main": "main-poster",
        "poster": "main-poster",
        "specials": "specials-poster",
    }
    target_key = target_aliases.get(target_key, target_key)
    if target_key not in {"main-poster", "specials-poster"}:
        raise ValueError("--target 只能是 main-poster 或 specials-poster")
    return target_key


def _library_project_dir(plan: dict) -> Path | None:
    projection = plan.get("library_projection") or {}
    if not projection.get("hardlinks_enabled"):
        return None
    link_root = projection.get("link_root")
    if not link_root:
        raise ValueError("启用硬链接的 plan 缺少 library_projection.link_root")
    subject = plan.get("show") if plan.get("type") == "tv" else plan.get("movie")
    subject = subject or {}
    title = str(subject.get("title") or "").strip()
    premiered = str(subject.get("premiered") or "").strip()
    if not title:
        raise ValueError("plan 缺少作品标题，无法推导库侧目录")
    year = premiered[:4] if re.fullmatch(r"\d{4}(-\d{2}-\d{2})?", premiered) else ""
    folder = f"{title} ({year})" if year else title
    folder = "".join(char for char in folder if char not in '\\/:*?"<>|').strip().rstrip(". ")
    if not folder:
        raise ValueError("作品标题无法生成合法库侧目录")
    return _path(str(link_root)) / folder


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inode(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _candidate_size(item: dict) -> tuple[object, object]:
    """读取正式 change set 中的 TMDB 候选尺寸提示。"""
    return item.get("candidate_width"), item.get("candidate_height")


def _valid_size_pair(width: object, height: object) -> bool:
    return (
        isinstance(width, int) and not isinstance(width, bool) and width > 0
        and isinstance(height, int) and not isinstance(height, bool) and height > 0
    )


def _download(url: str, timeout: int = images.DEFAULT_DOWNLOAD_TIMEOUT) -> bytes:
    return images.download_bytes(url, timeout=timeout)


def _path(value: str) -> Path:
    return Path(normalize_path(value)).expanduser().resolve()


def _path_key(path: Path) -> str:
    """用于恢复报告匹配的规范路径键；Windows/UNC 路径大小写不敏感。"""
    return os.path.normcase(os.path.normpath(str(path)))


def _review_candidate_urls(review: dict | None) -> set[str]:
    if not isinstance(review, dict):
        return set()
    return {
        str(candidate.get("url") or "").strip()
        for group in (review.get("groups") or [])
        if isinstance(group, dict)
        for candidate in (group.get("candidates") or [])
        if isinstance(candidate, dict) and str(candidate.get("url") or "").strip()
    }


def _resolve_cached_item(item: dict) -> dict:
    """Resolve a human-selected cache entry before normal change-set checks."""
    candidate_id = str(item.get("candidate_id") or "").strip()
    cache_path = item.get("cache_path")
    if not candidate_id and not cache_path:
        return item
    resolved = artwork_cache.resolve_candidate(
        item.get("original_cache_dir"),
        candidate_id=candidate_id or None,
        cache_path=cache_path,
    )
    prepared = dict(item)
    prepared["url"] = str(resolved.get("url") or "")
    prepared["candidate_width"] = resolved.get("width")
    prepared["candidate_height"] = resolved.get("height")
    prepared["_cached_path"] = str(resolved["cache_path"])
    prepared["_resolved_candidate_id"] = str(
        resolved.get("candidate_id") or candidate_id
    )
    prepared["original_cache_dir"] = str(resolved["cache_dir"])
    return prepared


def _cached_replace_context(
        plan_path: Path, candidate_id: str, target: str,
        original_cache_dir: str | None = None,
) -> dict:
    """Build the one-item cached replacement context from an existing plan."""
    normalized_id = _normalize_candidate_id(candidate_id)
    target_key = _normalize_cached_target(target)
    if not plan_path.is_file():
        raise FileNotFoundError(f"plan 不存在: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("plan_schema") != "anime-scraper-plan":
        raise ValueError(f"plan schema 非法: {plan_path}")
    artwork = [entry for entry in (plan.get("artwork") or [])
               if isinstance(entry, dict) and entry.get("kind") == "poster"]

    def rel(entry: dict) -> str:
        return _safe_library_relpath(entry.get("library_relpath"))

    if target_key == "specials-poster":
        entries = [entry for entry in artwork if rel(entry) == "Specials/poster.jpg"]
        if len(entries) != 1:
            raise ValueError(
                f"plan 中必须恰有一个 Specials/poster.jpg artwork: {plan_path}"
            )
    else:
        roots = [entry for entry in artwork if rel(entry) == "poster.jpg"]
        if len(roots) != 1:
            raise ValueError(f"plan 中必须恰有一个根 poster.jpg artwork: {plan_path}")
        source_key = _path_key(_path(str(roots[0].get("source_path") or "")))
        entries = [
            entry for entry in artwork
            if _path_key(_path(str(entry.get("source_path") or ""))) == source_key
            and (rel(entry) == "poster.jpg"
                 or re.fullmatch(r"Season \d{2}/poster\.jpg", rel(entry)))
        ]
        if any(
            _path_key(_path(str(entry.get("source_path") or ""))) == source_key
            and rel(entry) == "Specials/poster.jpg"
            for entry in artwork
        ):
            raise ValueError("主海报与 Specials 海报不得共享源侧实体")

    source_value = str(entries[0].get("source_path") or "")
    source = _path(source_value)
    if not source.is_file():
        raise FileNotFoundError(f"源侧海报不存在: {source}")
    source_keys = {
        _path_key(_path(str(entry.get("source_path") or "")))
        for entry in entries
    }
    if source_keys != {_path_key(source)}:
        raise ValueError("同一快捷替换目标的 source_path 不一致")
    old_urls = {str(entry.get("url") or "").strip() for entry in entries}
    if len(old_urls) != 1 or not next(iter(old_urls)):
        raise ValueError("快捷海报替换要求目标 artwork 具有唯一旧 TMDB URL")
    old_url = next(iter(old_urls))
    old_methods = {
        str(entry.get("method") or "")
        for entry in entries
    }
    if old_methods != {"tmdb"}:
        raise ValueError("快捷作品海报替换只支持已有 TMDB poster")

    review = plan.get("artwork_review") or {}
    cache_dir = original_cache_dir or review.get("original_cache_dir")
    resolved = artwork_cache.resolve_candidate(
        cache_dir, candidate_id=normalized_id,
    )
    project_dir = _library_project_dir(plan)
    destinations: list[Path] = []
    if project_dir is not None:
        for entry in entries:
            destination = project_dir / _safe_library_relpath(entry.get("library_relpath"))
            if not destination.is_file():
                raise FileNotFoundError(f"库侧海报不存在: {destination}")
            destinations.append(destination)
    return {
        "plan_path": plan_path,
        "plan": plan,
        "entries": entries,
        "source": source,
        "destinations": destinations,
        "old_url": old_url,
        "old_candidate_id": str(entries[0].get("candidate_id") or ""),
        "candidate": resolved,
        "candidate_id": normalized_id,
        "target": target_key,
        "library_relpaths": [rel(entry) for entry in entries],
    }


def _direct_cached_replace_context(
        source_dir: str | Path, library_dir: str | Path,
        cache_dir: str | Path, candidate_id: str, target: str,
) -> dict:
    """Build a bounded context without reading a plan or scanning a work tree."""
    normalized_id = _normalize_candidate_id(candidate_id)
    target_key = _normalize_cached_target(target)
    source_root = _path(str(source_dir))
    library_root = _path(str(library_dir))
    cache_root = _path(str(cache_dir))
    if not source_root.is_dir():
        raise FileNotFoundError(f"源侧作品目录不存在: {source_root}")
    if not library_root.is_dir():
        raise FileNotFoundError(f"库侧作品目录不存在: {library_root}")

    if target_key == "main-poster":
        source = source_root / "poster.jpg"
        root_destination = library_root / "poster.jpg"
        if not root_destination.is_file():
            raise FileNotFoundError(f"库侧根海报不存在: {root_destination}")
        destinations = [root_destination]
        for child in sorted(library_root.iterdir(), key=lambda path: path.name.casefold()):
            if not child.is_dir() or not re.fullmatch(r"Season \d{2}", child.name):
                continue
            destination = child / "poster.jpg"
            if destination.exists() and not destination.is_file():
                raise ValueError(f"库侧季海报不是文件: {destination}")
            if destination.is_file():
                destinations.append(destination)
    else:
        source = source_root / "specials-poster.jpg"
        destination = library_root / "Specials" / "poster.jpg"
        if not destination.is_file():
            raise FileNotFoundError(f"库侧 Specials 海报不存在: {destination}")
        destinations = [destination]

    if not source.is_file():
        raise FileNotFoundError(f"源侧海报不存在: {source}")
    source_inode = _inode(source)
    source_sha256 = _sha256(source)
    for destination in destinations:
        if destination.stat().st_dev != source.stat().st_dev:
            raise OSError(f"源库跨卷，拒绝快捷海报替换: {source} -> {destination}")
        if _inode(destination) != source_inode:
            if _sha256(destination) != source_sha256:
                raise OSError(f"库侧海报既不是源图硬链接且旧内容也不一致: {destination}")

    candidate = artwork_cache.resolve_candidate(
        cache_root, candidate_id=normalized_id,
    )
    if not str(candidate.get("url") or "").strip():
        raise ValueError(f"缓存候选缺少 TMDB URL: {normalized_id}")
    return {
        "source_dir": source_root,
        "library_dir": library_root,
        "cache_dir": cache_root,
        "source": source,
        "destinations": destinations,
        "candidate": candidate,
        "candidate_id": normalized_id,
        "target": target_key,
    }


def _direct_marker_group_ids(library_root: Path, target: str) -> list[str]:
    """把快捷目标映射为 manifest 组：main → tv-show/movie + 现有季，specials → specials。"""
    if target == "specials-poster":
        return ["specials"]
    seasons: list[str] = []
    for child in library_root.iterdir():
        if not child.is_dir():
            continue
        match = re.fullmatch(r"Season (\d{2})", child.name)
        if match:
            seasons.append(f"season-{match.group(1)}")
    if (library_root / "tvshow.nfo").is_file() or seasons:
        return ["tv-show", *seasons]
    return ["movie"]


def _sync_direct_current_markers(context: dict) -> dict:
    """成功替换后把目标组的 CURRENT 标记同步为已装图片。

    无 plan 快捷模式可能跨池安装（如主池图装到 Specials）；只同步候选池中
    确实包含该图片（共享同一缓存文件的别名也算）的目标组，其余保持原状。"""
    marker: dict = {"marker_synced": False}
    group_ids = _direct_marker_group_ids(context["library_dir"], context["target"])
    pairs: list[tuple[str, str]] = []
    for group_id in group_ids:
        alias_id = artwork_cache.find_alias_in_group(
            context["cache_dir"], group_id=group_id,
            cache_path=context["candidate"]["cache_path"],
        )
        if alias_id:
            pairs.append((group_id, alias_id))
    marker["marker_groups"] = {group: alias for group, alias in pairs}
    if not pairs:
        marker["marker_note"] = (
            "目标组候选池不含该图片，CURRENT 标记保持原状: " + ", ".join(group_ids)
        )
        return marker
    try:
        artwork_cache.sync_current_markers(
            context["cache_dir"],
            [{"candidate_id": alias} for _group, alias in pairs],
        )
    except (OSError, ValueError) as exc:
        marker["marker_note"] = f"CURRENT 标记同步失败: {exc}"
        return marker
    marker["marker_synced"] = True
    try:
        current = artwork_cache.resolve_candidate(
            context["cache_dir"], candidate_id=context["candidate_id"],
        )
        marker["cache_path"] = str(current["cache_path"])
    except (OSError, ValueError):
        pass
    return marker


def _finalize_direct_report(report_value: dict, context: dict) -> dict:
    """合并 CURRENT 同步结果；同步改名后回写报告中的缓存路径。"""
    marker = _sync_direct_current_markers(context)
    report_value.update(marker)
    if marker.get("cache_path"):
        report_value["items"][0]["cache_path"] = marker["cache_path"]
    return report_value


def _cached_replace_noop(context: dict) -> dict:
    """Verify and report a request whose candidate is already installed."""
    source = context["source"]
    destinations = context["destinations"]
    actual_size = decode_image_size(source)
    source_sha256 = _sha256(source)
    source_inode = _inode(source)
    for destination in destinations:
        if destination.parent.stat().st_dev != source.stat().st_dev:
            raise OSError(f"源库跨卷，拒绝快捷海报替换: {source} -> {destination}")
        if _inode(destination) != source_inode:
            raise OSError(f"库侧海报不是源图硬链接: {destination}")
    plan = context["plan"]
    changed = False
    for entry in context["entries"]:
        if entry.get("candidate_id") != context["candidate_id"]:
            entry["candidate_id"] = context["candidate_id"]
            changed = True
    if changed:
        atomic_write_json(context["plan_path"], plan)
    try:
        artwork_cache.sync_current_markers(
            context["candidate"]["cache_dir"],
            [{"candidate_id": context["candidate_id"]}],
        )
    except (OSError, ValueError):
        pass
    try:
        candidate = artwork_cache.resolve_candidate(
            context["candidate"]["cache_dir"],
            candidate_id=context["candidate_id"],
        )
    except (OSError, ValueError):
        candidate = context["candidate"]
    item = {
        "source_path": str(source),
        "library_paths": [str(path) for path in destinations],
        "url": candidate.get("url"),
        "candidate_id": context["candidate_id"],
        "cache_path": str(candidate["cache_path"]),
        "original_cache_dir": str(candidate["cache_dir"]),
        "width": actual_size[0],
        "height": actual_size[1],
        "sha256": source_sha256,
        "hardlinks_verified": len(destinations),
        "action": "already_current",
    }
    return {
        "schema": SCHEMA,
        "status": "completed",
        "mode": "cached-replace",
        "downloads": 0,
        "ffmpeg": 0,
        "updated": 0,
        "removed": 0,
        "resumed": 1,
        "target": context["target"],
        "candidate_id": context["candidate_id"],
        "cache_path": str(candidate["cache_path"]),
        "original_cache_dir": str(candidate["cache_dir"]),
        "items": [item],
    }


def _review_resolution_class(width, height) -> str:
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return "low"
    if w >= 1000 and h >= 1500:
        return "preferred"
    if w >= 800 and h >= 1200:
        return "acceptable"
    return "low"


def _sync_plan_artwork_review(
        plan_path: Path, *, target: str, old_url: str,
        old_candidate_id: str, candidate: dict,
) -> list[str]:
    """cached-replace 后把新候选并入 plan.artwork_review。

    替换只更新 artwork 条目会让新海报 URL 游离于 review 候选池之外，
    下次 dry-run 即被“未命中 Agent 识图选择”拒绝。这里按
    “当前选中旧候选的组 > 选中旧 URL 且组名后缀匹配 > 候选含旧 URL”定位
    目标组，把新候选挪到 candidates[0]（disabled 模式的确定性选择位）并
    同步 selection；plan 无 artwork_review 时只记账跳过。
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    review = plan.get("artwork_review")
    if not isinstance(review, dict):
        return ["plan 无 artwork_review，跳过候选池回写"]
    notes: list[str] = []
    groups = review.get("groups")
    if not isinstance(groups, list):
        groups = []
        review["groups"] = groups
    selections = review.get("selections")
    if not isinstance(selections, list):
        selections = []
        review["selections"] = selections
    cid = str(candidate.get("candidate_id") or "")
    url = str(candidate.get("url") or "").strip()
    if not cid or not url:
        return ["缓存候选缺少 candidate_id/url，跳过候选池回写"]

    def selection_of(group_id) -> dict | None:
        return next(
            (item for item in selections
             if isinstance(item, dict) and item.get("group_id") == group_id),
            None,
        )

    group = None
    if old_candidate_id:
        for raw in groups:
            if not isinstance(raw, dict):
                continue
            cids = [
                str(item.get("candidate_id") or "")
                for item in (raw.get("candidates") or [])
                if isinstance(item, dict)
            ]
            sel = selection_of(raw.get("group_id"))
            if (old_candidate_id in cids and sel is not None
                    and str(sel.get("candidate_id") or "") == old_candidate_id):
                group = raw
                break
    if group is None and old_url:
        suffix = "-specials" if target == "specials-poster" else "-poster"
        for raw in groups:
            if not isinstance(raw, dict):
                continue
            sel = selection_of(raw.get("group_id"))
            if sel is None:
                continue
            selected = next(
                (item for item in (raw.get("candidates") or [])
                 if isinstance(item, dict)
                 and str(item.get("candidate_id") or "")
                 == str(sel.get("candidate_id") or "")),
                None,
            )
            if (selected is not None
                    and str(selected.get("url") or "").strip() == old_url
                    and str(raw.get("group_id") or "").endswith(suffix)):
                group = raw
                break
    if group is None and old_url:
        for raw in groups:
            if not isinstance(raw, dict):
                continue
            if any(
                isinstance(item, dict)
                and str(item.get("url") or "").strip() == old_url
                for item in (raw.get("candidates") or [])
            ):
                group = raw
                break
    if group is None:
        group = {
            "group_id": f"cached-{target}",
            "label": f"cached-replace {target}",
            "candidates": [],
        }
        groups.append(group)
        notes.append(
            f"artwork_review 无旧海报候选组，已新建 {group['group_id']}"
        )
    candidates = group.setdefault("candidates", [])
    fresh = {
        "candidate_id": cid,
        "url": url,
        "width": candidate.get("width"),
        "height": candidate.get("height"),
        "language": str(candidate.get("language") or ""),
        "resolution_class": _review_resolution_class(
            candidate.get("width"), candidate.get("height")
        ),
        "visible_text_role": None,
        "primary_title_prominence": None,
        "visual_issues": None,
    }
    index = next(
        (i for i, item in enumerate(candidates)
         if isinstance(item, dict)
         and str(item.get("candidate_id") or "") == cid),
        None,
    )
    if index is not None:
        candidates.insert(0, candidates.pop(index))
    else:
        candidates.insert(0, fresh)
        notes.append(f"{group.get('group_id')} 已并入候选 {cid}")
    group_id = str(group.get("group_id"))
    sel = selection_of(group_id)
    if sel is None:
        selections.append({
            "group_id": group_id,
            "candidate_id": cid,
            "confidence": "high",
            "reason": "人工从原图缓存指定替换（cached-replace）",
            "flags": [],
            "decision_factors": None,
        })
        notes.append(f"已为 {group_id} 补充 selection {cid}")
    else:
        sel["candidate_id"] = cid
        sel["reason"] = "人工从原图缓存指定替换（cached-replace）"
    atomic_write_json(plan_path, plan)
    return notes


def apply_cached_replace(
        plan_path: str | Path, candidate_id: str, target: str, *,
        original_cache_dir: str | None = None,
        resume_report: dict | None = None,
        loader: Callable[[str], bytes] | None = None,
) -> dict:
    """Replace one cached main/Specials poster without a change-set file."""
    context = _cached_replace_context(
        _path(str(plan_path)), candidate_id, target, original_cache_dir,
    )
    new_url = str(context["candidate"].get("url") or "")
    if new_url == context["old_url"]:
        return _cached_replace_noop(context)
    item = {
        "operation": "replace",
        "kind": "poster",
        "library_relpath": (
            "Specials/poster.jpg"
            if context["target"] == "specials-poster" else ""
        ),
        "source_path": str(context["source"]),
        "library_paths": [str(path) for path in context["destinations"]],
        "plan_path": str(context["plan_path"]),
        "old_method": "tmdb",
        "old_url": context["old_url"],
        "new_method": "tmdb",
        "url": new_url,
        "candidate_id": context["candidate_id"],
        "original_cache_dir": str(context["candidate"]["cache_dir"]),
        "candidate_width": context["candidate"].get("width"),
        "candidate_height": context["candidate"].get("height"),
        "expected_old_sha256": _sha256(context["source"]),
    }
    if context["target"] == "specials-poster":
        selection = context["entries"][0].get("specials_selection")
        if selection:
            item["specials_selection"] = selection
    result = apply_change_set(
        {
            "schema": SCHEMA,
            "mode": "incremental",
            "budget": {"downloads": 0, "ffmpeg": 0},
            "items": [item],
        },
        loader=loader,
        resume_report=resume_report,
    )
    result.update({
        "mode": "cached-replace",
        "target": context["target"],
        "candidate_id": context["candidate_id"],
        "cache_path": result.get("items", [{}])[0].get("cache_path")
        or str(context["candidate"]["cache_path"]),
        "original_cache_dir": str(context["candidate"]["cache_dir"]),
        "artwork_review_sync": _sync_plan_artwork_review(
            context["plan_path"],
            target=context["target"],
            old_url=context["old_url"],
            old_candidate_id=context.get("old_candidate_id") or "",
            candidate=context["candidate"],
        ),
    })
    return result


def apply_cached_replace_direct(
        source_dir: str | Path, library_dir: str | Path,
        cache_dir: str | Path, candidate_id: str, target: str,
) -> dict:
    """Replace one cached poster using only explicit source/library/cache paths.

    On success the CURRENT markers of the target's manifest groups are synced
    to the installed image when those pools actually contain it."""
    context = _direct_cached_replace_context(
        source_dir, library_dir, cache_dir, candidate_id, target,
    )
    source = context["source"]
    destinations = context["destinations"]
    candidate_path = Path(context["candidate"]["cache_path"])
    candidate_size = (
        context["candidate"]["width"], context["candidate"]["height"]
    )
    candidate_sha256 = _sha256(candidate_path)
    source_sha256 = _sha256(source)

    def report(*, action: str, updated: int, resumed: int, sha256: str) -> dict:
        item = {
            "source_path": str(source),
            "library_paths": [str(path) for path in destinations],
            "url": context["candidate"]["url"],
            "candidate_id": context["candidate_id"],
            "cache_path": str(candidate_path),
            "original_cache_dir": str(context["cache_dir"]),
            "width": candidate_size[0],
            "height": candidate_size[1],
            "sha256": sha256,
            "hardlinks_verified": len(destinations),
            "action": action,
        }
        return {
            "schema": SCHEMA,
            "status": "completed",
            "mode": "cached-replace-direct",
            "target": context["target"],
            "candidate_id": context["candidate_id"],
            "cache_path": str(candidate_path),
            "original_cache_dir": str(context["cache_dir"]),
            "downloads": 0,
            "ffmpeg": 0,
            "updated": updated,
            "removed": 0,
            "resumed": resumed,
            "plan_synced": False,
            "items": [item],
        }

    if source_sha256 == candidate_sha256:
        actual_size = decode_image_size(source)
        if actual_size != candidate_size:
            raise OSError(f"源侧海报尺寸与缓存候选不一致: {source}")
        return _finalize_direct_report(report(
            action="already_current", updated=0, resumed=1,
            sha256=source_sha256,
        ), context)

    token = hashlib.sha256(
        f"{context['candidate'].get('url')}|{candidate_sha256}".encode("utf-8")
    ).hexdigest()[:16]
    staging = source.with_name(f".{source.name}.cached-replace-{token}.tmp")
    try:
        with candidate_path.open("rb") as source_handle, staging.open("wb") as staging_handle:
            shutil.copyfileobj(source_handle, staging_handle)
            staging_handle.flush()
            os.fsync(staging_handle.fileno())
        actual_size = decode_image_size(staging)
        if actual_size != candidate_size:
            raise OSError(f"缓存候选尺寸校验失败: {candidate_path}")
        if _sha256(staging) != candidate_sha256:
            raise OSError(f"缓存候选复制校验失败: {candidate_path}")
        os.replace(staging, source)

        source_inode = _inode(source)
        for destination in destinations:
            temporary = destination.with_name(
                f".{destination.name}.relink-{uuid.uuid4().hex}.tmp"
            )
            try:
                os.link(source, temporary)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink(missing_ok=True)

        final_size = decode_image_size(source)
        final_sha256 = _sha256(source)
        if final_size != candidate_size or final_sha256 != candidate_sha256:
            raise OSError(f"快捷海报替换后源图验证失败: {source}")
        for destination in destinations:
            if destination.stat().st_dev != source.stat().st_dev:
                raise OSError(f"源库跨卷，拒绝快捷海报替换: {source} -> {destination}")
            if _inode(destination) != source_inode:
                raise OSError(f"快捷海报替换后硬链接验证失败: {destination}")
        return _finalize_direct_report(report(
            action="updated", updated=1, resumed=0,
            sha256=final_sha256,
        ), context)
    finally:
        staging.unlink(missing_ok=True)


def _preflight_new_item(item: dict, review: dict | None,
                        prior_by_source: dict[str, dict]) -> dict:
    """校验并准备一个此前不在 plan 中的作品级海报。"""
    source_value = str(item.get("source_path") or "")
    source = _path(source_value)
    destinations = [_path(str(value)) for value in item.get("library_paths") or []]
    plan_path = _path(str(item.get("plan_path") or ""))
    target_kind = str(item.get("kind") or "poster")
    new_method = str(item.get("new_method") or "")
    url = str(item.get("url") or "")
    candidate_width, candidate_height = _candidate_size(item)
    width, height = candidate_width, candidate_height
    cached_path = _path(str(item["_cached_path"])) if item.get("_cached_path") else None
    library_relpath = str(item.get("library_relpath") or "").replace("\\", "/")
    if not plan_path.is_file() or not source.parent.is_dir():
        raise FileNotFoundError(f"新增海报目标缺失: {source}")
    if target_kind != "poster" or new_method != "tmdb":
        raise ValueError("图片增量新增目前只允许 TMDB 作品海报")
    if not url.startswith("https://image.tmdb.org/t/p/original/"):
        raise ValueError(f"新增海报必须使用 TMDB original URL: {url}")
    if not _valid_size_pair(candidate_width, candidate_height):
        raise ValueError(f"新增海报期望尺寸非法: {source}")
    if library_relpath != "Specials/poster.jpg":
        raise ValueError("新增作品海报目前只允许写入 Specials/poster.jpg")
    selection = str(item.get("specials_selection") or "")
    if selection not in _SPECIALS_SELECTIONS:
        raise ValueError(f"新增 Specials 海报缺少合法 specials_selection: {source}")
    if not review or url not in _review_candidate_urls(review):
        if cached_path is None:
            raise ValueError(f"新增 Specials 海报 URL 未出现在 artwork review 候选池: {url}")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    artwork = plan.get("artwork") or []
    matches = [
        entry for entry in artwork
        if _path(str(entry.get("source_path") or "")) == source
        and entry.get("kind") == target_kind
        and str(entry.get("library_relpath") or "").replace("\\", "/")
        == library_relpath
    ]
    for entry in artwork:
        if entry.get("kind") != "poster":
            continue
        relpath = str(entry.get("library_relpath") or "").replace("\\", "/")
        if relpath == library_relpath:
            continue
        if (_path(str(entry.get("source_path") or "")) == source
                or str(entry.get("url") or "") == url):
            raise ValueError("新增 Specials 海报不得复用主海报实体或 URL")

    source_device = source.parent.stat().st_dev
    linked_destinations = []
    for destination in destinations:
        if not destination.parent.is_dir():
            raise FileNotFoundError(f"库侧海报目录缺失: {destination.parent}")
        if destination.parent.stat().st_dev != source_device:
            raise OSError(f"源库跨卷，拒绝新增海报: {source} -> {destination}")
        if destination.exists():
            if not source.is_file() or _inode(destination) != _inode(source):
                raise FileExistsError(f"库侧海报目标已被占用: {destination}")
            linked_destinations.append(destination)

    source_sha256 = ""
    source_size = None
    if source.is_file():
        try:
            source_size = decode_image_size(source)
        except OSError as exc:
            raise ValueError(f"新增海报源文件无法解码: {source}") from exc
        source_sha256 = _sha256(source)

    prior = prior_by_source.get(_path_key(source))
    prior_sha256 = str((prior or {}).get("sha256") or "")
    plan_is_new = bool(matches) and all(
        str(entry.get("method") or "") == new_method
        and str(entry.get("url") or "") == url
        for entry in matches
    )
    source_is_new = (
        source_size is not None
        and source_size[0] > 0 and source_size[1] > 0
        and prior_sha256 and source_sha256 == prior_sha256
    )
    if source_is_new:
        width, height = source_size
        if not plan_is_new:
            raise ValueError(f"新增海报源图已完成但 plan 未同步: {plan_path}")
        state = "completed" if len(linked_destinations) == len(destinations) else "relink"
    else:
        state = "pending"

    return {
        "operation": "add",
        "source": source,
        "source_path_value": source_value,
        "destinations": destinations,
        "plan_path": plan_path,
        "plan": plan,
        "plan_matches": matches,
        "target_kind": target_kind,
        "scope": str(item.get("scope") or "season"),
        "library_relpath": library_relpath,
        "specials_selection": selection,
        "url": url,
        "new_method": new_method,
        "fallback_video": None,
        "new_tmdb_match_status": None,
        "width": width,
        "height": height,
        "candidate_width": candidate_width,
        "candidate_height": candidate_height,
        "cached_path": cached_path,
        "original_cache_dir": item.get("original_cache_dir"),
        "candidate_id": item.get("_resolved_candidate_id") or item.get("candidate_id"),
        "state": state,
        "source_sha256": source_sha256,
        "linked_destinations": linked_destinations,
    }


def _preflight_remove_item(item: dict) -> dict:
    """校验并准备一个仅删除 Specials 独立海报的变更。"""
    source_value = str(item.get("source_path") or "")
    source = _path(source_value)
    destinations = [_path(str(value)) for value in item.get("library_paths") or []]
    plan_path = _path(str(item.get("plan_path") or ""))
    target_kind = str(item.get("kind") or "poster")
    library_relpath = str(item.get("library_relpath") or "").replace("\\", "/")
    old_url = str(item.get("old_url") or "")
    old_selection = str(item.get("old_specials_selection") or "")
    expected_sha256 = str(item.get("expected_old_sha256") or "")
    removal_reason = str(item.get("removal_reason") or "").strip()

    if target_kind != "poster" or library_relpath != "Specials/poster.jpg":
        raise ValueError("图片删除目前只允许撤销 Specials/poster.jpg")
    if not old_url.startswith("https://image.tmdb.org/t/p/original/"):
        raise ValueError(f"删除海报必须提供原 TMDB original URL: {source}")
    if old_selection not in _SPECIALS_SELECTIONS:
        raise ValueError(f"删除 Specials 海报缺少合法旧选择: {source}")
    if not removal_reason:
        raise ValueError(f"删除 Specials 海报必须记录原因: {source}")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError(f"删除海报缺少 expected_old_sha256: {source}")
    if not plan_path.is_file() or not destinations:
        raise FileNotFoundError(f"删除海报目标缺失: {source}")
    if len(set(destinations)) != len(destinations) or source in destinations:
        raise ValueError(f"删除海报目标重复或包含源路径: {source}")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    artwork = plan.get("artwork") or []
    matches = [
        entry for entry in artwork
        if _path(str(entry.get("source_path") or "")) == source
        and entry.get("kind") == target_kind
        and str(entry.get("library_relpath") or "").replace("\\", "/")
        == library_relpath
    ]
    if len(matches) > 1:
        raise ValueError(f"删除海报在 plan 中存在重复 artwork: {plan_path}")
    for entry in artwork:
        if (_path(str(entry.get("source_path") or "")) == source
                and entry not in matches):
            raise ValueError(f"删除海报源实体仍被其它 artwork 引用: {source}")
    if matches:
        match = matches[0]
        if str(match.get("url") or "") != old_url:
            raise ValueError(f"删除海报 URL 与 plan 不一致: {source}")
        if str(match.get("specials_selection") or "") != old_selection:
            raise ValueError(f"删除海报选择与 plan 不一致: {source}")

    source_exists = source.is_file()
    if source_exists:
        source_sha256 = _sha256(source)
        if source_sha256 != expected_sha256:
            raise ValueError(f"删除海报源图哈希不符合预期: {source}")
        source_inode = _inode(source)
        for destination in destinations:
            if destination.is_file() and _inode(destination) != source_inode:
                raise ValueError(f"删除海报目标不是源图硬链接: {destination}")
    destination_exists = [destination.is_file() for destination in destinations]
    if not source_exists and any(destination_exists):
        raise ValueError(f"删除海报源图已丢失但库侧仍存在: {source}")

    plan_is_old = bool(matches)
    all_destinations_exist = all(destination_exists)
    no_destinations_exist = not any(destination_exists)
    if plan_is_old and source_exists and all_destinations_exist:
        state = "pending"
    elif plan_is_old and not source_exists and no_destinations_exist:
        state = "plan_only"
    elif not plan_is_old and source_exists:
        state = "files_only" if any(destination_exists) else "source_only"
    elif not plan_is_old and not source_exists and no_destinations_exist:
        state = "completed"
    else:
        raise ValueError(f"删除海报恢复状态不一致: {source}")

    return {
        "operation": "remove",
        "source": source,
        "source_path_value": source_value,
        "destinations": destinations,
        "plan_path": plan_path,
        "plan": plan,
        "plan_matches": matches,
        "target_kind": target_kind,
        "library_relpath": library_relpath,
        "old_url": old_url,
        "new_method": "remove",
        "width": None,
        "height": None,
        "state": state,
        "expected_old_sha256": expected_sha256,
        "removal_reason": removal_reason,
    }


def _preflight_item(item: dict, review: dict, prior_by_source: dict[str, dict]) -> dict:
    operation = str(item.get("operation") or "replace").lower()
    item = _resolve_cached_item(item)
    if operation == "add":
        return _preflight_new_item(item, review, prior_by_source)
    if operation == "remove":
        return _preflight_remove_item(item)
    source = _path(str(item.get("source_path") or ""))
    destinations = [_path(str(value)) for value in item.get("library_paths") or []]
    plan_path = _path(str(item.get("plan_path") or ""))
    target_kind = str(item.get("kind") or "poster")
    new_method = str(item.get("new_method") or "")
    url = str(item.get("url") or "")
    old_url = str(item.get("old_url") or "")
    old_method = str(item.get("old_method") or "")
    fallback_video = _path(str(item.get("fallback_video_path") or "")) \
        if item.get("fallback_video_path") else None
    cached_path = _path(str(item["_cached_path"])) if item.get("_cached_path") else None
    candidate_width, candidate_height = _candidate_size(item)
    width, height = candidate_width, candidate_height
    expected_old_sha256 = str(item.get("expected_old_sha256") or "")
    if not source.is_file() or not plan_path.is_file():
        raise FileNotFoundError(f"增量海报目标缺失: {source}")
    if len(set(destinations)) != len(destinations):
        raise ValueError(f"库侧海报目标重复: {source}")
    if target_kind == "poster" and new_method != "tmdb":
        raise ValueError(f"作品海报增量更新只允许 TMDB 新图: {source}")
    if new_method == "tmdb":
        if not url.startswith("https://image.tmdb.org/t/p/original/"):
            raise ValueError(f"最终海报必须使用 TMDB original URL: {url}")
        if not old_url or old_url == url:
            raise ValueError(f"海报 old_url/new url 无有效差异: {source}")
        if not _valid_size_pair(candidate_width, candidate_height):
            raise ValueError(f"海报期望尺寸非法: {source}")
    elif new_method == "frame":
        if target_kind != "episode_thumb":
            raise ValueError(f"本地截帧增量更新只允许分集 thumb: {source}")
        if old_method != "tmdb" or not old_url:
            raise ValueError(f"本地截帧必须明确替换旧 TMDB still: {source}")
        if fallback_video is None or not fallback_video.is_file():
            raise FileNotFoundError(f"本地截帧视频缺失: {fallback_video}")
        if candidate_width is not None or candidate_height is not None:
            if not _valid_size_pair(candidate_width, candidate_height):
                raise ValueError(f"截帧期望尺寸非法: {source}")
    else:
        raise ValueError(f"未知增量 artwork method: {new_method}")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_old_sha256):
        raise ValueError(f"海报缺少 expected_old_sha256: {source}")
    source_sha256 = _sha256(source)
    source_inode = _inode(source)
    linked_destinations = []
    for destination in destinations:
        if not destination.is_file():
            raise FileNotFoundError(f"库侧海报目标缺失: {destination}")
        if destination.parent.stat().st_dev != source.stat().st_dev:
            raise OSError(f"源库跨卷，拒绝替换海报: {source} -> {destination}")
        if _inode(destination) == source_inode:
            linked_destinations.append(destination)

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    matches = [
        artwork for artwork in plan.get("artwork") or []
        if _path(str(artwork.get("source_path") or "")) == source
        and artwork.get("kind") == target_kind
        and (not item.get("library_relpath")
             or str(artwork.get("library_relpath") or "").replace("\\", "/")
             == str(item["library_relpath"]).replace("\\", "/"))
        and (
            operation == "replace"
            or target_kind != "poster"
            or str(artwork.get("library_relpath") or "").replace("\\", "/")
            != "Specials/poster.jpg"
        )
    ]
    if not matches:
        raise ValueError(f"plan artwork source_path 不符合预期: {plan_path}")

    def match_method(match: dict) -> str:
        return str(match.get("method") or "")

    def matches_old_state() -> bool:
        return all(
            match_method(match) == old_method
            and (old_method != "tmdb" or str(match.get("url") or "") == old_url)
            for match in matches
        )

    def matches_new_state() -> bool:
        if not all(match_method(match) == new_method for match in matches):
            return False
        if new_method == "tmdb":
            return all(str(match.get("url") or "") == url for match in matches)
        return all(
            _path(str(match.get("fallback_video_path") or "")) == fallback_video
            and not match.get("url")
            for match in matches
        )

    plan_is_old = matches_old_state()
    plan_is_new = matches_new_state()
    media_records = [
        raw for key in ("episodes", "extras")
        for raw in (plan.get(key) or [])
        if isinstance(raw, dict)
    ]
    record = None
    if fallback_video is not None:
        record = next(
            (raw for raw in media_records
             if _path(str(raw.get("video_path") or "")) == fallback_video),
            None,
        )
        if record is None:
            raise ValueError(f"plan 中找不到截帧对应视频记录: {fallback_video}")
    record_is_old = True
    record_is_new = True
    if record is not None:
        record_is_old = str(record.get("tmdb_still_url") or "") == old_url
        new_status = str(item.get("new_tmdb_match_status") or "")
        record_is_new = (
            not record.get("tmdb_still_url")
            and (not new_status or record.get("tmdb_match_status") == new_status)
        )
    plan_is_old = plan_is_old and record_is_old
    plan_is_new = plan_is_new and record_is_new
    source_is_old = source_sha256 == expected_old_sha256
    try:
        source_size = decode_image_size(source)
    except OSError as exc:
        raise ValueError(f"源海报无法解码: {source}") from exc
    prior = prior_by_source.get(_path_key(source))
    prior_sha256 = str((prior or {}).get("sha256") or "")
    expected_size = (
        (width, height)
        if new_method == "frame" and _valid_size_pair(width, height)
        else None
    )
    source_is_new = (source_size == expected_size if expected_size else source_size[0] > 0
                     and source_size[1] > 0) and (
        not prior_sha256 or source_sha256 == prior_sha256
    )
    if source_is_new:
        width, height = source_size
    all_linked = len(linked_destinations) == len(destinations)
    resumed_new = source_is_new and bool(prior_sha256)

    if source_is_old and all_linked and (plan_is_old or plan_is_new):
        state = "pending"
    elif source_is_new and plan_is_new:
        state = "completed" if all_linked else "relink"
    elif resumed_new and plan_is_old:
        # 上一次执行可能已替换源图，但在共享 plan 的合并写入前中断；
        # 只补 plan 与硬链接，不重复下载或截帧。
        state = "plan_only"
    else:
        raise ValueError(
            f"增量海报恢复状态不一致: {source} "
            f"(plan_methods={[match_method(match) for match in matches]}, "
            f"source_sha256={source_sha256})"
        )
    return {
        "operation": "replace",
        "source": source,
        "destinations": destinations,
        "plan_path": plan_path,
        "plan": plan,
        "plan_matches": matches,
        "target_kind": target_kind,
        "library_relpath": item.get("library_relpath"),
        "url": url,
        "new_method": new_method,
        "old_url": old_url,
        "old_method": old_method,
        "fallback_video": fallback_video,
        "new_tmdb_match_status": item.get("new_tmdb_match_status"),
        "width": width,
        "height": height,
        "candidate_width": candidate_width,
        "candidate_height": candidate_height,
        "cached_path": cached_path,
        "original_cache_dir": item.get("original_cache_dir"),
        "candidate_id": item.get("_resolved_candidate_id") or item.get("candidate_id"),
        "state": state,
        "source_sha256": source_sha256,
        "linked_destinations": linked_destinations,
    }


def apply_change_set(
        change_set: dict, *, loader: Callable[[str], bytes] | None = None,
        resume_report: dict | None = None,
) -> dict:
    if change_set.get("schema") != SCHEMA or change_set.get("mode") != "incremental":
        raise ValueError("图片 change set schema/mode 非法")
    items = change_set.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ValueError("图片 change set items 必须为 1..10 项")
    budget = change_set.get("budget") or {}

    def uses_cached_candidate(item: dict) -> bool:
        return bool(str(item.get("candidate_id") or "").strip()
                    or str(item.get("cache_path") or "").strip())

    expected_downloads = sum(
        str(item.get("operation") or "replace").lower() != "remove"
        and str(item.get("new_method") or "") == "tmdb"
        and not uses_cached_candidate(item)
        for item in items
    )
    expected_ffmpeg = sum(
        str(item.get("operation") or "replace").lower() != "remove"
        and str(item.get("new_method") or "") == "frame"
        for item in items
    )
    if int(budget.get("downloads", -1)) != expected_downloads:
        raise ValueError("图片 change set 下载预算与 TMDB 项数不一致")
    if int(budget.get("ffmpeg", 0)) != expected_ffmpeg:
        raise ValueError("图片 change set ffmpeg 预算与截帧项数不一致")
    review = None
    review_path_value = str(change_set.get("review_path") or "")
    if review_path_value:
        review_path = _path(review_path_value)
        if not review_path.is_file():
            raise FileNotFoundError(f"紧凑 artwork review 不存在: {review_path}")
        review = json.loads(review_path.read_text(encoding="utf-8"))
    elif expected_downloads:
        raise ValueError("TMDB artwork 增量更新必须提供紧凑 artwork review")

    prior_by_source = {
        _path_key(_path(str(item.get("source_path") or ""))): item
        for item in ((resume_report or {}).get("items") or [])
        if isinstance(item, dict)
        and item.get("source_path")
    }
    prepared = [_preflight_item(item, review, prior_by_source) for item in items]
    all_targets = [target for item in prepared for target in [item["source"], *item["destinations"]]]
    if len(set(all_targets)) != len(all_targets):
        raise ValueError("图片 change set 跨项目标重复")

    staged: list[dict] = []
    try:
        for item in prepared:
            if item["operation"] == "remove":
                continue
            if item["state"] != "pending":
                continue
            token_source = item["url"] or str(item["fallback_video"])
            token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:16]
            staging = item["source"].with_name(
                f".{item['source'].name}.update-{token}.tmp"
            )
            try:
                if item["new_method"] == "tmdb":
                    if item.get("cached_path"):
                        with Path(item["cached_path"]).open("rb") as source_handle:
                            with staging.open("wb") as staging_handle:
                                shutil.copyfileobj(source_handle, staging_handle)
                                staging_handle.flush()
                                os.fsync(staging_handle.fileno())
                        actual_size = decode_image_size(staging)
                    elif loader is None:
                        # Stable staging name lets images.download_image reuse its
                        # sibling .part file after an interrupted incremental run.
                        try:
                            actual_size = decode_image_size(staging)
                        except (FileNotFoundError, OSError, ValueError):
                            staging.unlink(missing_ok=True)
                            download_result = images.download_image(
                                item["url"], staging, skip_existing=False,
                                throttle=0, return_size=True,
                            )
                            if not isinstance(download_result, tuple):
                                raise OSError(
                                    f"TMDB 图片下载未返回实际尺寸: {item['url']}"
                                )
                            actual_size = download_result
                    else:
                        payload = loader(item["url"])
                        actual_size = decode_image_size(payload)
                        with staging.open("wb") as handle:
                            handle.write(payload)
                            handle.flush()
                            os.fsync(handle.fileno())
                    if actual_size[0] <= 0 or actual_size[1] <= 0:
                        raise ValueError(f"TMDB original 实际尺寸非法: {item['url']}")
                    item["width"], item["height"] = actual_size
                else:
                    frame_result = images.ffmpeg_thumb(
                        item["fallback_video"], staging, skip_existing=False,
                        return_size=True,
                    )
                    if not isinstance(frame_result, tuple):
                        raise OSError(f"本地截帧失败: {item['fallback_video']}")
                    actual_size = frame_result
                    expected_size = (
                        (item.get("candidate_width"), item.get("candidate_height"))
                        if item.get("candidate_width") is not None
                        or item.get("candidate_height") is not None
                        else None
                    )
                    if expected_size and actual_size != expected_size:
                        raise ValueError(
                            f"截帧实际尺寸 {actual_size} 与预期 {expected_size} 不一致: "
                            f"{item['fallback_video']}"
                        )
                    item["width"], item["height"] = actual_size
                item["staging"] = staging
                item["new_sha256"] = _sha256(staging)
                staged.append(item)
            except Exception:
                # Keep images' stable .part file for the next invocation, but do
                # not leave a decoded staging file that could be mistaken for a
                # committed source image.
                staging.unlink(missing_ok=True)
                raise

        plans_by_path: dict[str, dict] = {}
        for item in prepared:
            key = str(item["plan_path"])
            plans_by_path.setdefault(key, item["plan"])
            item["plan"] = plans_by_path[key]

        def plan_matches(plan: dict, item: dict) -> list[dict]:
            matches = []
            for artwork in plan.get("artwork") or []:
                if _path(str(artwork.get("source_path") or "")) != item["source"]:
                    continue
                if artwork.get("kind") != item["target_kind"]:
                    continue
                if item["library_relpath"]:
                    if (str(artwork.get("library_relpath") or "").replace("\\", "/")
                            != str(item["library_relpath"]).replace("\\", "/")):
                        continue
                elif (
                    str(item.get("operation") or "replace").lower() != "replace"
                    and item["target_kind"] == "poster"
                    and str(artwork.get("library_relpath") or "").replace("\\", "/")
                    == "Specials/poster.jpg"
                ):
                    continue
                matches.append(artwork)
            return matches

        for plan_path, plan in plans_by_path.items():
            related = [item for item in prepared if str(item["plan_path"]) == plan_path]
            before = json.dumps(plan, ensure_ascii=False, sort_keys=True)
            for item in related:
                if item["state"] == "completed":
                    continue
                matches = plan_matches(plan, item)
                if item.get("operation") == "remove":
                    if matches:
                        plan["artwork"] = [
                            entry for entry in plan.get("artwork") or []
                            if entry not in matches
                        ]
                    meta = plan.setdefault("_meta", {})
                    meta["specials_selection"] = "none"
                    meta["specials_selection_reason"] = item["removal_reason"]
                    meta["specials_artwork_policy"] = (
                        "No independent Specials poster selected: "
                        f"{item['removal_reason']}"
                    )
                    continue
                if item.get("operation") == "add" and not matches:
                    record = {
                        "scope": item["scope"],
                        "kind": item["target_kind"],
                        "source_path": item["source_path_value"],
                        "library_relpath": item["library_relpath"],
                        "method": item["new_method"],
                        "url": item["url"],
                        "specials_selection": item["specials_selection"],
                    }
                    if item.get("candidate_id"):
                        record["candidate_id"] = item["candidate_id"]
                    plan.setdefault("artwork", []).append(record)
                    matches = [record]
                if not matches:
                    raise ValueError(f"共享 plan 中找不到 artwork 目标: {item['source']}")
                if item["new_method"] == "tmdb" and review is not None:
                    plan["artwork_review"] = review
                for match in matches:
                    match["method"] = item["new_method"]
                    if item["new_method"] == "tmdb":
                        match["url"] = item["url"]
                        if item.get("candidate_id"):
                            match["candidate_id"] = item["candidate_id"]
                        if (
                            item.get("specials_selection")
                            and item["target_kind"] == "poster"
                            and str(item.get("library_relpath") or "").replace("\\", "/")
                            == "Specials/poster.jpg"
                        ):
                            match["specials_selection"] = item["specials_selection"]
                        match.pop("fallback_video_path", None)
                    else:
                        match.pop("url", None)
                        match["fallback_video_path"] = str(item["fallback_video"])
                if item["fallback_video"] is not None:
                    for key in ("episodes", "extras"):
                        for raw in plan.get(key) or []:
                            if (_path(str(raw.get("video_path") or ""))
                                    == item["fallback_video"]):
                                raw.pop("tmdb_still_url", None)
                                if item["new_tmdb_match_status"]:
                                    raw["tmdb_match_status"] = item["new_tmdb_match_status"]
            after = json.dumps(plan, ensure_ascii=False, sort_keys=True)
            if after != before:
                atomic_write_json(Path(plan_path), plan)

        results = []
        marker_updates: dict[str, list[dict]] = {}
        staged_by_source = {item["source"]: item for item in staged}
        for item in prepared:
            if item["operation"] == "remove":
                action = "already_removed"
                if item["state"] != "completed":
                    for destination in item["destinations"]:
                        destination.unlink(missing_ok=True)
                    item["source"].unlink(missing_ok=True)
                    if item["source"].exists() or any(
                            destination.exists()
                            for destination in item["destinations"]):
                        raise OSError(f"增量海报删除后验证失败: {item['source']}")
                    action = "removed"
                results.append({
                    "source_path": str(item["source"]),
                    "library_paths": [str(path) for path in item["destinations"]],
                    "url": item["old_url"],
                    "sha256": item["expected_old_sha256"],
                    "hardlinks_verified": 0,
                    "action": action,
                    "reason": item["removal_reason"],
                })
                continue
            action = "already_completed"
            staged_item = staged_by_source.get(item["source"])
            if staged_item is not None:
                os.replace(staged_item["staging"], item["source"])
                action = "updated"
            elif item["state"] == "relink":
                action = "relinked"
            elif item["state"] == "plan_only":
                action = "plan_reconciled"
            if item["state"] != "completed":
                source_inode = _inode(item["source"])
                for destination in item["destinations"]:
                    if destination.is_file() and _inode(destination) == source_inode:
                        continue
                    temporary = destination.with_name(
                        f".{destination.name}.relink-{uuid.uuid4().hex}.tmp"
                    )
                    try:
                        os.link(item["source"], temporary)
                        os.replace(temporary, destination)
                    finally:
                        if temporary.exists():
                            temporary.unlink(missing_ok=True)
            actual_size = decode_image_size(item["source"])
            linked = all(_inode(path) == _inode(item["source"])
                         for path in item["destinations"])
            actual_sha256 = _sha256(item["source"])
            expected_sha256 = (staged_item["new_sha256"] if staged_item is not None
                               else str((prior_by_source.get(_path_key(item["source"])) or {}).get("sha256") or ""))
            expected_size = ((item["width"], item["height"])
                             if _valid_size_pair(item["width"], item["height"])
                             else None)
            size_valid = actual_size == expected_size if expected_size else (
                actual_size[0] > 0 and actual_size[1] > 0
            )
            if (not size_valid or not linked
                    or (expected_sha256 and actual_sha256 != expected_sha256)):
                raise OSError(f"增量海报替换后验证失败: {item['source']}")
            if item.get("cached_path") and item.get("candidate_id"):
                marker_updates.setdefault(
                    str(item.get("original_cache_dir") or item["cached_path"].parent),
                    [],
                ).append({"candidate_id": item["candidate_id"]})
            results.append({
                "source_path": str(item["source"]),
                "library_paths": [str(path) for path in item["destinations"]],
                "url": item["url"],
                "candidate_id": item.get("candidate_id"),
                "cache_path": (
                    str(item["cached_path"])
                    if item.get("cached_path") else None
                ),
                "original_cache_dir": item.get("original_cache_dir"),
                "width": item["width"],
                "height": item["height"],
                "candidate_width": item.get("candidate_width"),
                "candidate_height": item.get("candidate_height"),
                "sha256": actual_sha256,
                "hardlinks_verified": len(item["destinations"]),
                "action": action,
            })
        for cache_dir, updates in marker_updates.items():
            try:
                artwork_cache.sync_current_markers(cache_dir, updates)
                for result in results:
                    if not result.get("candidate_id"):
                        continue
                    if _path_key(_path(str(result.get("original_cache_dir") or ""))) \
                            != _path_key(_path(cache_dir)):
                        continue
                    try:
                        current = artwork_cache.resolve_candidate(
                            cache_dir, candidate_id=result["candidate_id"]
                        )
                        result["cache_path"] = str(current["cache_path"])
                    except (OSError, ValueError):
                        pass
            except (OSError, ValueError):
                pass
        return {
            "schema": SCHEMA,
            "status": "completed",
            "downloads": expected_downloads,
            "ffmpeg": expected_ffmpeg,
            "updated": sum(item["action"] == "updated" for item in results),
            "removed": sum(item["action"] == "removed" for item in results),
            "resumed": sum(item["action"] not in {"updated", "removed"}
                            for item in results),
            "items": results,
        }
    finally:
        for item in staged:
            staging = item.get("staging")
            if isinstance(staging, Path) and staging.exists():
                staging.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cached-replace", action="store_true", required=True,
        help="从现有 plan/cache 直接构造内存 replace，不写 change-set JSON",
    )
    parser.add_argument("--plan", help="--cached-replace 使用的已有 plan JSON；省略时启用显式路径快捷模式")
    parser.add_argument(
        "--source-dir",
        help="无 plan 快捷模式：包含 poster.jpg/specials-poster.jpg 的精确片源作品目录",
    )
    parser.add_argument(
        "--library-dir",
        help="无 plan 快捷模式：精确库侧作品目录，不是公共 _Jellyfin 根",
    )
    parser.add_argument("--candidate-id", help="缓存候选编号，如 G01C01 或 G01-C01")
    parser.add_argument(
        "--target", choices=("main-poster", "specials-poster", "main", "specials"),
        help="--cached-replace 的目标海报",
    )
    parser.add_argument(
        "--original-cache-dir", dest="original_cache_dir",
        help="plan 模式可覆盖缓存目录；无 plan 模式必填",
    )
    parser.add_argument("--report", help="本机 JSON 报告路径；快捷模式省略时自动生成")
    args = parser.parse_args(argv)
    if args.cached_replace:
        if not args.candidate_id or not args.target:
            parser.error("--cached-replace 必须同时提供 --candidate-id、--target")
        normalized_id = _normalize_candidate_id(args.candidate_id)
        target_slug = _normalize_cached_target(args.target)
        if args.plan:
            if args.source_dir or args.library_dir:
                parser.error("--plan 不能与 --source-dir/--library-dir 同时使用")
            plan_path = _path(args.plan)
            report_path = _path(args.report) if args.report else plan_path.with_name(
                f"{plan_path.stem}.artwork-update-{target_slug}-{normalized_id}.report.json"
            )
        else:
            if not args.source_dir or not args.library_dir or not args.original_cache_dir:
                parser.error(
                    "无 plan 的 --cached-replace 必须同时提供 "
                    "--source-dir、--library-dir、--original-cache-dir、"
                    "--candidate-id、--target"
                )
            source_dir = _path(args.source_dir)
            library_dir = _path(args.library_dir)
            cache_dir = _path(args.original_cache_dir)
            report_path = _path(args.report) if args.report else Path.cwd() / (
                f"artwork-update-direct-{target_slug}-{normalized_id}.report.json"
            )
    try:
        previous_report = None
        if report_path.is_file():
            candidate = json.loads(report_path.read_text(encoding="utf-8"))
            expected_mode = (
                "cached-replace-direct" if args.cached_replace and not args.plan
                else "cached-replace"
            )
            if candidate.get("schema") == SCHEMA and (
                candidate.get("mode") == expected_mode
                and candidate.get("candidate_id") == normalized_id
                and candidate.get("target") == target_slug
            ):
                previous_report = candidate
        if args.cached_replace:
            if args.plan:
                report = apply_cached_replace(
                    plan_path, normalized_id, args.target,
                    original_cache_dir=args.original_cache_dir,
                    resume_report=previous_report,
                )
            else:
                report = apply_cached_replace_direct(
                    source_dir, library_dir, cache_dir,
                    normalized_id, args.target,
                )
    except Exception as exc:
        report = {"schema": SCHEMA, "status": "failed", "error": str(exc)}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(report_path, report)
        raise
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(report_path, report)
    cache_suffix = (
        f"；缓存文件: {report.get('cache_path')}；缓存目录: {report['original_cache_dir']}"
        if report.get("original_cache_dir") else ""
    )
    if report.get("marker_synced"):
        cache_suffix += "；CURRENT 标记已同步"
    elif report.get("marker_note"):
        cache_suffix += f"；CURRENT 未同步: {report['marker_note']}"
    print(f"增量图片变更完成: 更新 {report['updated']}；"
          f"删除 {report.get('removed', 0)}；"
          f"恢复/已完成 {report['resumed']}；报告: {report_path}"
          f"{cache_suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

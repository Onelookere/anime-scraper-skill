"""生成 TMDB 海报候选的 320x480 预览与 Agent 识图 contact sheet。

输入 JSON 描述已完成身份认证的 poster 候选。多模态开关只控制预览和审美复核；
人工原图缓存由独立配置开关控制，开启时才缓存合格竖版候选，当前 deterministic/Agent
选图仍直接作为刮削结果。开启识图时下载预览、缩放、补边和拼图，由 Agent 查看 sheet
后完成 ``selections``；关闭时沿用 ``deterministic_selection``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps

import artwork_cache
import tmdb
import images
from _common import (
    atomic_write_json,
    cache_dir,
    load_config,
    multimodal_artwork_review_enabled,
    normalize_path,
    artwork_cache_enabled,
)

PREVIEW_SIZE = (320, 480)
CANDIDATE_LIMIT = 5
LABEL_HEIGHT = 58
GROUPS_PER_SHEET = 3
JPEG_QUALITY = 90
BACKGROUND = (32, 32, 32)
LABEL_BACKGROUND = (18, 18, 18)
LABEL_FOREGROUND = (245, 245, 245)
try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow 9.0 compatibility
    _LANCZOS = Image.LANCZOS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_DETERMINISTIC_FIELDS = (
    "kind", "file_path", "url", "language", "vote_count", "vote_average",
    "width", "height",
)


def _formal_seasons(tv_entry: dict) -> list[int] | None:
    """从快照 TV detail 提取正式季号；缺 detail 返回 None。"""
    detail = tv_entry.get("detail") or {}
    if not isinstance(detail, dict) or not detail:
        return None
    numbers = [
        item["season_number"] for item in detail.get("seasons") or []
        if isinstance(item, dict) and isinstance(item.get("season_number"), int)
        and item["season_number"] != 0
    ]
    return sorted(numbers)


def _snapshot_tv_entry(snapshot: dict, tv_id: int) -> dict:
    entry = ((snapshot.get("tmdb") or {}).get("tv") or {}).get(str(tv_id))
    if not isinstance(entry, dict) or not entry:
        raise ValueError(
            f"快照中没有 TV {tv_id} 的数据；请先用 metadata_snapshot.py "
            "补 --tmdb-tv-id/--tmdb-season-images 后重跑"
        )
    return entry


def _snapshot_season_posters(snapshot: dict, tv_id: int, season: int) -> list[dict]:
    entry = _snapshot_tv_entry(snapshot, tv_id)
    seasons = entry.get("seasons") or {}
    images = (seasons.get(str(season)) or {}).get("images") or {}
    pool = list(images.get("posters") or [])
    if not pool:
        raise ValueError(
            f"快照缺少 TV {tv_id} Season {season} 的海报池；"
            f"请补 --tmdb-season-images {tv_id}:{season} 重跑快照"
        )
    return pool


def _merged_main_pool(snapshot: dict, tv_id: int, season: int) -> list[dict]:
    """正式季海报池；单正式季时按 artwork-library §6 合并季池与系列池。"""
    entry = _snapshot_tv_entry(snapshot, tv_id)
    formal = _formal_seasons(entry)
    if formal is None:
        raise ValueError(
            f"快照缺少 TV {tv_id} 的详情(--tmdb-tv-id)，无法判定单季合并规则；"
            "请补 --tmdb-tv-id 重跑快照"
        )
    pool = _snapshot_season_posters(snapshot, tv_id, season)
    if season != 0 and formal == [season]:
        tv_pool = list((entry.get("images") or {}).get("posters") or [])
        if not tv_pool:
            raise ValueError(
                f"TV {tv_id} 只有一个正式季，主海报池必须合并系列池；"
                f"请补 --tmdb-tv-images {tv_id} 重跑快照"
            )
        pool = pool + tv_pool
    return pool


def _deterministic_pick(pool: list[dict]) -> dict:
    picked = tmdb.poster_review_candidates(pool, 1)
    if not picked:
        raise ValueError(
            f"海报池 {len(pool)} 张里没有可用竖版候选；"
            "确认快照图片池是否只含横图或缺少 width/height"
        )
    return picked[0]


def _build_season_group(snapshot: dict, tv_id: int, season: int,
                        group_id: str, label: str,
                        candidate_limit: int) -> tuple[dict, int]:
    pool = _merged_main_pool(snapshot, tv_id, season)
    deterministic = _deterministic_pick(pool)
    return _render_group(group_id, label, pool, deterministic, candidate_limit), len(pool)


def _render_group(group_id: str, label: str, pool: list[dict],
                  deterministic: dict, candidate_limit: int,
                  *, cache_pool: list[dict] | None = None) -> dict:
    """``pool`` 决定识图席位，``cache_pool`` 决定原图缓存池（默认与 pool 相同）。

    确定性选择必须占一个识图席位：Agent 看不到它就既无法确认也无法否决它，
    contact sheet 会退化成“从另一批图里另选一张”。席位满时挤掉排序最末位，
    因为确定性结果比第 N 名候选更需要被看见。
    """
    review_pool = list(pool)
    candidates = tmdb.poster_review_candidates(review_pool, candidate_limit)
    deterministic_path = str(deterministic.get("file_path") or "")
    if deterministic_path and not any(
            str(item.get("file_path") or "") == deterministic_path
            for item in candidates):
        candidates = [dict(deterministic), *candidates][:max(1, candidate_limit)]
    return {
        "group_id": group_id,
        "label": label,
        "work_name": label,
        "deterministic_selection": {
            key: deterministic.get(key) for key in _DETERMINISTIC_FIELDS
        },
        "candidates": candidates,
        "cache_candidates": list(cache_pool if cache_pool is not None else pool),
    }


def build_request(
        snapshot: dict, *, series_name: str,
        season_groups: list[dict], movie_groups: list[dict],
        specials_groups: list[dict],
        specials_runner=None, candidate_limit: int = CANDIDATE_LIMIT,
) -> tuple[dict, list[str]]:
    """从元数据快照确定性组装 artwork review 请求 JSON。

    组装规则全部来自既有机械步骤：候选席位 = ``tmdb.poster_review_candidates``
    的排序/竖图/席位覆盖，deterministic = 排序头部或 Specials 三段选择结果。
    Agent 只需要决定组划分（哪几个单元进同一次 review）与标签，不再手工誊写
    候选池。返回 ``(请求 JSON, 提示行列表)``；Specials 三段选择结果为 none 时
    跳过该组并记录提示。
    """
    if not str(series_name or "").strip():
        raise ValueError("必须提供 --series-name（系列名，用于原图缓存目录）")
    groups: list[dict] = []
    notices: list[str] = []
    for spec in season_groups:
        group, pool_size = _build_season_group(
            snapshot, spec["tv_id"], spec["season"],
            spec["group_id"], spec["label"], candidate_limit,
        )
        groups.append(group)
        notices.append(f"组 {spec['group_id']}: 池 {pool_size} 张（Season {spec['season']}）")
    for spec in movie_groups:
        entry = ((snapshot.get("tmdb") or {}).get("movie") or {}).get(str(spec["movie_id"]))
        pool = list(((entry or {}).get("images") or {}).get("posters") or [])
        if not pool:
            raise ValueError(
                f"快照缺少 movie {spec['movie_id']} 的海报池；"
                f"请补 --tmdb-movie-images {spec['movie_id']} 重跑快照"
            )
        groups.append(_render_group(
            spec["group_id"], spec["label"], pool,
            _deterministic_pick(pool), candidate_limit,
        ))
        notices.append(f"组 {spec['group_id']}: 池 {len(pool)} 张（movie {spec['movie_id']}）")
    runner = specials_runner or tmdb.run_specials_cli
    for spec in specials_groups:
        result = runner(spec["tv_id"], spec["main_season"])
        selection = result.get("selection")
        candidate = result.get("candidate")
        if selection == "none" or not isinstance(candidate, dict):
            notices.append(
                f"组 {spec['group_id']}: Specials 三段选择无独立候选，"
                "跳过该组（不生成 Specials/poster.jpg）"
            )
            continue
        main_pool = _merged_main_pool(snapshot, spec["tv_id"], spec["main_season"])
        # 识图席位取候选实际来源的池。season_zero 的候选来自 Season 0 池，若把
        # 主池混进识图池，高票主海报会占满 5 个席位、把真正的 Specials 候选挤掉，
        # Agent 只能从主池另选一张（artwork-library.md §6 要求该组放 Season 0 池）。
        # 缓存池仍取合并池，原图缓存覆盖两边。
        if selection == "season_zero":
            review_pool = _snapshot_season_posters(snapshot, spec["tv_id"], 0)
            cache_pool = [*main_pool, *review_pool]
        else:
            review_pool = list(main_pool)
            cache_pool = list(main_pool)
        candidate_path = str(candidate.get("file_path") or "")
        if candidate_path:
            for bucket in (review_pool, cache_pool):
                if not any(str(item.get("file_path") or "") == candidate_path
                           for item in bucket):
                    bucket.append(dict(candidate))
        groups.append(_render_group(
            spec["group_id"], spec["label"], review_pool, candidate,
            candidate_limit, cache_pool=cache_pool,
        ))
        notices.append(
            f"组 {spec['group_id']}: Specials 选择 {selection}，"
            f"识图池 {len(review_pool)} 张 / 缓存池 {len(cache_pool)} 张"
            f"（候选 {candidate_path}）"
        )
    if not groups:
        raise ValueError("没有生成任何候选组；至少提供一个 --season-group/--movie-group")
    return {"series_name": series_name, "groups": groups}, notices


def _parse_group_spec(raw: str, numeric_fields: list[str], template: str) -> dict:
    """解析 ``ID:...:GROUP_ID[=显示名]`` 形式的组参数；显示名缺省取 GROUP_ID。"""
    parts = raw.split(":", len(numeric_fields))
    if len(parts) != len(numeric_fields) + 1 or not parts[-1].strip():
        raise argparse.ArgumentTypeError(
            f"组参数必须是 {template}（GROUP_ID 后可用 = 追加显示名）: {raw}"
        )
    parsed: dict = {}
    for name, value in zip(numeric_fields, parts[:-1]):
        try:
            parsed[name] = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError(
                f"组参数必须是 {template}: {raw}"
            ) from error
        if parsed[name] < 1:
            raise argparse.ArgumentTypeError(f"ID 必须是正整数: {raw}")
    group_id, separator, label = parts[-1].partition("=")
    group_id = group_id.strip()
    if not group_id or not (label or group_id).strip():
        raise argparse.ArgumentTypeError(f"GROUP_ID 与显示名不能为空: {raw}")
    parsed["group_id"] = group_id
    parsed["label"] = (label or group_id).strip()
    return parsed


def _safe_slug(value: str, fallback: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (rendered or fallback)[:80]


def _cache_series_name(request_payload: dict) -> str:
    explicit = str(request_payload.get("series_name") or "").strip()
    if not explicit:
        raise ValueError("开启原图缓存时 request 必须提供 series_name")
    return artwork_cache.compact_artwork_label(explicit)


def _preview_url(url: str) -> str:
    """TMDB original URL 改用 w500 预览；其它来源 URL 保持不变。"""
    marker = "image.tmdb.org/t/p/original/"
    if marker in url:
        return url.replace(marker, "image.tmdb.org/t/p/w500/", 1)
    return url


def _download_bytes(candidate: dict, timeout: int = images.DEFAULT_DOWNLOAD_TIMEOUT) -> bytes:
    url = _preview_url(str(candidate.get("url") or ""))
    if not url:
        raise ValueError("poster 候选缺少 url")
    try:
        return images.download_bytes(url, timeout=timeout)
    except Exception as error:
        raise OSError(
            f"poster 预览下载失败: {candidate.get('file_path') or '<unknown>'}: {error}"
        ) from error


def _download_original_bytes(
        candidate: dict, timeout: int = images.DEFAULT_DOWNLOAD_TIMEOUT,
) -> tuple[bytes, tuple[int, int]]:
    url = str(candidate.get("url") or "")
    if not url:
        raise ValueError("poster 候选缺少 original url")
    try:
        payload = images.download_bytes(
            url, timeout=timeout, return_size=True,
        )
        if not isinstance(payload, tuple):
            raise OSError("poster 原图下载未返回实际尺寸")
        return payload
    except Exception as error:
        raise OSError(
            f"poster 原图下载失败: {candidate.get('file_path') or '<unknown>'}: {error}"
        ) from error


def _cached_preview_bytes(
        candidate: dict, loader: Callable[[dict], bytes], cache_root: Path | None,
) -> tuple[bytes, bool]:
    """按预览 URL 缓存并校验候选字节；损坏缓存自动失效。"""
    if cache_root is None:
        return loader(candidate), False
    preview_url = _preview_url(str(candidate.get("url") or ""))
    key = hashlib.sha256(preview_url.encode("utf-8")).hexdigest()
    path = cache_root / key[:2] / f"{key}.img"
    if path.is_file():
        payload = path.read_bytes()
        try:
            with Image.open(BytesIO(payload)) as image:
                image.verify()
            return payload, True
        except (OSError, ValueError):
            path.unlink(missing_ok=True)

    payload = loader(candidate)
    with Image.open(BytesIO(payload)) as image:
        image.verify()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return payload, False


def _font(size: int = 22):
    candidates = (
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    )
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _candidate_label(candidate: dict) -> tuple[str, str]:
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    resolution = candidate.get("resolution_class") or tmdb.poster_resolution_class(candidate)
    top = (f"{candidate['candidate_id']}  {candidate.get('language') or '-'}  "
           f"v{int(candidate.get('vote_count') or 0)}")
    bottom = f"{width}x{height}  {str(resolution).upper()}"
    return top, bottom


def _preview_image(payload: bytes) -> tuple[Image.Image, tuple[int, int]]:
    with Image.open(BytesIO(payload)) as source:
        source.load()
        oriented = ImageOps.exif_transpose(source)
        original_size = oriented.size
        rgb = oriented.convert("RGB")
    preview = ImageOps.pad(
        rgb,
        PREVIEW_SIZE,
        method=_LANCZOS,
        color=BACKGROUND,
        centering=(0.5, 0.5),
    )
    return preview, original_size


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(tempfile.gettempdir()) / "anime-scraper" / "artwork-review" / stamp


def _manifest_payload(review: dict) -> bytes:
    return json.dumps(
        review, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def compact_plan_review(review: dict) -> dict:
    """把完整临时 manifest 投影为可长期保存在 plan 内的最小审计记录。"""
    if review.get("schema") != "anime-scraper-artwork-review-v1":
        raise ValueError("artwork review schema 非法")
    status = review.get("status")
    if status not in {"completed", "not_required", "disabled"}:
        raise ValueError("只有 completed/not_required/disabled review 可写入 plan")

    groups = review.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("artwork review groups 必须是非空列表")
    compact_groups = []
    for group in groups:
        candidates = group.get("candidates") if isinstance(group, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("artwork review group 必须包含候选")
        compact_groups.append({
            "group_id": group.get("group_id"),
            "candidates": [
                {"candidate_id": item.get("candidate_id"), "url": item.get("url"),
                 "width": item.get("width"), "height": item.get("height"),
                 "language": item.get("language"),
                 "resolution_class": item.get("resolution_class"),
                 "visible_text_role": item.get("visible_text_role"),
                 "primary_title_prominence": item.get("primary_title_prominence"),
                 "visual_issues": item.get("visual_issues")}
                for item in candidates
            ],
        })

    warnings = review.get("warnings") or []
    if not isinstance(warnings, list) or any(
            not isinstance(item, str) or not item.strip() for item in warnings):
        raise ValueError("artwork review warnings 必须是非空字符串列表")

    original_cache = review.get("original_cache") or {}
    original_cache_dir = str(
        review.get("original_cache_dir") or original_cache.get("dir") or ""
    ).strip()
    if original_cache_dir and review.get("selections"):
        try:
            artwork_cache.sync_current_markers(
                original_cache_dir,
                review.get("selections") or [],
            )
        except (OSError, ValueError) as error:
            # The cache is a human fallback, but selected-marker drift must be visible.
            warnings.append(f"原图缓存 CURRENT 标记校验失败: {error}")

    result = {
        "schema": review["schema"],
        "status": status,
        "multimodal_review_enabled": review.get("multimodal_review_enabled"),
        "selection_method": review.get("selection_method"),
        "generated_at": review.get("generated_at"),
        "source_manifest_sha256": hashlib.sha256(_manifest_payload(review)).hexdigest(),
        "preview": {
            key: (review.get("preview") or {}).get(key)
            for key in ("width", "height", "resize", "label_fields")
        },
        "candidate_limit": review.get("candidate_limit"),
        "groups": compact_groups,
        "selections": [
            {
                key: selection.get(key)
                for key in ("group_id", "candidate_id", "confidence", "reason", "flags",
                            "decision_factors")
            }
            for selection in (review.get("selections") or [])
        ],
        "warnings": list(warnings),
    }
    if original_cache_dir:
        result["original_cache_dir"] = original_cache_dir
    if status in {"not_required", "disabled"}:
        result["reason"] = review.get("reason")
    return result


def build_review(
        request_payload: dict, output_dir: str | Path, *,
        loader: Callable[[dict], bytes] = _download_bytes,
        candidate_limit: int = CANDIDATE_LIMIT,
        multimodal_enabled: bool | None = None,
        preview_cache_dir: str | Path | None = None,
        original_cache_root: str | Path | None = None,
        original_loader: Callable[
            [dict], bytes | tuple[bytes, tuple[int, int]]
        ] | None = None,
) -> dict:
    """生成预览、contact sheet 与待 Agent 填写的 review manifest。"""
    groups = request_payload.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("artwork review 输入必须包含非空 groups 列表")
    if candidate_limit < 1 or candidate_limit > CANDIDATE_LIMIT:
        raise ValueError(f"candidate_limit 必须在 1..{CANDIDATE_LIMIT} 之间")
    enabled = (multimodal_artwork_review_enabled()
               if multimodal_enabled is None else multimodal_enabled)
    if type(enabled) is not bool:
        raise ValueError("multimodal_enabled 必须是布尔值")

    output = Path(normalize_path(output_dir)).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache_root = (Path(normalize_path(preview_cache_dir)).expanduser().resolve()
                  if preview_cache_dir is not None else None)
    prepared_groups: list[dict] = []
    warnings: list[str] = []
    for group_index, raw_group in enumerate(groups, 1):
        if not isinstance(raw_group, dict):
            raise ValueError(f"groups[{group_index - 1}] 必须是对象")
        group_id = str(raw_group.get("group_id") or "").strip()
        if not group_id:
            raise ValueError(f"groups[{group_index - 1}] 缺少 group_id")
        label = str(raw_group.get("label") or "").strip()
        work_name = str(raw_group.get("work_name") or "").strip()
        if not label or not work_name:
            raise ValueError(
                f"groups[{group_index - 1}] 必须提供 label 和 work_name"
            )
        raw_candidates = raw_group.get("candidates")
        if not isinstance(raw_candidates, list):
            raise ValueError(f"groups[{group_index - 1}].candidates 必须是列表")
        if "cache_candidates" not in raw_group:
            raise ValueError(f"groups[{group_index - 1}] 缺少 cache_candidates")
        raw_cache_candidates = raw_group["cache_candidates"]
        if not isinstance(raw_cache_candidates, list):
            raise ValueError(
                f"groups[{group_index - 1}].cache_candidates 必须是列表"
            )
        # The cache pool is intentionally independent from the small visual-review
        # pool. It is still normalized/deduplicated here, but never truncated to 5.
        cache_candidates = tmdb.poster_review_candidates(
            raw_cache_candidates, max(1, len(raw_cache_candidates))
        )
        if not cache_candidates:
            raise ValueError(f"识图组 {group_id} 没有可用竖版 poster 候选")
        review_group_id = f"G{group_index:02d}"
        review_candidates = tmdb.poster_review_candidates(
            raw_candidates, candidate_limit
        )
        cache_paths = {
            str(candidate.get("file_path") or "") for candidate in cache_candidates
        }
        missing_review_paths = [
            str(candidate.get("file_path") or "")
            for candidate in review_candidates
            if str(candidate.get("file_path") or "") not in cache_paths
        ]
        if missing_review_paths:
            raise ValueError(
                f"识图组 {group_id} 的 cache_candidates 必须包含 candidates"
            )

        # Keep the five review IDs stable when a full cache pool is added; extra
        # cache-only candidates receive IDs after the review candidates.
        ordered_candidates = []
        seen_paths: set[str] = set()
        for candidate in [*review_candidates, *cache_candidates]:
            path = str(candidate.get("file_path") or "")
            if path and path not in seen_paths:
                ordered_candidates.append(candidate)
                seen_paths.add(path)
        all_with_ids = []
        for candidate_index, candidate in enumerate(ordered_candidates, 1):
            rendered = dict(candidate)
            rendered["candidate_id"] = f"{review_group_id}-C{candidate_index:02d}"
            all_with_ids.append(rendered)
        candidates_by_path = {
            str(candidate.get("file_path") or ""): candidate
            for candidate in all_with_ids
        }
        if enabled:
            candidates = [
                candidates_by_path[str(candidate.get("file_path") or "")]
                for candidate in review_candidates
                if str(candidate.get("file_path") or "") in candidates_by_path
            ]
        else:
            deterministic = raw_group.get("deterministic_selection")
            if not isinstance(deterministic, dict):
                raise ValueError(
                    f"关闭多模态时识图组 {group_id} 必须提供 deterministic_selection"
                )
            deterministic_path = str(deterministic.get("file_path") or "")
            deterministic_candidate = candidates_by_path.get(deterministic_path)
            candidates = [deterministic_candidate] if deterministic_candidate else []
        if not candidates:
            raise ValueError(f"识图组 {group_id} 没有可用竖版 poster 候选")
        deterministic = raw_group.get("deterministic_selection")
        current_candidate = None
        if isinstance(deterministic, dict):
            current_candidate = candidates_by_path.get(
                str(deterministic.get("file_path") or "")
            )
        current_candidate = current_candidate or all_with_ids[0]
        prepared_groups.append({
            "group_id": group_id,
            "review_group_id": review_group_id,
            "label": label,
            "work_name": work_name,
            "group_slug": _safe_slug(group_id, review_group_id.lower()),
            "candidates": candidates,
            "cache_candidates": all_with_ids,
            "cache_candidate_count": len(all_with_ids),
            "current_candidate_id": current_candidate["candidate_id"],
        })

    original_cache_info = None
    if original_cache_root is not None:
        original_loader = original_loader or (
            _download_original_bytes if loader is _download_bytes else loader
        )
        series_name = _cache_series_name(request_payload)
        try:
            cache_path = artwork_cache.create_review_dir(
                original_cache_root, series_name or "Artwork"
            )
            cache_manifest = artwork_cache.cache_originals(
                cache_path,
                [
                    {
                        "group_id": group["group_id"],
                        "review_group_id": group["review_group_id"],
                        "label": group["label"],
                        "work_name": group["work_name"],
                        "current_candidate_id": group["current_candidate_id"],
                        "cache_candidates": group["cache_candidates"],
                    }
                    for group in prepared_groups
                ],
                loader=original_loader,
                max_workers=images.artwork_worker_config()["tmdb_workers"],
                series_name=series_name,
            )
            original_cache_info = {
                "dir": str(cache_path),
                "series_name": series_name,
                "status": cache_manifest.get("status"),
                "cached_count": sum(
                    bool(candidate.get("cached"))
                    for group in cache_manifest.get("groups") or []
                    for candidate in group.get("candidates") or []
                ),
                "failed_count": len(cache_manifest.get("errors") or []),
            }
        except Exception as error:  # Best effort: artwork review must continue.
            original_cache_info = {
                "status": "failed",
                "cached_count": 0,
                "failed_count": 1,
                "error": str(error),
            }

    rendered_groups: list[dict] = []
    preview_images: dict[str, Image.Image] = {}
    cache_hits = 0
    cache_misses = 0
    visual_review_required = enabled and any(
        len(group["candidates"]) > 1 for group in prepared_groups
    )
    prefetched: dict[tuple[int, int], tuple[bytes, bool]] = {}
    if visual_review_required:
        jobs = [
            (group_index, candidate_index, candidate)
            for group_index, group in enumerate(prepared_groups)
            for candidate_index, candidate in enumerate(group["candidates"], 1)
        ]
        worker_count = images.artwork_worker_config()["tmdb_workers"]
        executor = ThreadPoolExecutor(max_workers=min(worker_count, len(jobs)))
        futures = {
            executor.submit(_cached_preview_bytes, candidate, loader, cache_root):
            (group_index, candidate_index)
            for group_index, candidate_index, candidate in jobs
        }
        try:
            for future in as_completed(futures):
                prefetched[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    for group_index, group in enumerate(prepared_groups):
        rendered_candidates: list[dict] = []
        for candidate_index, candidate in enumerate(group["candidates"], 1):
            candidate_id = candidate["candidate_id"]
            rendered = dict(candidate)
            rendered["candidate_id"] = candidate_id
            rendered["resolution_class"] = tmdb.poster_resolution_class(rendered)
            if visual_review_required:
                payload, cache_hit = prefetched[(group_index, candidate_index)]
                cache_hits += int(cache_hit)
                cache_misses += int(not cache_hit)
                preview, decoded_size = _preview_image(payload)
                preview_path = output / f"{group['group_slug']}-{candidate_id}.jpg"
                preview.save(preview_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
                preview_images[candidate_id] = preview
                rendered.update({
                    "preview_path": str(preview_path),
                    "decoded_width": decoded_size[0],
                    "decoded_height": decoded_size[1],
                })
            rendered_candidates.append(rendered)
        rendered_groups.append({
            "group_id": group["group_id"],
            "review_group_id": group["review_group_id"],
            "label": group["label"],
            "cache_candidate_count": group["cache_candidate_count"],
            "candidates": rendered_candidates,
        })

    sheets: list[dict] = []
    font = _font(18)
    row_height = PREVIEW_SIZE[1] + LABEL_HEIGHT
    sheet_starts = range(0, len(rendered_groups), GROUPS_PER_SHEET)
    for sheet_index, start in enumerate(sheet_starts if visual_review_required else [], 1):
        sheet_groups = rendered_groups[start:start + GROUPS_PER_SHEET]
        canvas = Image.new(
            "RGB",
            (PREVIEW_SIZE[0] * CANDIDATE_LIMIT, row_height * len(sheet_groups)),
            BACKGROUND,
        )
        draw = ImageDraw.Draw(canvas)
        for row, group in enumerate(sheet_groups):
            y = row * row_height
            for column, candidate in enumerate(group["candidates"]):
                x = column * PREVIEW_SIZE[0]
                canvas.paste(preview_images[candidate["candidate_id"]], (x, y))
                draw.rectangle(
                    (x, y + PREVIEW_SIZE[1], x + PREVIEW_SIZE[0], y + row_height),
                    fill=LABEL_BACKGROUND,
                )
                top_label, bottom_label = _candidate_label(candidate)
                draw.text(
                    (x + 8, y + PREVIEW_SIZE[1] + 4), top_label,
                    font=font, fill=LABEL_FOREGROUND,
                )
                draw.text(
                    (x + 8, y + PREVIEW_SIZE[1] + 29), bottom_label,
                    font=font, fill=LABEL_FOREGROUND,
                )
        sheet_path = output / f"contact-sheet-{sheet_index:02d}.jpg"
        canvas.save(sheet_path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        sheets.append({
            "sheet_id": f"sheet-{sheet_index:02d}",
            "path": str(sheet_path),
            "group_ids": [group["group_id"] for group in sheet_groups],
            "width": canvas.width,
            "height": canvas.height,
        })

    if not enabled:
        status = "disabled"
        selection_method = "deterministic_existing_pipeline"
        reason = "config_disabled"
    elif visual_review_required:
        status = "pending_agent_review"
        selection_method = "agent_multimodal"
        reason = None
    else:
        status = "not_required"
        selection_method = "deterministic_single_candidate"
        reason = "single_candidate"

    manifest = {
        "schema": "anime-scraper-artwork-review-v1",
        "status": status,
        "multimodal_review_enabled": enabled,
        "selection_method": selection_method,
        "generated_at": _utc_now(),
        "preview": {
            "width": PREVIEW_SIZE[0],
            "height": PREVIEW_SIZE[1],
            "resize": "contain-and-pad",
            "label_fields": ["candidate_id", "width", "height", "resolution_class",
                             "language", "vote_count"],
            "background_rgb": list(BACKGROUND),
            "jpeg_quality": JPEG_QUALITY,
        },
        "candidate_limit": candidate_limit,
        "preview_cache": {
            "enabled": cache_root is not None,
            "hits": cache_hits,
            "misses": cache_misses,
        },
        "groups": rendered_groups,
        "sheets": sheets,
        "selections": ([] if visual_review_required else [
            {
                "group_id": group["group_id"],
                "candidate_id": group["candidates"][0]["candidate_id"],
                "confidence": "high",
                "reason": ("配置关闭多模态，沿用原有排序与感知哈希流程结果"
                           if not enabled else "该组只有一个有效候选"),
                "flags": [],
            }
            for group in rendered_groups
        ]),
        "warnings": warnings,
    }
    if original_cache_info:
        manifest["original_cache"] = original_cache_info
        if original_cache_info.get("dir"):
            manifest["original_cache_dir"] = original_cache_info["dir"]
    if reason:
        manifest["reason"] = reason
    manifest_path = output / "artwork-review.json"
    manifest["manifest_path"] = str(manifest_path)
    atomic_write_json(manifest_path, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="候选分组 JSON")
    parser.add_argument("--output-dir", help="预览输出目录；默认写系统临时目录")
    parser.add_argument("--original-cache-root",
                        help="原图缓存根目录；需同时开启 config.artwork.artwork_cache")
    parser.add_argument("--candidate-limit", type=int, default=CANDIDATE_LIMIT,
                        help=f"每组最多候选数（1..{CANDIDATE_LIMIT}，默认 {CANDIDATE_LIMIT}）")
    parser.add_argument("--compact-review", metavar="MANIFEST",
                        help="把已完成的完整 review manifest 压缩为 plan 审计记录")
    parser.add_argument("--plan-review-out", metavar="PATH",
                        help="紧凑 plan 审计记录输出路径")
    parser.add_argument("--build-request", action="store_true",
                        help="从元数据快照确定性生成 --input 请求 JSON，不再手工誊写候选池")
    parser.add_argument("--snapshot", metavar="FILE",
                        help="metadata_snapshot.py 产出的快照 JSON（--build-request 用）")
    parser.add_argument("--output", metavar="FILE",
                        help="请求 JSON 输出路径（--build-request 用）")
    parser.add_argument("--series-name",
                        help="系列名（--build-request 用；原图缓存目录命名）")
    parser.add_argument("--season-group", action="append", default=[], metavar="TV_ID:SEASON:GROUP_ID[=显示名]",
                        help="正式季海报组，可重复；池为该季海报（单正式季自动合并系列池）")
    parser.add_argument("--movie-group", action="append", default=[], metavar="MOVIE_ID:GROUP_ID[=显示名]",
                        help="电影海报组，可重复")
    parser.add_argument("--specials-group", action="append", default=[], metavar="TV_ID:MAIN_SEASON:GROUP_ID[=显示名]",
                        help="Specials 三段选择组，可重复；选择结果为 none 时自动跳过")
    args = parser.parse_args(argv)
    if args.build_request:
        if args.input or args.output_dir or args.compact_review or args.plan_review_out:
            parser.error("--build-request 不能与 --input/--output-dir/--compact-review 同时使用")
        if not (args.snapshot and args.output and args.series_name):
            parser.error("--build-request 需要 --snapshot/--output/--series-name")
        if not (args.season_group or args.movie_group or args.specials_group):
            parser.error("--build-request 至少提供一个 --season-group/--movie-group/--specials-group")
        snapshot = json.loads(
            Path(normalize_path(args.snapshot)).read_text(encoding="utf-8")
        )
        season_specs = [
            _parse_group_spec(raw, ["tv_id", "season"], "TV_ID:SEASON:GROUP_ID[=显示名]")
            for raw in args.season_group
        ]
        movie_specs = [
            _parse_group_spec(raw, ["movie_id"], "MOVIE_ID:GROUP_ID[=显示名]")
            for raw in args.movie_group
        ]
        specials_specs = [
            _parse_group_spec(raw, ["tv_id", "main_season"], "TV_ID:MAIN_SEASON:GROUP_ID[=显示名]")
            for raw in args.specials_group
        ]
        payload, notices = build_request(
            snapshot,
            series_name=args.series_name,
            season_groups=season_specs,
            movie_groups=movie_specs,
            specials_groups=specials_specs,
            candidate_limit=args.candidate_limit,
        )
        output_path = atomic_write_json(args.output, payload)
        print(f"识图请求 JSON: {output_path}")
        for notice in notices:
            print(f"  {notice}")
        print(f"  共 {len(payload['groups'])} 组；"
              "接着运行 --input <该文件> 生成候选与 contact sheet")
        return 0
    if args.compact_review:
        if args.input or args.output_dir:
            parser.error("--compact-review 不能与 --input/--output-dir 同时使用")
        manifest_path = Path(normalize_path(args.compact_review)).expanduser().resolve()
        review = json.loads(manifest_path.read_text(encoding="utf-8"))
        compact = compact_plan_review(review)
        output_path = (Path(normalize_path(args.plan_review_out)).expanduser().resolve()
                       if args.plan_review_out else
                       manifest_path.with_name("artwork-review-plan.json"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_path, compact)
        print(f"紧凑 plan 识图记录: {output_path}")
        return 0
    if (args.snapshot or args.output or args.series_name or args.season_group
            or args.movie_group or args.specials_group):
        parser.error("--snapshot/--output/--series-name/--*-group 只能与 --build-request 一起使用")
    if not args.input:
        parser.error("生成候选时必须提供 --input")
    if args.plan_review_out:
        parser.error("--plan-review-out 只能与 --compact-review 一起使用")
    request_payload = json.loads(
        Path(normalize_path(args.input)).read_text(encoding="utf-8")
    )
    cfg = load_config()
    cache_enabled = artwork_cache_enabled(cfg)
    if args.original_cache_root and not cache_enabled:
        parser.error(
            "config.artwork.artwork_cache=false 时不能使用 --original-cache-root；"
            "请先开启该配置项"
        )
    original_cache_root = (
        args.original_cache_root
        if args.original_cache_root
        else cache_dir(cfg, "artwork-originals")
    ) if cache_enabled else None
    review = build_review(
        request_payload,
        args.output_dir or _default_output_dir(),
        candidate_limit=args.candidate_limit,
        multimodal_enabled=multimodal_artwork_review_enabled(cfg),
        preview_cache_dir=cache_dir(cfg, "artwork-preview"),
        original_cache_root=original_cache_root,
    )
    print(f"识图候选: {len(review['groups'])} 组；"
          f"{sum(len(group['candidates']) for group in review['groups'])} 张；"
          f"contact sheet {len(review['sheets'])} 张")
    if review["preview_cache"]["enabled"]:
        print(f"预览缓存: 命中 {review['preview_cache']['hits']}；"
              f"下载 {review['preview_cache']['misses']}")
    if review.get("original_cache_dir"):
        print(f"原图缓存目录: {review['original_cache_dir']}")
    elif (review.get("original_cache") or {}).get("status") == "failed":
        print("原图缓存未完成（不影响本次刮削）", file=sys.stderr)
    for warning in review.get("warnings") or []:
        print(f"  ! {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

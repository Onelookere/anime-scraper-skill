"""从元数据快照 + manifest 预生成 plan 骨架(确定性字段),语义字段留给 Agent。

定位:阶段二的省力工具,不是决策者。本脚本只填"有唯一确定答案"的字段:
  - plan_schema/type/library_projection(config 合成状态快照);
  - show 级:bgm_id/anidb_aid/premiered/rating/studio/plot(Bangumi 原文)/
    actors+staff_note+staff_status+staff_audit(复用 match.populate_show_staff);
  - 正片 episodes:集号、标题、airdate、runtime、Bangumi desc,以及
    manifest 中集号唯一对应时的 video_path;
  - 每个已匹配视频的同 stem thumb artwork(method=frame,library_relpath 留待 Agent)。

以下永远不填、列入 agent_todo 由 Agent 完成:
  - 认番确认、sorttitle 系列前缀、tmdb_identity/tmdb_match_status 认证;
  - 特殊集语义匹配与 S00E 分配(manifest 特殊件仅整理进 worksheet);
  - 空 plot 的四级回退与 plot_evidence、OP/ED song_evidence;
  - 海报选择与 artwork_review、库侧 library_relpath。

产出的草稿带顶层 ``scaffold`` 标记;scrape.py 在 dry-run 与实际落盘一律拒绝
仍含该标记的 plan,Agent 完成 agent_todo 并删除 scaffold 块后才可进入 dry-run。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import match
from _common import (
    atomic_write_json, ensure_utf8_stdout, hardlink_library_enabled,
    link_root, load_config, normalize_path,
)

SCAFFOLD_SCHEMA = "anime-scraper-plan-scaffold-v1"
PLAN_SCHEMA = "anime-scraper-plan"


def _parse_runtime_minutes(duration: str | None) -> int | None:
    """Bangumi duration('00:24:00' / '24m')→分钟;解析不了返回 None。"""
    if not duration:
        return None
    m = re.match(r"^(\d+):(\d+):(\d+)$", str(duration).strip())
    if m:
        total = int(m.group(1)) * 60 + int(m.group(2)) + (1 if int(m.group(3)) >= 30 else 0)
        return total or None
    m = re.match(r"^(\d+)\s*m", str(duration).strip(), re.IGNORECASE)
    if m:
        return int(m.group(1)) or None
    return None


def _episode_number(ep: dict) -> int | None:
    """取 Bangumi 正片集号:优先 ep,回退 sort;非整数(如 13.5)返回 None。"""
    for key in ("ep", "sort"):
        value = ep.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number == int(number):
            return int(number)
    return None


def _select_unit_files(manifest: dict, unit_dirs: list[str]) -> list[dict]:
    """按 --unit-dir 前缀过滤 manifest 文件;不传则取全部。"""
    files = manifest.get("files") or []
    if not unit_dirs:
        return list(files)
    prefixes = [p.replace("\\", "/").strip("/") for p in unit_dirs]
    selected = []
    for rec in files:
        rel = str(rec.get("rel_path", "")).replace("\\", "/")
        if any(rel == p or rel.startswith(p + "/") for p in prefixes):
            selected.append(rec)
    return selected


def _split_unit_files(files: list[dict]) -> tuple[dict[int, list[dict]], list[dict]]:
    """(正片集号→文件列表, 特殊/未知件列表)。判定只用 manifest 既有 hint,不重扫。"""
    normals: dict[int, list[dict]] = {}
    others: list[dict] = []
    for rec in files:
        hint = rec.get("hint") or {}
        number = hint.get("episode_number")
        if number is not None and not hint.get("is_special") and not rec.get("subdir_special_hint"):
            normals.setdefault(int(number), []).append(rec)
        else:
            others.append(rec)
    return normals, others


def _worksheet_entry(rec: dict) -> dict:
    """特殊/未知件的工作底稿条目;只保留 Agent 语义匹配需要的既有事实。"""
    hint = rec.get("hint") or {}
    return {
        "rel_path": rec.get("rel_path"),
        "stem": rec.get("stem"),
        "duration": rec.get("duration"),
        "episode_type": hint.get("episode_type"),
        "special_tag": hint.get("special_tag") or hint.get("special_type"),
        "subdir_special_hint": bool(rec.get("subdir_special_hint")),
    }


def _library_projection(cfg: dict) -> dict:
    enabled = hardlink_library_enabled(cfg)
    root = link_root(cfg, required=False) if enabled else None
    return {"hardlinks_enabled": enabled, "link_root": str(root) if root else None}


def _scaffold_show(subject: dict, args, todo: list[str]) -> dict:
    info = subject.get("subject") or {}
    rating = info.get("rating") or {}
    score = rating.get("score")
    show: dict = {
        "title": info.get("name_cn") or info.get("name") or "",
        "sorttitle": "",
        "plot": (info.get("summary") or "").strip(),
        "premiered": info.get("date") or "",
        "studio": match.pick_studio(info.get("infobox") or {}),
        "actors": [],
        "bgm_id": args.bgm_id,
        "lockdata": True,
    }
    if args.anidb_aid:
        show["anidb_aid"] = args.anidb_aid
    if isinstance(score, (int, float)) and score > 0 and (rating.get("total") or 0) > 0:
        show["rating"] = round(float(score), 1)
    match.populate_show_staff(
        show, subject.get("persons") or [], characters=subject.get("characters") or [])
    todo.append("确认 title/认番结论,并按 metadata-rules §3 审查 sorttitle 系列前缀(当前为空)")
    if show["plot"]:
        todo.append("审查作品 plot:剥离混入的 staff 文本,staff 行只经 staff_note 输出")
    if not show["studio"]:
        todo.append("studio 未能从 infobox 确定:按 metadata-rules §1 用 persons 公司实体归一化或留空记账")
    return show


def _scaffold_episodes(subject: dict, tmdb_season: dict | None,
                       normals: dict[int, list[dict]], manifest_root: Path,
                       todo: list[str], worksheet: dict) -> tuple[list[dict], list[dict]]:
    """正片 episodes + 对应 thumb artwork;只收录本地有唯一对应文件的集。"""
    tmdb_by_number: dict[int, dict] = {}
    for ep in (tmdb_season or {}).get("episodes") or []:
        number = ep.get("episode_number")
        if isinstance(number, int):
            tmdb_by_number[number] = ep

    episodes: list[dict] = []
    artwork: list[dict] = []
    matched_numbers: set[int] = set()
    for ep in subject.get("episodes") or []:
        if ep.get("type_id") != 0:
            continue
        number = _episode_number(ep)
        if number is None or number not in normals:
            continue
        candidates = normals[number]
        if len(candidates) != 1:
            worksheet.setdefault("ambiguous_episodes", []).append({
                "episode": number,
                "files": [rec.get("rel_path") for rec in candidates],
            })
            continue
        matched_numbers.add(number)
        video = manifest_root / str(candidates[0]["rel_path"])
        plot = (ep.get("desc") or "").strip()
        plot_source = "bangumi_desc" if plot else ""
        if not plot and number in tmdb_by_number:
            plot = (tmdb_by_number[number].get("overview") or "").strip()
            plot_source = "tmdb_overview" if plot else ""
        entry = {
            "category": "normal",
            "season": 1,
            "episode": number,
            "title": ep.get("name_cn") or ep.get("name") or f"第{number}话",
            "plot": plot,
            "airdate": ep.get("airdate") or "",
            "tmdb_match_status": "unknown",
            "video_path": str(video),
        }
        runtime = _parse_runtime_minutes(ep.get("duration"))
        if runtime:
            entry["runtime"] = runtime
        if plot_source:
            entry["plot_source"] = plot_source
        else:
            todo.append(f"第 {number} 话 plot 为空:按 4.3.3 完成四级回退并填写 plot_evidence")
        episodes.append(entry)
        artwork.append({
            "scope": "episode",
            "kind": "thumb",
            "source_path": str(video.with_name(video.stem + "-thumb.jpg")),
            "library_relpath": "",
            "method": "frame",
            "fallback_video_path": str(video),
        })

    unmatched_files = sorted(
        rec.get("rel_path")
        for number, records in normals.items() if number not in matched_numbers
        for rec in records
    )
    if unmatched_files:
        worksheet["unmatched_normal_files"] = unmatched_files
        todo.append("处理未对齐正片文件(集号超出 Bangumi 表或多版本):见 worksheet.unmatched_normal_files")
    return episodes, artwork


def scaffold_tv(snapshot: dict, manifest: dict, args, cfg: dict) -> dict:
    subject = (snapshot.get("bangumi") or {}).get("subjects", {}).get(str(args.bgm_id))
    if not subject:
        raise ValueError(f"快照中没有 bgm_id={args.bgm_id} 的 Bangumi 数据;先跑 metadata_snapshot.py")

    tmdb_season = None
    if args.tmdb_tv_id and args.tmdb_main_season is not None:
        tmdb_season = (((snapshot.get("tmdb") or {}).get("tv") or {})
                       .get(str(args.tmdb_tv_id), {})
                       .get("seasons", {}).get(str(args.tmdb_main_season)))

    todo: list[str] = []
    worksheet: dict = {}
    files = _select_unit_files(manifest, args.unit_dir)
    if not files:
        raise ValueError("manifest 中没有命中本单元的文件;检查 --unit-dir 前缀")
    normals, others = _split_unit_files(files)
    manifest_root = Path(normalize_path(manifest.get("root") or ""))

    show = _scaffold_show(subject, args, todo)
    episodes, artwork = _scaffold_episodes(
        subject, tmdb_season, normals, manifest_root, todo, worksheet)
    if others:
        worksheet["special_files"] = [_worksheet_entry(rec) for rec in others]
        todo.append("按 special-rules §3 完成 worksheet.special_files 的语义匹配与 S00E 分配")

    todo.append("完成 TMDB 认证:tmdb_identity/tmdb_match_status,以及 4.7a 海报选择与 artwork_review")
    todo.append("为全部 artwork 填写 library_relpath,并补作品级海报条目")
    if any(ep.get("category") == "credit" for ep in episodes):
        todo.append("为全部入库 credit 完成 4.6 查证并填写 song_evidence")

    plan = {
        "plan_schema": PLAN_SCHEMA,
        "type": "tv",
        "output_dir": "",
        "library_projection": _library_projection(cfg),
        "refresh_artwork": False,
        "show": show,
        "episodes": episodes,
        "artwork": artwork,
        "scaffold": {
            "schema": SCAFFOLD_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent_todo": ["确认 output_dir(单元独占片源根时直接用该根;多单元共享根时用 <片源根>/meta/<单元>/)"] + todo,
            "worksheet": worksheet,
        },
    }
    return plan


APPLY_SCHEMA = "anime-scraper-todo-apply-v1"

_EPISODE_PATCH_FIELDS = (
    "title", "plot", "plot_evidence", "tmdb_match_status", "tmdb_still_url",
    "video_path", "runtime", "airdate", "song_evidence",
)
_SPECIAL_OPTIONAL_FIELDS = (
    "plot", "runtime", "anidb_epno", "anidb_type", "special_order",
    "tmdb_match_status", "song_evidence",
)
_PLOT_EVIDENCE_FIELDS = ("bangumi_zh", "tmdb_zh", "bangumi_ja", "tmdb_en")
_POSTER_SPECS = {
    # posters 键 → (artwork kind, 源侧文件名, 库侧相对路径列表)
    # kind 必须显式声明：它是 plan 契约字段,不等于源侧文件名。Specials 海报的
    # 源文件名是 specials-poster.jpg,但 kind 仍是 poster——识图护栏
    # (scrape._validate_artwork_review) 与快捷替换 (update_artwork) 都按
    # kind == "poster" 取件,派生文件名会让它们静默漏掉这张图。
    "main_poster": ("poster", "poster.jpg", ("poster.jpg",)),
    "specials_poster": ("poster", "specials-poster.jpg", ("Specials/poster.jpg",)),
    "fanart": ("fanart", "fanart.jpg", ("fanart.jpg",)),
    "banner": ("banner", "banner.jpg", ("banner.jpg",)),
}


def _episode_base_name(title: str, season: int, episode: int) -> str:
    """复用 link_library._episode_base 的库侧 stem，避免与建库规则漂移。"""
    from types import SimpleNamespace
    from link_library import _episode_base
    return _episode_base(
        SimpleNamespace(title=title),
        SimpleNamespace(season=season, episode=episode),
    )


def _season_dir(season: int) -> str:
    return "Specials" if season == 0 else f"Season {season:02d}"


def _thumb_artwork(video_path: str, *, method: str, url: str | None,
                   title: str, season: int, episode: int) -> dict:
    video = Path(video_path)
    entry = {
        "scope": "episode",
        "kind": "thumb",
        "source_path": str(video.with_name(video.stem + "-thumb.jpg")),
        "library_relpath": f"{_season_dir(season)}/{_episode_base_name(title, season, episode)}-thumb.jpg",
        "method": method,
    }
    if method == "tmdb":
        entry["url"] = url
    else:
        entry["fallback_video_path"] = str(video)
    return entry


def _require_keys(entry: dict, keys: tuple[str, ...], where: str) -> None:
    missing = [key for key in keys if not str(entry.get(key) or "").strip()]
    if missing:
        raise ValueError(f"{where} 缺少必填字段: {', '.join(missing)}")


def apply_todo(plan: dict, manifest: dict, answers: dict) -> tuple[dict, list[str]]:
    """把 Agent 的语义决定 sidecar 合并进草稿并删除 scaffold 块。

    Agent 只写小体积 answers JSON(语义字段),不再 Read+Edit 整份大 plan;
    机械部分由本函数完成:S00E 条目、thumb artwork、library_relpath、
    作品级海报条目、artwork_review 嵌入、scaffold 删除与覆盖率校验。
    返回 ``(最终 plan, 摘要行)``;草稿原文件不被修改。
    """
    if answers.get("apply_schema") != APPLY_SCHEMA:
        raise ValueError(f"answers 必须声明 apply_schema: {APPLY_SCHEMA}")
    scaffold = plan.get("scaffold")
    if not isinstance(scaffold, dict):
        raise ValueError("plan 草稿缺少 scaffold 块;--apply-todo 只处理骨架草稿")
    worksheet = scaffold.get("worksheet") or {}
    notes: list[str] = []
    manifest_root = Path(normalize_path(manifest.get("root") or ""))
    if not manifest_root.name:
        raise ValueError("manifest 缺少 root,无法把 rel_path 解析为绝对路径")

    output_dir = str(answers.get("output_dir") or "").strip()
    if not output_dir:
        raise ValueError("answers.output_dir 必填(单元独占片源根时直接用该根)")
    if output_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower() in {
            "meta", "metadata"}:
        raise ValueError("output_dir 末级目录不得是 meta/metadata;单元独占片源根时直接用该根")
    plan["output_dir"] = output_dir

    show = plan["show"]
    for field in ("title", "sorttitle", "plot", "studio"):
        value = answers.get(field)
        if isinstance(value, str) and value.strip():
            show[field] = value.strip()
    if not str(show.get("sorttitle") or "").strip():
        raise ValueError("answers.sorttitle 必填;无法可靠确认系列时使用完整 title")
    if answers.get("tmdb_identity") is not None:
        plan["tmdb_identity"] = answers["tmdb_identity"]
    title = str(show["title"])

    episodes: list[dict] = list(plan.get("episodes") or [])
    artwork: list[dict] = list(plan.get("artwork") or [])
    existing_numbers = {
        ep["episode"] for ep in episodes if isinstance(ep.get("episode"), int)
    }

    # 正片补丁:只允许白名单字段,防拼错静默丢失。
    for patch in answers.get("episode_patches") or []:
        number = patch.get("episode")
        if not isinstance(number, int):
            raise ValueError(f"episode_patches 必须用整数集号定位: {patch}")
        unknown = set(patch) - {"episode", *_EPISODE_PATCH_FIELDS}
        if unknown:
            raise ValueError(f"第 {number} 话补丁含未知字段: {sorted(unknown)}")
        target = next(
            (ep for ep in episodes
             if ep.get("season", 1) != 0 and ep.get("episode") == number), None)
        if target is None:
            raise ValueError(f"第 {number} 话不在草稿正片中,无法打补丁;新增集用 add_episodes")
        for key, value in patch.items():
            if key != "episode":
                target[key] = value
        if str(target.get("video_path") or "") and str(target.get("tmdb_still_url") or ""):
            for item in artwork:
                if (item.get("scope") == "episode" and item.get("method") == "frame"
                        and item.get("fallback_video_path") == target["video_path"]):
                    item["method"] = "tmdb"
                    item["url"] = target["tmdb_still_url"]
                    item.pop("fallback_video_path", None)
        notes.append(f"正片补丁: 第 {number} 话")

    # 补入正片(集号冲突文件择一、集号超出 Bangumi 表)。
    # worksheet 存平台原生分隔符(Windows 为反斜杠),统一归一化后再与 answers 匹配。
    ambiguous = [
        {**group, "files": [str(rec).replace("\\", "/") for rec in group.get("files") or []]}
        for group in worksheet.get("ambiguous_episodes") or []
    ]
    unmatched = [
        str(rec).replace("\\", "/")
        for rec in worksheet.get("unmatched_normal_files") or []
    ]
    allowed_added = {
        str(rec) for group in ambiguous for rec in group.get("files") or []
    } | {str(rec) for rec in unmatched}
    resolved_ambiguous = set()
    for added in answers.get("add_episodes") or []:
        _require_keys(added, ("rel_path", "episode", "title", "airdate"),
                      f"add_episodes[{added.get('rel_path', '?')}]")
        rel_path = str(added["rel_path"]).replace("\\", "/")
        if rel_path not in allowed_added:
            raise ValueError(
                f"add_episodes 的 {rel_path} 不在 worksheet 冲突/未对齐清单中;"
                "已有唯一集号的文件不需要 add_episodes"
            )
        number = added["episode"]
        if number in existing_numbers:
            raise ValueError(f"add_episodes 集号 {number} 与草稿正片冲突")
        video = str(manifest_root / rel_path)
        entry = {
            "category": "normal", "season": 1, "episode": number,
            "title": added["title"], "plot": str(added.get("plot") or ""),
            "airdate": added["airdate"], "tmdb_match_status": "unknown",
            "video_path": video,
        }
        if added.get("runtime"):
            entry["runtime"] = added["runtime"]
        if added.get("plot_evidence"):
            entry["plot_evidence"] = added["plot_evidence"]
        episodes.append(entry)
        existing_numbers.add(number)
        artwork.append(_thumb_artwork(
            video, method="frame", url=None, title=title, season=1, episode=number))
        for group in ambiguous:
            if group.get("episode") == number and rel_path in (group.get("files") or []):
                resolved_ambiguous.add(group.get("episode"))
        notes.append(f"补入正片: 第 {number} 话 ← {rel_path}")
    unresolved_ambiguous = [
        group["episode"] for group in ambiguous
        if group["episode"] not in resolved_ambiguous
    ]
    if unresolved_ambiguous:
        raise ValueError(
            f"worksheet.ambiguous_episodes 未解决(集号 {unresolved_ambiguous});"
            "用 add_episodes 为每个冲突集号择一文件"
        )
    skipped_normal = {
        str(item.get("rel_path") or "").replace("\\", "/"): str(item.get("reason") or "").strip()
        for item in answers.get("skipped_normal") or []
    }
    for rel_path, reason in skipped_normal.items():
        if rel_path not in unmatched:
            raise ValueError(f"skipped_normal 的 {rel_path} 不在未对齐清单中")
        if not reason:
            raise ValueError(f"skipped_normal 的 {rel_path} 必须记录略过理由")
        notes.append(f"略过: {rel_path}（{reason}）")
    left_unmatched = [
        rel for rel in unmatched
        if str(rel) not in {
            str(added["rel_path"]).replace("\\", "/")
            for added in answers.get("add_episodes") or []
        } and str(rel) not in skipped_normal
    ]
    if left_unmatched:
        raise ValueError(
            f"worksheet.unmatched_normal_files 未处理: {left_unmatched};"
            "用 add_episodes 入库,或用 skipped_normal 记录理由后由人工处理"
        )

    # Specials:语义匹配结论 → Season 0 条目 + thumb artwork。
    worksheet_specials = {
        str(rec.get("rel_path") or "").replace("\\", "/"): rec
        for rec in worksheet.get("special_files") or []
    }
    used_rel_paths: set[str] = set()

    def _worksheet_file(rel_path: str) -> str:
        normalized = str(rel_path).replace("\\", "/")
        if normalized not in worksheet_specials:
            raise ValueError(f"{normalized} 不在 worksheet.special_files 中")
        if normalized in used_rel_paths:
            raise ValueError(f"{normalized} 被重复处理")
        used_rel_paths.add(normalized)
        return str(manifest_root / normalized)

    for skipped in answers.get("skipped_specials") or []:
        rel_path = str(skipped.get("rel_path") or "").replace("\\", "/")
        video = _worksheet_file(rel_path)
        reason = str(skipped.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"skipped_specials 的 {rel_path} 必须记录略过理由")
        notes.append(f"略过: {rel_path}（{reason}）")

    specials_count = len(answers.get("specials") or [])
    used_special_numbers: set[int] = set()
    for special in answers.get("specials") or []:
        rel_path = str(special.get("rel_path") or "").replace("\\", "/")
        where = f"specials[{rel_path or '?'}]"
        _require_keys(special, ("rel_path", "episode", "title", "airdate"), where)
        video = _worksheet_file(rel_path)
        number = special["episode"]
        if not isinstance(number, int) or number < 1 or number in used_special_numbers:
            raise ValueError(f"{where} 的 S00E 编号非法或重复: {number}")
        used_special_numbers.add(number)
        unknown = set(special) - {
            "rel_path", "episode", "title", "airdate", "thumb",
            "null_video_reason", "category", *_SPECIAL_OPTIONAL_FIELDS,
        }
        if unknown:
            raise ValueError(f"{where} 含未知字段: {sorted(unknown)}")
        null_reason = str(special.get("null_video_reason") or "").strip()
        entry = {
            "category": special.get("category") or "special",
            "season": 0,
            "episode": number,
            "title": special["title"],
            "plot": str(special.get("plot") or ""),
            "airdate": special["airdate"],
            "tmdb_match_status": special.get("tmdb_match_status") or "unknown",
        }
        if null_reason:
            entry["video_path"] = None
            entry["skip_reason"] = null_reason
        else:
            entry["video_path"] = video
        for field in _SPECIAL_OPTIONAL_FIELDS:
            if special.get(field) is not None and field != "tmdb_match_status":
                entry[field] = special[field]
        if entry["category"] == "credit" and not special.get("song_evidence"):
            raise ValueError(f"{where} 是 credit,必须完成 4.6 查证并填写 song_evidence")
        if specials_count > 1 and not special.get("special_order"):
            raise ValueError(
                f"{where} 缺 special_order;Season 0 多条入库时每条都要填排序键")
        episodes.append(entry)
        if not null_reason:
            thumb = special.get("thumb") or {}
            method = str(thumb.get("method") or "frame")
            if method not in {"frame", "tmdb"}:
                raise ValueError(f"{where} thumb.method 只能是 frame/tmdb")
            url = str(thumb.get("url") or "") or None
            if method == "tmdb" and not url:
                raise ValueError(f"{where} thumb.method=tmdb 必须提供 url")
            if method == "tmdb":
                entry["tmdb_still_url"] = url
            artwork.append(_thumb_artwork(
                video, method=method, url=url, title=title, season=0, episode=number))
        notes.append(
            f"Special: S00E{number:02d} {entry['title']} ← {rel_path}")

    missing_coverage = sorted(set(worksheet_specials) - used_rel_paths)
    if missing_coverage:
        raise ValueError(
            f"worksheet.special_files 未全部处理: {missing_coverage};"
            "每份文件要么进 specials,要么进 skipped_specials(六类略过)"
        )

    # 作品级海报与其它作品图;source 固定在 output_dir,库侧投影按规则表。
    posters = answers.get("posters") or {}
    for key, (kind, source_name, library_paths) in _POSTER_SPECS.items():
        spec = posters.get(key)
        if not spec:
            continue
        method = str(spec.get("method") or "tmdb")
        if method != "tmdb" or not str(spec.get("url") or ""):
            raise ValueError(f"posters.{key} 目前只支持 method=tmdb + url")
        projections = list(library_paths)
        if key == "main_poster" and spec.get("project_season", True):
            projections.append("Season 01/poster.jpg")
        for library_path in projections:
            entry = {
                "scope": "show", "kind": kind,
                "source_path": str(Path(output_dir) / source_name),
                "library_relpath": library_path,
                "method": "tmdb", "url": spec["url"],
            }
            if key == "specials_poster":
                selection = str(spec.get("specials_selection") or "").strip()
                if not selection:
                    raise ValueError("posters.specials_poster 必须带 specials_selection")
                if library_path == "Specials/poster.jpg":
                    entry["specials_selection"] = selection
            artwork.append(entry)
        notes.append(f"作品图: {key} → {'、'.join(projections)}")
    if posters.get("clearlogo"):
        spec = posters["clearlogo"]
        suffix = ".svg" if str(spec.get("url", "")).lower().endswith(".svg") else ".png"
        artwork.append({
            "scope": "show", "kind": "clearlogo",
            "source_path": str(Path(output_dir) / f"clearlogo{suffix}"),
            "library_relpath": f"clearlogo{suffix}",
            "method": "tmdb", "url": spec["url"],
        })
        notes.append(f"作品图: clearlogo{suffix}")

    for extra in answers.get("extra_artwork") or []:
        artwork.append(dict(extra))

    review_path = answers.get("artwork_review_plan")
    if review_path:
        review = json.loads(
            Path(normalize_path(review_path)).read_text(encoding="utf-8"))
        plan["artwork_review"] = review
        notes.append("artwork_review: 已嵌入紧凑审查记录")
    elif isinstance(answers.get("artwork_review"), dict):
        plan["artwork_review"] = answers["artwork_review"]
        notes.append("artwork_review: 已嵌入内联记录")

    # 回填草稿正片 thumb 的空 library_relpath(库侧 stem 与建库规则同源)。
    thumb_owners = {}
    for ep in episodes:
        if ep.get("video_path"):
            video = Path(str(ep["video_path"]))
            thumb_owners[str(video.with_name(video.stem + "-thumb.jpg"))] = ep
    for item in artwork:
        if item.get("scope") == "episode" and not str(item.get("library_relpath") or ""):
            episode = thumb_owners.get(str(item.get("source_path") or ""))
            if episode is None:
                raise ValueError(
                    f"artwork {item.get('source_path')} 找不到对应 episode;"
                    "检查 source_path 是否为视频同 stem 的 -thumb.jpg"
                )
            season = episode.get("season", 1)
            item["library_relpath"] = (
                f"{_season_dir(season)}/"
                f"{_episode_base_name(title, season, episode['episode'])}-thumb.jpg"
            )

    # 收尾校验:空 plot 证据、credit 歌证、scaffold 删除。
    unfinished_plots = []
    for ep in episodes:
        if not ep.get("video_path"):
            continue
        if str(ep.get("plot") or "").strip():
            continue
        evidence = ep.get("plot_evidence") or {}
        complete = (
            ep.get("season") == 0
            or (all(evidence.get(field) == "empty" for field in _PLOT_EVIDENCE_FIELDS)
                and set(evidence) == set(_PLOT_EVIDENCE_FIELDS))
        )
        if not complete:
            unfinished_plots.append(
                f"第 {ep['episode']} 话" if ep.get("season") != 0
                else f"S00E{ep['episode']:02d}")
    if unfinished_plots:
        raise ValueError(
            f"正片空 plot 未完成四级回退/证据: {unfinished_plots};"
            "补 plot 或 plot_evidence(四项全 empty)"
        )
    plan["episodes"] = episodes
    plan["artwork"] = artwork
    plan.pop("scaffold", None)
    return plan, notes


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-todo", action="store_true",
                        help="把 Agent 的 answers sidecar 合并进草稿并删除 scaffold 块")
    parser.add_argument("--plan", help="草稿 plan JSON(--apply-todo 用)")
    parser.add_argument("--answers", help="Agent 语义决定 sidecar JSON(--apply-todo 用)")
    parser.add_argument("--snapshot", help="metadata_snapshot.py 产出的快照 JSON")
    parser.add_argument("--manifest", required=True, help="identify.py 产出的 manifest JSON")
    parser.add_argument("--output", required=True, help="输出路径(草稿或最终 plan)")
    parser.add_argument("--type", choices=["tv"], default="tv",
                        help="v1 只支持 tv;movie plan 结构简单,仍由 Agent 直接编写")
    parser.add_argument("--bgm-id", type=int, help="已确认的 Bangumi subject ID")
    parser.add_argument("--anidb-aid", type=int, default=None, help="已确认的 AniDB aid")
    parser.add_argument("--tmdb-tv-id", type=int, default=None,
                        help="快照中已存在的 TMDB TV ID,用于正片 plot 回退")
    parser.add_argument("--tmdb-main-season", type=int, default=None,
                        help="正片对应的 TMDB 季号(配合 --tmdb-tv-id)")
    parser.add_argument("--unit-dir", action="append", default=[],
                        help="本单元在 manifest 中的相对目录前缀,可重复;省略则取全部文件")
    args = parser.parse_args(argv)
    if args.apply_todo:
        if args.snapshot or args.bgm_id or args.anidb_aid or args.unit_dir:
            parser.error("--apply-todo 只需要 --plan/--answers/--manifest/--output")
        for required in ("plan", "answers"):
            if not getattr(args, required):
                parser.error(f"--apply-todo 需要 --{required.replace('_', '-')}")
    else:
        if args.plan or args.answers:
            parser.error("--plan/--answers 只能与 --apply-todo 一起使用")
        if not args.bgm_id or not args.snapshot:
            parser.error("生成草稿需要 --snapshot 与 --bgm-id")
    return args


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    args = _parse_args(argv)
    manifest_raw = json.loads(Path(normalize_path(args.manifest)).read_text(encoding="utf-8"))
    manifest = manifest_raw.get("scan")
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须是 identify.py 输出且包含 scan 对象")

    if args.apply_todo:
        plan = json.loads(Path(normalize_path(args.plan)).read_text(encoding="utf-8"))
        answers = json.loads(
            Path(normalize_path(args.answers)).read_text(encoding="utf-8"))
        final, notes = apply_todo(plan, manifest, answers)
        output = atomic_write_json(args.output, final)
        print(f"最终 plan: {output}（草稿 {args.plan} 保留为审查证据）")
        for note in notes:
            print(f"  {note}")
        specials = sum(1 for ep in final["episodes"] if ep.get("season") == 0)
        print(f"  正片 {len(final['episodes']) - specials} 集;Specials {specials} 条;"
              f"artwork {len(final['artwork'])} 项;scaffold 块已删除")
        return 0

    snapshot = json.loads(Path(normalize_path(args.snapshot)).read_text(encoding="utf-8"))
    cfg = load_config()
    plan = scaffold_tv(snapshot, manifest, args, cfg)
    output = atomic_write_json(args.output, plan)

    scaffold = plan["scaffold"]
    empty_plots = sum(1 for ep in plan["episodes"] if not ep.get("plot"))
    print(f"plan 骨架完成: {output}")
    print(f"  正片 {len(plan['episodes'])} 集(空 plot {empty_plots});"
          f"特殊/未知件 {len(scaffold['worksheet'].get('special_files', []))} 个进 worksheet")
    print(f"  待 Agent 完成 {len(scaffold['agent_todo'])} 项(见草稿 scaffold.agent_todo);"
          "完成后写 answers sidecar 并用 --apply-todo 合并")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

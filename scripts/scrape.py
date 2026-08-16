"""将 agent 产出的 unit plan 写为源侧 NFO/图片，并可投影到扁平 Jellyfin 混合库。

图片物理实体只落源目录；配置启用时把视频/NFO/图片硬链接进库树。
流程：预检（零副作用）→ 源侧 NFO → 源侧图片实体化 →（可选）同卷硬链接
staging → 验证/提升。跨卷或硬链接失败一律停止，绝不复制。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import images
import link_library
import match
import nfo
import tmdb
from _common import (
    atomic_write_json,
    hardlink_library_enabled,
    link_root,
    multimodal_artwork_review_enabled,
    normalize_path,
)


PLAN_SCHEMA = "anime-scraper-plan"
ARTWORK_REVIEW_SCHEMA = "anime-scraper-artwork-review-v1"
SONG_EVIDENCE_SOURCE_KINDS = {"official", "wiki", "bangumi", "local_cd", "web", "network_error"}
SONG_EVIDENCE_WEB_KINDS = {"official", "wiki", "web", "network_error"}
PLOT_STAFF_LINE = re.compile(
    r"^\s*(?:【(?:制作|製作|staff)】|脚本|分[镜鏡]|演出|"
    r"(?:总|總|総)?作画[监監]督|人物设定|人物設定|美术监督|美術監督|"
    r"音响监督|音響監督|摄影监督|撮影監督|色彩设计|色彩設計|"
    r"系列构成|系列構成|制片人|製片人|制作人|製作人|音乐|音樂)\s*[:：]",
    re.IGNORECASE,
)
PLOT_STAFF_MARKER = re.compile(r"【(?:制作|製作|staff)】", re.IGNORECASE)
PLOT_EVIDENCE_FIELDS = (
    "bangumi_zh",
    "tmdb_zh",
    "bangumi_ja",
    "tmdb_en",
)
PLOT_EVIDENCE_STATES = {"empty", "present"}
STAFF_STATUS_STATES = {"empty", "present"}
SPECIAL_ORDER_FIELDS = ("priority", "series_order", "item_order", "source_index")
FORBIDDEN_OUTPUT_DIR_NAMES = {"meta", "metadata"}


def _load_plan(src: str) -> dict:
    if src == "-":
        return json.load(sys.stdin)
    return json.loads(Path(src).read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plan_original_cache_dir(plan: dict) -> str | None:
    """Return the successful artwork original-cache directory from a plan."""
    review = plan.get("artwork_review")
    if not isinstance(review, dict):
        return None
    value = str(review.get("original_cache_dir") or "").strip()
    return value or None


def _require_plan_schema(plan: dict) -> None:
    """plan 必须声明当前契约版本，否则在任何写入前拒绝。"""
    if not isinstance(plan, dict):
        raise ValueError("plan 根必须是对象")
    schema = plan.get("plan_schema")
    if type(schema) is not str or schema != PLAN_SCHEMA:
        raise ValueError(
            f"plan.plan_schema 必须为 {PLAN_SCHEMA}，实际为 {schema!r}"
        )
    if "scaffold" in plan:
        raise ValueError(
            "plan 仍含 plan_scaffold.py 的 scaffold 草稿标记：Agent 必须完成 "
            "scaffold.agent_todo 中的全部语义字段并删除 scaffold 块后才能 dry-run 或落盘"
        )


def _validate_plan_type(plan: dict) -> str:
    """只允许明确的 TV/movie 计划，禁止未知值默认为 TV。"""
    plan_type = plan.get("type")
    if type(plan_type) is not str or plan_type not in {"tv", "movie"}:
        raise ValueError("plan.type 必须明确为 tv 或 movie")
    return plan_type


def _validate_lockdata(plan: dict) -> dict:
    """Require Jellyfin lockdata to be explicit before any write."""
    subject = plan.get("movie") if plan.get("type") == "movie" else plan.get("show")
    if not isinstance(subject, dict):
        raise ValueError("plan 缺少 show/movie 对象，无法确认 lockdata")
    if subject.get("lockdata") is not True:
        raise ValueError("plan show/movie.lockdata 必须明确为 true")
    return {"status": "passed"}


def _sorttitle_script_mismatch(title: str, prefix: str) -> str | None:
    """title 首字符与前缀首字符的文字系统不一致（跨语言错位）时返回原因。

    例：title「白箱」首字符是中文，前缀 SHIROBAKO 首字符是拉丁字母——Jellyfin
    排序会落在字母区，与显示的首字错位。中文 title 必须用中文系列前缀，
    罗马字 title 必须用罗马字系列前缀；同属非 ASCII（如「剧场版 白箱」+
    「白箱」）或同属 ASCII 时不算错位。
    """
    if not title or not prefix:
        return None
    t, p = title[0], prefix[0]
    if t.isascii() != p.isascii():
        kind_t = "ASCII" if t.isascii() else "非 ASCII"
        kind_p = "ASCII" if p.isascii() else "非 ASCII"
        return (f"title 首字符 {t!r}({kind_t}) 与排序前缀首字符 {p!r}"
                f"({kind_p}) 文字系统不一致；中文 title 不得用罗马字系列前缀"
                "（如「白箱」不能用 SHIROBAKO 作排序前缀）")
    return None


def _validate_sorttitle(plan: dict) -> dict:
    """校验 sorttitle 的结构，不替 Agent 判断系列关系。

    从 ``sorttitle`` 末尾反向拆出 prefix，无需新增字段或联网请求。前缀可以是
    title 中的原文基底，也可以是 Agent 根据元数据和可靠知识确认的外部系列名；
    后者只返回语义审查提醒，不能由这个纯本地函数证实系列关系。
    """
    subject = plan.get("movie") if plan.get("type") == "movie" else plan.get("show")
    if not isinstance(subject, dict):
        raise ValueError("plan 缺少 show/movie 对象，无法校验 sorttitle")

    title = str(subject.get("title") or "")
    sorttitle = str(subject.get("sorttitle") or "")
    premiered = str(subject.get("premiered") or "")
    if not title or title != title.strip():
        raise ValueError("plan title 不能为空或带首尾空格")
    if not sorttitle or sorttitle != sorttitle.strip():
        raise ValueError("plan sorttitle 不能为空或带首尾空格")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", premiered):
        sort_date = premiered
    elif re.fullmatch(r"\d{4}", premiered):
        sort_date = f"{premiered}-01-01"
    elif not premiered:
        sort_date = "9999-12-31"
    else:
        raise ValueError("plan premiered 必须为 YYYY-MM-DD、YYYY 或省略")

    suffix = f" {sort_date} {title}"
    if not sorttitle.endswith(suffix):
        raise ValueError(
            "sorttitle 格式错误，必须为 {前缀} {排序日期} {最终 title}；"
            f"期望后缀={suffix!r}，实际={sorttitle!r}"
        )
    prefix = sorttitle[:-len(suffix)]
    if not prefix:
        raise ValueError("sorttitle 缺少标题前缀")
    mismatch = _sorttitle_script_mismatch(title, prefix)
    if mismatch:
        raise ValueError(f"sorttitle 文字系统错位：{mismatch}")
    prefix_in_title = prefix in title
    return {
        "status": "passed",
        "prefix": prefix,
        "sort_date": sort_date,
        "prefix_in_title": prefix_in_title,
        "semantic_review_required": not prefix_in_title,
    }


def _validate_song_evidence(plan: dict) -> dict:
    """阻止 OP/ED 在证据未穷尽时直接落为猜测或裸标题。"""
    entries_key = "extras" if plan.get("type") == "movie" else "episodes"
    credits = [
        entry for entry in plan.get(entries_key) or []
        if isinstance(entry, dict)
        and entry.get("category") == "credit"
        and entry.get("video_path")
    ]
    if not credits:
        return {"status": "not_required", "credit_count": 0}
    counts = Counter()
    for entry in credits:
        label = str(entry.get("title") or "credit")
        evidence = entry.get("song_evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"{label} 缺少 song_evidence，不能写入 OP/ED")
        status = evidence.get("status")
        if status not in {"resolved", "exhausted"}:
            raise ValueError(f"{label} song_evidence.status 必须为 resolved/exhausted")
        sources = evidence.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError(f"{label} song_evidence.sources 必须是非空列表")
        if any(
            not isinstance(source, str) or ":" not in source
            or source.split(":", 1)[0] not in SONG_EVIDENCE_SOURCE_KINDS
            or not source.split(":", 1)[1].strip()
            for source in sources
        ):
            raise ValueError(f"{label} song_evidence source 必须为有效 kind:value")
        kinds = {source.split(":", 1)[0] for source in sources}
        note = str(evidence.get("note") or "").strip()
        if not note:
            raise ValueError(f"{label} song_evidence.note 必须记录对应关系或未解决原因")
        has_song_name = " - " in label
        if (status == "resolved") != has_song_name:
            raise ValueError(f"{label} 标题与 song_evidence.status 矛盾")
        if status == "exhausted" and not (kinds & SONG_EVIDENCE_WEB_KINDS):
            raise ValueError(f"{label} 裸 OP/ED 必须记录 Web 查询或 network_error")
        counts[status] += 1
    return {
        "status": "passed",
        "credit_count": len(credits),
        "resolved": counts["resolved"],
        "exhausted": counts["exhausted"],
    }


def _validate_tv_primary_content(plan: dict) -> dict:
    """TV 单元必须含正片；纯 Season 0 内容必须先并入母作计划。"""
    if plan.get("type") != "tv":
        return {"status": "not_applicable"}
    episodes = plan.get("episodes") or []
    primary = [entry for entry in episodes if isinstance(entry, dict)
               and entry.get("video_path")
               and entry.get("category") == "normal"
               and int(entry.get("season") or 0) >= 1]
    if not primary:
        raise ValueError(
            "纯 Season 0/SP 不能独立建立 TV 库条目；请按 workflow.md §6 "
            "合并到证据最强且最接近的已有正片单元"
        )
    return {"status": "passed", "primary_count": len(primary)}


def _primary_plot_entries(plan: dict) -> list[tuple[str, dict]]:
    """Return only primary media entries whose synopsis is required."""
    entries_key = "extras" if plan.get("type") == "movie" else "episodes"
    entries: list[tuple[str, dict]] = []
    if plan.get("type") == "movie":
        movie = plan.get("movie") or plan.get("show") or {}
        if isinstance(movie, dict) and movie.get("video_path"):
            entries.append((str(movie.get("title") or "电影正片"), movie))
    for entry in plan.get(entries_key) or []:
        if not isinstance(entry, dict) or not entry.get("video_path"):
            continue
        try:
            default_season = 1 if entry.get("category", "normal") == "normal" else 0
            season = int(entry.get("season") or default_season)
        except (TypeError, ValueError):
            continue
        if season <= 0:
            continue
        episode_value = entry.get("episode") or 0
        try:
            episode_number = int(episode_value)
        except (TypeError, ValueError):
            episode_number = 0
        label = entry.get("title") or f"S{season:02d}E{episode_number:02d}"
        entries.append((str(label), entry))
    return entries


def _plot_evidence_error(label: str, entry: dict) -> str | None:
    """Require an explicit all-empty source audit before omitting a primary plot."""
    evidence = entry.get("plot_evidence")
    if not isinstance(evidence, dict):
        return (
            f"{label} 的 plot 为空但缺少 plot_evidence；必须明确记录 "
            "Bangumi 中文、TMDB 中文、Bangumi 日文、TMDB 英文均为 empty"
        )
    expected = set(PLOT_EVIDENCE_FIELDS)
    actual = set(evidence)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        details = []
        if missing:
            details.append("缺少 " + ", ".join(sorted(missing)))
        if extra:
            details.append("未知字段 " + ", ".join(sorted(extra)))
        return f"{label} 的 plot_evidence 字段不完整（{'；'.join(details)}）"
    invalid = [
        field for field in PLOT_EVIDENCE_FIELDS
        if type(evidence[field]) is not str or evidence[field] not in PLOT_EVIDENCE_STATES
    ]
    if invalid:
        return (
            f"{label} 的 plot_evidence 只能使用 present/empty，非法字段: "
            + ", ".join(invalid)
        )
    present = [field for field in PLOT_EVIDENCE_FIELDS if evidence[field] == "present"]
    if present:
        return (
            f"{label} 的 plot 为空，但来源仍标记为 present: "
            + ", ".join(present)
        )
    return None


def _inspect_episode_plots(plan: dict) -> dict:
    """Inspect primary plot content without raising, for a complete dry-run report."""
    checked = 0
    empty_labels: list[str] = []
    exhausted_labels: list[str] = []
    errors: list[str] = []
    for label, entry in _primary_plot_entries(plan):
        checked += 1
        plot = str(entry.get("plot") or "")
        if _plot_has_staff_line(plot):
            errors.append(
                f"{label} 的 plot 含 staff 字段；先剥离 staff，清洗后为空则按简介优先级回退"
            )
            continue
        if plot.strip():
            continue
        empty_labels.append(label)
        error = _plot_evidence_error(label, entry)
        if error:
            errors.append(error)
        else:
            exhausted_labels.append(label)
    return {
        "status": "failed" if errors else "passed",
        "checked_count": checked,
        "empty_count": len(empty_labels),
        "empty_labels": empty_labels,
        "exhausted_labels": exhausted_labels,
        "errors": errors,
    }


def _validate_episode_plots(plan: dict) -> dict:
    """Reject staff metadata and unproven empty primary plots before writes."""
    result = _inspect_episode_plots(plan)
    if result["status"] != "passed":
        errors = result["errors"]
        rendered = "；".join(errors[:10])
        if len(errors) > 10:
            rendered += "；…"
        raise ValueError("正片 plot 校验失败：" + rendered)
    return result


def _plot_has_staff_line(plot: str) -> bool:
    """Detect explicit staff labels without attempting semantic plot cleaning."""
    return any(
        PLOT_STAFF_LINE.match(line) or PLOT_STAFF_MARKER.search(line)
        for line in str(plot or "").splitlines()
    )


def _compact_plot_text(value: str) -> str:
    """Compare plan text across ordinary whitespace and line wrapping."""
    return re.sub(r"\s+", "", str(value or ""))


def _inspect_show_plot(plan: dict) -> dict:
    """Reject staff metadata that would otherwise become a user-facing plot."""
    section = plan.get("movie") if plan.get("type") == "movie" else plan.get("show")
    if not isinstance(section, dict):
        return {"status": "failed", "errors": ["plan 缺少 show/movie 对象"]}

    label = str(section.get("title") or ("电影" if plan.get("type") == "movie" else "作品"))
    plot = str(section.get("plot") or "")
    note = str(section.get("staff_note") or "").strip()
    errors: list[str] = []
    if _plot_has_staff_line(plot):
        errors.append(
            f"{label} 的 plot 含 staff 字段；staff_note 必须独立保存，写 NFO 时由生成器追加到简介末尾"
        )
    if note and _compact_plot_text(note) in _compact_plot_text(plot):
        errors.append(
            f"{label} 的 plot 与 staff_note 重叠；plot 只保留剧情，staff_note 由生成器在 NFO 末尾追加"
        )
    return {"status": "failed" if errors else "passed", "errors": errors}


def _validate_show_plot(plan: dict) -> dict:
    result = _inspect_show_plot(plan)
    if result["status"] != "passed":
        raise ValueError("作品级 plot 校验失败：" + "；".join(result["errors"]))
    return result


def _staff_note_mappable_roles(note: str) -> list[str]:
    """Return mapped Bangumi positions incorrectly written into staff_note."""
    found = []
    for role in match._CREW_KIND:
        pattern = rf"(?<![\w一-龥]){re.escape(role)}\s*[:：]"
        if re.search(pattern, note, re.IGNORECASE):
            found.append(role)
    return found


def _inspect_show_staff(plan: dict) -> dict:
    """检查作品级 staff 是否显式完成，避免只留下声优而静默漏主创。"""
    section = plan.get("movie") if plan.get("type") == "movie" else plan.get("show")
    if not isinstance(section, dict):
        return {"status": "failed", "required": True,
                "errors": ["plan 缺少 show/movie 对象"]}
    # 没有 Bangumi 身份的最小离线测试/手工 plan 不强制联网 staff；一旦有 bgm_id
    # 或主动声明 staff 字段，就必须完成二态审查。
    has_bgm = section.get("bgm_id") not in (None, "")
    cards = section.get("actors") or []
    crew = [
        card for card in cards
        if isinstance(card, dict)
        and (card.get("type") or "Actor") != "Actor"
        and str(card.get("name") or "").strip()
    ]
    note_present = bool(str(section.get("staff_note") or "").strip())
    note_mappable_roles = _staff_note_mappable_roles(
        str(section.get("staff_note") or "")
    )
    status = section.get("staff_status")
    audit = section.get("staff_audit")
    required = has_bgm or "staff_status" in section or "staff_note" in section
    if not required:
        return {"status": "passed", "required": False, "state": "unchecked",
                "crew_count": len(crew), "note_present": note_present, "errors": []}
    errors: list[str] = []
    if note_mappable_roles:
        errors.append(
            "staff_note 不得包含可映射职位："
            + "、".join(note_mappable_roles)
            + "；这些职位必须只写入 crew 卡片"
        )
    audit_valid = False
    audited_mappable = None
    if audit is not None:
        if not isinstance(audit, dict):
            errors.append("staff_audit 必须是对象")
        elif audit.get("persons_checked") is not True:
            errors.append("staff_audit.persons_checked 必须为 true")
        elif (type(audit.get("mappable_crew_count")) is not int
              or audit.get("mappable_crew_count") < 0):
            errors.append("staff_audit.mappable_crew_count 必须是非负整数")
        else:
            audit_valid = True
            audited_mappable = audit["mappable_crew_count"]
    if status not in STAFF_STATUS_STATES:
        errors.append("staff_status 必须明确为 present 或 empty")
    elif status == "present" and not (crew or note_present):
        errors.append("staff_status=present 与内容矛盾：没有 crew 卡片或 staff_note")
    elif status == "present" and not crew:
        if not audit_valid:
            errors.append(
                "staff_status=present 只有 staff_note 时必须提供 staff_audit，"
                "证明已检查且没有可映射 crew"
            )
        elif audited_mappable != 0:
            errors.append(
                "staff_status=present 的 staff_audit 显示存在可映射 crew，"
                "但 actors 中没有 crew 卡片"
            )
    elif status == "empty" and (crew or note_present):
        errors.append("staff_status=empty 与内容矛盾：仍有 crew 卡片或 staff_note")
    elif status == "empty":
        if not audit_valid:
            errors.append(
                "staff_status=empty 必须提供 staff_audit，证明已检查 Bangumi persons"
            )
        elif audited_mappable != 0:
            errors.append(
                "staff_status=empty 与 staff_audit 矛盾：存在可映射 crew"
            )
    if audit_valid and crew and audited_mappable != len(crew):
        errors.append(
            f"staff_audit.mappable_crew_count={audited_mappable} 与实际 crew={len(crew)} 不一致"
        )
    return {
        "status": "failed" if errors else "passed",
        "required": True,
        "state": status,
        "crew_count": len(crew),
        "note_present": note_present,
        "staff_note_mappable_roles": note_mappable_roles,
        "audited_mappable_crew_count": audited_mappable,
        "errors": errors,
    }


def _validate_show_staff(plan: dict) -> dict:
    result = _inspect_show_staff(plan)
    if result["status"] != "passed":
        raise ValueError("作品级 staff 校验失败：" + "；".join(result["errors"]))
    return result


def _validate_special_order(plan: dict) -> dict:
    """只校验 Agent 给出的 Season 0 排序，不按标题自行分类。"""
    entries_key = "extras" if plan.get("type") == "movie" else "episodes"
    candidates = []
    for input_index, entry in enumerate(plan.get(entries_key) or []):
        if not isinstance(entry, dict) or not entry.get("video_path"):
            continue
        try:
            season = int(entry.get("season") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Season 0 排序校验遇到非法 season") from exc
        if season == 0:
            candidates.append((input_index, entry))

    if len(candidates) <= 1:
        return {"status": "not_required", "checked_count": len(candidates)}

    errors: list[str] = []
    records: list[dict] = []
    for input_index, entry in candidates:
        label = entry.get("title") or f"S00E{entry.get('episode', 0)}"
        order = entry.get("special_order")
        if not isinstance(order, dict):
            errors.append(f"{label!r} 缺少 special_order")
            continue
        values: dict = {}
        valid = True
        for field in SPECIAL_ORDER_FIELDS:
            value = order.get(field)
            minimum = 0 if field == "source_index" else 1
            if type(value) is not int or value < minimum:
                errors.append(
                    f"{label!r} 的 special_order.{field} 必须是 >= {minimum} 的整数"
                )
                valid = False
            else:
                values[field] = value
        series_key = order.get("series_key")
        if not isinstance(series_key, str) or not series_key.strip():
            errors.append(f"{label!r} 的 special_order.series_key 不能为空")
            valid = False
        else:
            values["series_key"] = series_key.strip()
        if valid:
            values.update({
                "input_index": input_index,
                "entry": entry,
                "label": str(label),
            })
            records.append(values)

    if errors:
        raise ValueError("Season 0 special_order 不完整：" + "；".join(errors))

    seen_keys: set[tuple] = set()
    seen_sources: set[int] = set()
    group_names: dict[tuple, str] = {}
    series_orders: dict[tuple, int] = {}
    for record in records:
        key = tuple(record[field] for field in SPECIAL_ORDER_FIELDS[:3])
        if key in seen_keys:
            errors.append(f"{record['label']!r} 与其它条目重复 special_order")
        seen_keys.add(key)
        source_index = record["source_index"]
        if source_index in seen_sources:
            errors.append(f"{record['label']!r} 重复 source_index={source_index}")
        seen_sources.add(source_index)
        group_key = (record["priority"], record["series_order"])
        old_name = group_names.setdefault(group_key, record["series_key"])
        if old_name != record["series_key"]:
            errors.append(f"special_order 系列顺序 {group_key} 对应多个 series_key")
        series_key = (record["priority"], record["series_key"])
        old_order = series_orders.setdefault(series_key, record["series_order"])
        if old_order != record["series_order"]:
            errors.append(f"series_key {record['series_key']!r} 被分配多个 series_order")

    for priority in {record["priority"] for record in records}:
        orders = sorted(
            record["series_order"] for record in records if record["priority"] == priority
        )
        expected = list(range(1, len(set(orders)) + 1))
        if sorted(set(orders)) != expected:
            errors.append(f"priority={priority} 的 series_order 必须从 1 连续编号")
    if errors:
        raise ValueError("Season 0 special_order 无效：" + "；".join(errors))

    expected_records = sorted(
        records,
        key=lambda record: tuple(record[field] for field in SPECIAL_ORDER_FIELDS),
    )
    if [record["input_index"] for record in records] != [
        record["input_index"] for record in expected_records
    ]:
        actual = "、".join(record["label"] for record in records)
        expected = "、".join(record["label"] for record in expected_records)
        raise ValueError(
            "Season 0 未按 Agent 给出的 special_order 排列；"
            f"实际={actual}；应为={expected}"
        )

    episode_numbers = []
    for record in records:
        value = record["entry"].get("episode")
        if type(value) is not int or value < 1:
            raise ValueError(f"{record['label']!r} 的 Season 0 episode 必须是正整数")
        episode_numbers.append(value)
    expected_numbers = list(range(min(episode_numbers), min(episode_numbers) + len(episode_numbers)))
    if episode_numbers != sorted(episode_numbers) or sorted(episode_numbers) != expected_numbers:
        raise ValueError("Season 0 的 S00E episode 必须按排序结果连续递增，不能重复或断号")

    return {
        "status": "passed",
        "checked_count": len(records),
        "series_count": len(group_names),
        "priority_count": len({record["priority"] for record in records}),
    }


# 无歧义的压制/技术标签：真实标题里不可能出现，可作为硬拒绝依据。
_RELEASE_TAG_ALTERNATION = (
    r"\d{3,4}[xX×]\d{3,4}|2160p|1080[pi]|720p|HEVC|AVC|x26[45]|Hi10P?"
    r"|FLAC|AAC|AC-?3|\bBDRip\b|\bBDMV\b|\bWeb-?DL\b|\[[0-9A-Fa-f]{8}\]"
)
# 载体词：只是介质名，不是压制标签。「BD Box 購入特典」「DVD Vol.1 映像特典」
# 都是常见的真实特典标题，而 special-rules.md §4.3.7 正要求优先采用这类文件名
# 标题；若把它们当技术标签，护栏会直接中止整次刮削。
_MEDIUM_WORD_ALTERNATION = r"\bBD\b|\bDVD\b"

# 标题硬护栏用：只认无歧义标签，刻意排除载体词。
SPECIAL_TITLE_RELEASE_TAG_RE = re.compile(f"(?:{_RELEASE_TAG_ALTERNATION})")
# 文件名清洗用：判断残余是否仍含真实标题时，连载体词一起剥掉更干净。
SPECIAL_TITLE_STEM_TAG_RE = re.compile(
    f"(?:{_RELEASE_TAG_ALTERNATION}|{_MEDIUM_WORD_ALTERNATION})"
)
SPECIAL_TITLE_FALLBACK_RE = re.compile(r"^特典\s*\d+$")
SPECIAL_TITLE_CJK_RUN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff01-\uff5e]{4,}"
)


def _special_title_stem_remnant(video_path: str, show_title: str) -> str:
    stem = os.path.basename(str(video_path))
    stem = os.path.splitext(stem)[0]
    stem = re.sub(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】", " ", stem)
    stem = SPECIAL_TITLE_STEM_TAG_RE.sub(" ", stem)
    if show_title:
        stem = stem.replace(show_title, " ")
    return stem.strip()


def _validate_special_titles(plan: dict) -> dict:
    """Season 0/extras 标题护栏：非空、无压制技术标签；「特典 N」须无文件名标题证据。

    标题语义（翻译质量、专有名词取舍）仍由 Agent 按 special-rules.md §3 判断；
    这里只拦截机械可判的错误：空标题、把原文件名当标题、以及文件名明明携带
    明确标题却偷懒使用「特典 N」兜底（后者为 warning，交 Agent 复核）。
    """
    entries_key = "extras" if plan.get("type") == "movie" else "episodes"
    owner = plan.get("show") or plan.get("movie") or {}
    show_title = str(owner.get("title") or "")
    errors: list[str] = []
    warnings: list[str] = []
    for entry in plan.get(entries_key) or []:
        if not isinstance(entry, dict) or not entry.get("video_path"):
            continue
        try:
            season = int(entry.get("season") or 0)
        except (TypeError, ValueError):
            continue
        if season != 0:
            continue
        label = str(entry.get("title") or f"S00E{entry.get('episode', 0)}")
        title = str(entry.get("title") or "").strip()
        if not title:
            errors.append(f"S00E{entry.get('episode', 0)} 标题为空")
            continue
        tag = SPECIAL_TITLE_RELEASE_TAG_RE.search(title)
        if tag:
            errors.append(
                f"{label!r} 标题含压制/技术标签 {tag.group(0)!r}；"
                "禁止把原文件名当标题，需按 special-rules.md §3 清洗或本地化"
            )
            continue
        if SPECIAL_TITLE_FALLBACK_RE.match(title):
            remnant = _special_title_stem_remnant(entry["video_path"], show_title)
            if SPECIAL_TITLE_CJK_RUN_RE.search(remnant):
                warnings.append(
                    f"{label} 使用「特典 N」兜底，但文件名疑似携带明确标题"
                    f"（{remnant[:40]}…）；按 special-rules.md §3 应优先使用真实标题"
                )
    if errors:
        raise ValueError(
            "Season 0 标题护栏失败：" + "；".join(errors[:10])
            + ("；..." if len(errors) > 10 else "")
        )
    return {"status": "passed", "warnings": warnings}


def _dry_run_report_path(requested: str | None = None) -> Path:
    """完整 dry-run 报告只写本机计划位置，不写片源或库根。"""
    if requested:
        path = Path(normalize_path(requested)).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = (Path(tempfile.gettempdir()) / "anime-scraper" / "dry-run" /
                f"{stamp}-{os.getpid()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _dry_run_summary(show, media_paths: list, artwork: list[dict], *,
                     plan: dict, plan_type: str, link_enabled: bool,
                     link_root_value: str | None = None,
                     hardlink_mode_source: str | None = None,
                     library_projection_status: str = "passed",
                     primary_video=None, visual_review: dict | None = None) -> dict:
    included = [(episode, path) for episode, path in zip(show.episodes, media_paths) if path]
    normal_count = (int(bool(primary_video)) if plan_type == "movie"
                    else sum(episode.season > 0 for episode, _ in included))
    special_count = sum(episode.season == 0 for episode, _ in included)
    skipped_count = len(media_paths) - len(included)
    normal_missing_plot_count = (
        int(bool(primary_video) and not str(show.plot or "").strip())
        if plan_type == "movie" else
        sum(episode.season > 0 and not str(episode.plot or "").strip()
            for episode, _ in included)
    )
    special_missing_plot_count = sum(
        episode.season == 0 and not str(episode.plot or "").strip()
        for episode, _ in included
    )
    missing_plot_count = normal_missing_plot_count + special_missing_plot_count
    tmdb_counts = Counter(
        episode.tmdb_match_status for episode, _ in included if episode.season == 0
    )
    artwork_methods = Counter(str(item.get("method") or "unspecified") for item in artwork)
    if visual_review is None:
        visual_review = _validate_artwork_review(plan, artwork)
    original_cache_dir = _plan_original_cache_dir(plan)
    episode_plot_validation = _inspect_episode_plots(plan)
    show_plot_validation = _inspect_show_plot(plan)
    show_staff_validation = _inspect_show_staff(plan)
    warnings = []
    if normal_missing_plot_count:
        if episode_plot_validation["status"] == "passed":
            warnings.append(
                f"{normal_missing_plot_count} 个正片 plot 为空；四级来源均明确为空，按规则放行"
            )
        else:
            warnings.append(
                f"{normal_missing_plot_count} 个正片 plot 为空且缺少有效来源证据；dry-run 将拒绝"
            )
    if show_staff_validation["status"] != "passed":
        warnings.append("作品级 staff 未完成 present/empty 审查；dry-run 将拒绝")
    if show_plot_validation["status"] != "passed":
        warnings.append("作品级 plot 含制作人员信息；dry-run 将拒绝")
    if show.bgm_id is None:
        warnings.append("缺少 bgm_id")
    if show.anidb_aid is None:
        warnings.append("缺少 anidb_aid")
    for warning in visual_review.get("warnings") or []:
        if warning not in warnings:
            warnings.append(str(warning))
    sorttitle_validation = _validate_sorttitle(plan)
    if sorttitle_validation["semantic_review_required"]:
        warnings.append(
            "sorttitle 前缀不在最终 title 中；必须由 Agent 根据已查元数据和可靠知识"
            "完成系列语义审查，脚本只校验格式"
        )
    song_evidence_validation = _validate_song_evidence(plan)
    special_order_validation = _validate_special_order(plan)
    special_titles_validation = _validate_special_titles(plan)
    for warning in special_titles_validation.get("warnings") or []:
        if warning not in warnings:
            warnings.append(str(warning))
    tmdb_identity_validation = _validate_tmdb_special_identity(
        plan, plan_type=plan_type
    )
    return {
        "type": plan_type,
        "mode": "link-library" if link_enabled else "source-only",
        "library_projection": {
            "hardlinks_enabled": link_enabled,
            "link_root": link_root_value if link_enabled else None,
            "decision_source": hardlink_mode_source,
        },
        "title": show.title,
        "sorttitle": show.sorttitle,
        "sorttitle_validation": sorttitle_validation,
        "bgm_id": show.bgm_id,
        "anidb_aid": show.anidb_aid,
        "primary_video_present": bool(primary_video) if plan_type == "movie" else None,
        "normal_episode_count": normal_count,
        "special_or_extra_count": special_count,
        "skipped_count": skipped_count,
        "missing_plot_count": missing_plot_count,
        "normal_missing_plot_count": normal_missing_plot_count,
        "special_missing_plot_count": special_missing_plot_count,
        "plot_validation": episode_plot_validation,
        "show_plot": show_plot_validation,
        "show_staff": show_staff_validation,
        "tmdb_match_status": {
            "matched": tmdb_counts.get("matched", 0),
            "not_found": tmdb_counts.get("not_found", 0),
            "unknown": tmdb_counts.get("unknown", 0),
        },
        "artwork_count": len(artwork),
        "artwork_methods": dict(sorted(artwork_methods.items())),
        "artwork_review": visual_review,
        "original_cache_dir": original_cache_dir,
        "special_order": special_order_validation,
        "tmdb_identity": tmdb_identity_validation,
        "validations": {
            "sorttitle": sorttitle_validation["status"],
            "song_evidence": song_evidence_validation["status"],
            "episode_plots": episode_plot_validation["status"],
            "show_plot": show_plot_validation["status"],
            "show_staff": show_staff_validation["status"],
            "special_order": special_order_validation["status"],
            "special_titles": special_titles_validation["status"],
            "tmdb_identity": tmdb_identity_validation["status"],
            "special_airdates": "passed",
            "source_media": "passed",
            "artwork_contract": "passed",
            "artwork_visual_review": visual_review["status"],
            "library_projection": library_projection_status,
            "library_preflight": "passed" if link_enabled else "not_requested",
        },
        "warnings": warnings,
    }


def _emit_dry_run_report(plan: dict, show, media_paths: list, artwork: list[dict], args,
                         *, plan_type: str, actions: dict, preflight: dict | None = None,
                         primary_video=None) -> int:
    """完整结果留盘；默认终端只打印审计摘要，避免大 plan 截断。"""
    report_path = _dry_run_report_path(args.report_file)
    visual_review = preflight.get("artwork_review") if preflight else None
    summary = _dry_run_summary(
        show, media_paths, artwork,
        plan=plan,
        plan_type=plan_type,
        link_enabled=bool(args.link_root),
        link_root_value=args.link_root,
        hardlink_mode_source=args.hardlink_mode_source,
        library_projection_status=args.library_projection["status"],
        primary_video=primary_video,
        visual_review=visual_review,
    )
    summary["report_path"] = str(report_path)
    report = {
        "schema": "anime-scraper-dry-run-report-v1",
        "generated_at": _utc_now(),
        "summary": summary,
        "source_plan": plan,
        "normalized": match.to_dict(show),
        "media_paths": [str(path) if path else None for path in media_paths],
        "primary_video": str(primary_video) if primary_video else None,
        "artwork": artwork,
        "preflight": preflight,
        "actions": actions,
    }
    atomic_write_json(report_path, report, default=str)

    tmdb = summary["tmdb_match_status"]
    methods = ", ".join(f"{key}={value}" for key, value in summary["artwork_methods"].items()) or "无"
    validations = summary["validations"]
    required_validations = ("episode_plots", "show_plot", "show_staff")
    run_status = (
        "通过" if all(summary["validations"][key] == "passed"
                      for key in required_validations) else "拒绝"
    )
    print(f"DRY-RUN {run_status}: [{plan_type}] {summary['title']} ({summary['mode']})")
    print(f"ID: bgm={summary['bgm_id']}；anidb={summary['anidb_aid']}")
    print(f"内容: 正片 {summary['normal_episode_count']}；Specials/extras "
          f"{summary['special_or_extra_count']}；跳过 {summary['skipped_count']}；"
          f"空 plot 正片 {summary['normal_missing_plot_count']} / "
          f"Specials/extras {summary['special_missing_plot_count']}")
    print(f"TMDB S0: matched={tmdb['matched']}；not_found={tmdb['not_found']}；"
          f"unknown={tmdb['unknown']}")
    print(f"图片: {summary['artwork_count']}（{methods}）")
    if summary.get("original_cache_dir"):
        print(f"原图缓存目录: {summary['original_cache_dir']}")
    projection = summary["library_projection"]
    print("硬链接: enabled={hardlinks_enabled}；root={link_root}；source={decision_source}".format(
        **projection
    ))
    print("验证: sorttitle={sorttitle}；song_evidence={song_evidence}；"
          "episode_plots={episode_plots}；show_plot={show_plot}；"
          "show_staff={show_staff}；"
          "special_order={special_order}；"
          "special_airdates={special_airdates}；source_media={source_media}；"
          "artwork={artwork_contract}；"
          "visual_review={artwork_visual_review}；"
          "library_projection={library_projection}；"
          "library_preflight={library_preflight}".format(**validations))
    for warning in summary["warnings"]:
        print(f"  ! {warning}")
    print(f"完整 dry-run 报告: {report_path}")
    if args.verbose:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if summary["validations"]["episode_plots"] != "passed":
        errors = summary["plot_validation"]["errors"]
        rendered = "；".join(errors[:10])
        if len(errors) > 10:
            rendered += "；…"
        raise ValueError("dry-run 拒绝：正片 plot 校验失败：" + rendered)
    if summary["validations"]["show_plot"] != "passed":
        errors = summary["show_plot"]["errors"]
        raise ValueError("dry-run 拒绝：作品级 plot 校验失败：" + "；".join(errors))
    if summary["validations"]["show_staff"] != "passed":
        errors = summary["show_staff"]["errors"]
        raise ValueError("dry-run 拒绝：作品级 staff 校验失败：" + "；".join(errors))
    return 0


def _print_skipped(skipped: list[str]) -> None:
    if skipped:
        print(f"⚠ 跳过 {len(skipped)} 项(无匹配视频/没把握,待人工):", file=sys.stderr)
        for title in skipped:
            print(f"    - {title}", file=sys.stderr)


def _refresh_artwork(plan: dict, args) -> bool:
    """CLI --refresh-artwork 或 plan.refresh_artwork 任一为真则强制重下源侧图。"""
    return bool(getattr(args, "refresh_artwork", False) or plan.get("refresh_artwork"))


def _review_candidate_index(review: dict, candidate_limit: int):
    groups = review.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("plan.artwork_review groups 必须是非空列表")

    candidates_by_id: dict[str, tuple[str, dict]] = {}
    group_ids: set[str] = set()
    group_candidate_counts: dict[str, int] = {}
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"plan.artwork_review.groups[{group_index}] 必须是对象")
        group_id = str(group.get("group_id") or "").strip()
        if not group_id or group_id in group_ids:
            raise ValueError("plan.artwork_review group_id 缺失或重复")
        group_ids.add(group_id)
        candidates = group.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"artwork_review 组 {group_id} 没有候选")
        if len(candidates) > candidate_limit:
            raise ValueError(f"artwork_review 组 {group_id} 超过 candidate_limit")
        group_candidate_counts[group_id] = len(candidates)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError(f"artwork_review 组 {group_id} 含非法候选")
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            if not candidate_id or candidate_id in candidates_by_id:
                raise ValueError("artwork_review candidate_id 缺失或重复")
            if not str(candidate.get("url") or "").strip():
                raise ValueError(f"artwork_review 候选 {candidate_id} 缺少 url")
            candidates_by_id[candidate_id] = (group_id, candidate)
    return group_ids, group_candidate_counts, candidates_by_id


def _validate_artwork_review(
        plan: dict, artwork: list[dict], *,
        multimodal_enabled: bool | None = None,
) -> dict:
    """校验 TMDB poster 已由 Agent 从 320x480 候选中完成视觉选择。

    只要 plan 有 TMDB poster，就必须声明完整的候选/选择契约，且 plan 中每张
    TMDB poster 的 URL 都要命中已选候选。episode still、fanart、logo 与 ffmpeg
    thumb 不属于本审查。
    """
    posters = [
        item for item in artwork
        if str(item.get("kind") or "").lower() == "poster"
        and item.get("method") == "tmdb"
    ]
    if not posters:
        return {"status": "not_required", "poster_count": 0,
                "reason": "no_tmdb_posters"}

    review = plan.get("artwork_review")
    if review is None:
        raise ValueError("plan 的 TMDB poster 必须声明 artwork_review")
    if not isinstance(review, dict):
        raise ValueError("plan.artwork_review 必须是对象")
    if review.get("schema") != ARTWORK_REVIEW_SCHEMA:
        raise ValueError("plan.artwork_review schema 必须为 anime-scraper-artwork-review-v1")

    status = review.get("status")
    if status not in {"completed", "not_required", "disabled"}:
        raise ValueError("plan.artwork_review 状态必须为 completed/not_required/disabled")

    recorded_enabled = review.get("multimodal_review_enabled")
    if type(recorded_enabled) is not bool:
        raise ValueError("plan 必须记录 multimodal_review_enabled 布尔值")
    if type(multimodal_enabled) is not bool:
        raise ValueError("dry-run 未取得 config artwork.multimodal_review 状态")
    if recorded_enabled != multimodal_enabled:
        raise ValueError("plan 识图开关与当前 config artwork.multimodal_review 不一致")
    expected_statuses = {"completed", "not_required"} if multimodal_enabled else {"disabled"}
    if status not in expected_statuses:
        raise ValueError("plan.artwork_review 状态与当前多模态开关不一致")
    expected_method = {
        "completed": "agent_multimodal",
        "not_required": "deterministic_single_candidate",
        "disabled": "deterministic_existing_pipeline",
    }[status]
    if review.get("selection_method") != expected_method:
        raise ValueError("plan.artwork_review selection_method 与状态不一致")

    preview = review.get("preview") or {}
    if [preview.get("width"), preview.get("height")] != [320, 480]:
        raise ValueError("plan.artwork_review preview 必须为 320x480")
    candidate_limit = review.get("candidate_limit")
    if not isinstance(candidate_limit, int) or not 1 <= candidate_limit <= 5:
        raise ValueError("plan.artwork_review candidate_limit 必须在 1..5 之间")

    digest = str(review.get("source_manifest_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("plan.artwork_review 缺少有效 source_manifest_sha256")

    groups = review.get("groups") or []
    if not groups:
        return {"status": "passed", "group_ids": set(), "group_candidate_counts": {},
                "candidates_by_id": {}}

    group_ids, group_candidate_counts, candidates_by_id = _review_candidate_index(
        review, candidate_limit
    )

    if status == "completed":
        text_roles = {"primary_title", "other_text", "none"}
        for candidate_id, (_group_id, candidate) in candidates_by_id.items():
            if candidate.get("visible_text_role") not in text_roles:
                raise ValueError(
                    f"artwork_review 候选 {candidate_id} 必须标注 visible_text_role"
                )
            prominence = candidate.get("primary_title_prominence")
            if candidate.get("visible_text_role") == "primary_title":
                if prominence not in {1, 2, 3}:
                    raise ValueError(
                        f"artwork_review 候选 {candidate_id} 的主标题显著度必须为 1/2/3"
                    )
            elif prominence != 0:
                raise ValueError(
                    f"artwork_review 候选 {candidate_id} 非主标题时显著度必须为 0"
                )
            issues = candidate.get("visual_issues")
            if not isinstance(issues, list) or any(
                    not isinstance(issue, str) or not issue.strip()
                    for issue in issues):
                raise ValueError(
                    f"artwork_review 候选 {candidate_id} 必须记录 visual_issues 列表"
                )
            width = candidate.get("width")
            height = candidate.get("height")
            if (not isinstance(width, int) or isinstance(width, bool) or width <= 0
                    or not isinstance(height, int) or isinstance(height, bool)
                    or height <= 0):
                raise ValueError(
                    f"artwork_review 候选 {candidate_id} 缺少有效原始分辨率"
                )
            expected_class = tmdb.poster_resolution_class(candidate)
            if candidate.get("resolution_class") != expected_class:
                raise ValueError(
                    f"artwork_review 候选 {candidate_id} resolution_class 与尺寸不一致"
                )

    if status == "not_required":
        reason = str(review.get("reason") or "").strip()
        if reason != "single_candidate":
            raise ValueError("artwork_review not_required 必须说明 single_candidate")
        if any(count != 1 for count in group_candidate_counts.values()):
            raise ValueError("artwork_review single_candidate 的每组必须恰有一个候选")
        selected_urls = {
            str(candidate["url"]).strip() for _, candidate in candidates_by_id.values()
        }
    else:
        if status == "disabled" and review.get("reason") != "config_disabled":
            raise ValueError("artwork_review disabled 必须说明 config_disabled")
        selections = review.get("selections")
        if not isinstance(selections, list):
            raise ValueError("plan.artwork_review selections 必须是列表")

        selected_groups: set[str] = set()
        selected_urls: set[str] = set()
        for index, selection in enumerate(selections):
            if not isinstance(selection, dict):
                raise ValueError(f"plan.artwork_review.selections[{index}] 必须是对象")
            group_id = str(selection.get("group_id") or "").strip()
            candidate_id = str(selection.get("candidate_id") or "").strip()
            if candidate_id not in candidates_by_id:
                raise ValueError(f"artwork_review 选择了未知 candidate_id: {candidate_id}")
            candidate_group, candidate = candidates_by_id[candidate_id]
            if group_id != candidate_group or group_id in selected_groups:
                raise ValueError(f"artwork_review 组选择缺失、错组或重复: {group_id}")
            if status == "disabled":
                group_candidates = next(
                    group["candidates"] for group in review["groups"]
                    if group["group_id"] == group_id
                )
                if candidate_id != group_candidates[0]["candidate_id"]:
                    raise ValueError("关闭多模态时必须沿用确定性原流程选择")
            if not str(selection.get("reason") or "").strip():
                raise ValueError(f"artwork_review 选择 {candidate_id} 缺少 reason")
            if selection.get("confidence") not in {"high", "medium", "low"}:
                raise ValueError(f"artwork_review 选择 {candidate_id} confidence 非法")
            if not isinstance(selection.get("flags", []), list):
                raise ValueError(f"artwork_review 选择 {candidate_id} flags 必须是列表")
            if status == "completed":
                # 只验证「审查覆盖 + 无致命缺陷」；语言/分辨率/标题都不得单项
                # 机械决胜，排名交给已看过图的 Agent。
                factors = selection.get("decision_factors")
                required_factors = {"language", "resolution", "title", "visual_quality"}
                if (not isinstance(factors, dict)
                        or set(factors) != required_factors
                        or any(not str(factors[key] or "").strip()
                               for key in required_factors)):
                    raise ValueError(
                        f"artwork_review 选择 {candidate_id} 必须完整记录"
                        " language/resolution/title/visual_quality 四项综合判断"
                    )
                fatal_issues = {
                    "wrong_work", "third_party_watermark", "severe_crop", "damaged",
                }
                if fatal_issues.intersection(candidate.get("visual_issues") or []):
                    raise ValueError(
                        f"artwork_review 选择 {candidate_id} 含不可接受的视觉缺陷"
                    )
            selected_groups.add(group_id)
            selected_urls.add(str(candidate["url"]).strip())

        if selected_groups != group_ids:
            missing = ", ".join(sorted(group_ids - selected_groups))
            raise ValueError(f"artwork_review 尚未为全部组完成选择: {missing}")
    candidate_urls = {
        str(candidate["url"]).strip() for _, candidate in candidates_by_id.values()
    }
    allowed_specials_selections = {
        "season_zero", "main_pool_alternative", "series_specials_reuse",
    }
    unreviewed = []
    for item in posters:
        url = str(item.get("url") or "").strip()
        relpath = str(item.get("library_relpath") or "").replace("\\", "/")
        if url in selected_urls:
            continue
        if (relpath == "Specials/poster.jpg"
                and item.get("specials_selection") in allowed_specials_selections
                and url in candidate_urls):
            continue
        unreviewed.append(url)
    unreviewed = sorted(set(unreviewed))
    if unreviewed:
        raise ValueError("plan.artwork 中存在未命中 Agent 识图选择的 TMDB poster")
    result = {
        "status": ({"completed": "passed", "not_required": "not_required",
                    "disabled": "disabled"}[status]),
        "poster_count": len(posters),
        "group_count": len(group_ids),
        "candidate_count": len(candidates_by_id),
        "reason": review.get("reason") if status != "completed" else None,
        "multimodal_review_enabled": review.get("multimodal_review_enabled"),
        "selection_method": review.get("selection_method"),
        "warnings": list(review.get("warnings") or []),
    }
    original_cache_dir = _plan_original_cache_dir(plan)
    if original_cache_dir:
        result["original_cache_dir"] = original_cache_dir
    return result


def _validate_specials_poster_distinctness(artwork: list[dict]) -> None:
    """Specials poster 不得与作品或季度主 poster 共用图片实体/URL。"""
    specials = [item for item in artwork
                if str(item.get("library_relpath") or "").replace("\\", "/")
                == "Specials/poster.jpg"]
    for special in specials:
        for main in artwork:
            if main is special or str(main.get("kind") or "").lower() != "poster":
                continue
            rel = str(main.get("library_relpath") or "").replace("\\", "/")
            if rel == "Specials/poster.jpg":
                continue
            if (special.get("source_path") == main.get("source_path")
                    or (special.get("url") and special.get("url") == main.get("url"))):
                raise ValueError(
                    "Specials/poster.jpg 不得复用作品或季度主海报；"
                    "没有独立候选时应省略该 artwork 项"
                )


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".svg"}


_POSTER_RELPATH_RE = re.compile(
    r"(?:poster|(?:Season \d{2}|Specials)/poster)\.[A-Za-z0-9]+$"
)


def _validate_artwork_relpath(item: dict, relpath: str, index: int) -> None:
    """拒绝没有图片扩展名的库侧 artwork 路径。

    Jellyfin 的作品级图片依赖约定文件名；例如 ``poster`` 不会等价于
    ``poster.jpg``。分集 thumb 另由 link_library 校验 stem，这里只负责扩展名。
    另外锁定海报位与 ``kind`` 的对应：识图护栏和快捷替换都按
    ``kind == "poster"`` 取件，落在海报位却写别的 kind 会让这张图静默绕过审查。
    """
    suffix = Path(relpath).suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        raise ValueError(
            f"plan.artwork[{index}] library_relpath 必须带图片扩展名(.jpg/.png/.webp/.svg): {relpath}"
        )
    kind = str(item.get("kind") or "").lower()
    if kind == "clearlogo" and suffix == ".svg" and not str(relpath).lower().endswith(".svg"):
        raise ValueError(f"plan.artwork[{index}] clearlogo 的 SVG 扩展名不合法: {relpath}")
    normalized = str(relpath).replace("\\", "/")
    if _POSTER_RELPATH_RE.fullmatch(normalized) and kind != "poster":
        raise ValueError(
            f"plan.artwork[{index}] 落在海报位 {normalized} 却声明 kind={kind!r}；"
            "海报位(根/Season NN/Specials)的 kind 必须是 poster，"
            "否则识图护栏与 --cached-replace 都取不到这张图"
        )


def _artwork(plan: dict, *, require_library_relpath: bool = False) -> list[dict]:
    """校验 plan.artwork；实体路径 source_path 必填。

    同一 source_path 可对应多条 library_relpath（一份实体、库侧多处硬链）。
    library_relpath 在建库时必填；仅源侧落盘时可缺省。
    """
    artwork = plan.get("artwork") or []
    if not isinstance(artwork, list):
        raise ValueError("plan.artwork 必须是列表")
    seen_relpaths: set[str] = set()
    for index, item in enumerate(artwork):
        if not isinstance(item, dict):
            raise ValueError(f"plan.artwork[{index}] 必须是对象")
        source = item.get("source_path")
        if not source:
            raise ValueError(f"plan.artwork[{index}] 缺少 source_path")
        source_path = Path(normalize_path(source))
        if not source_path.is_absolute():
            raise ValueError(f"plan.artwork[{index}] source_path 必须是绝对路径: {source}")
        if source_path.suffix.lower() not in _IMAGE_SUFFIXES:
            raise ValueError(f"plan.artwork[{index}] source_path 必须带图片扩展名: {source}")
        relpath = item.get("library_relpath")
        if require_library_relpath and not relpath:
            raise ValueError(f"plan.artwork[{index}] 建库时必须提供 library_relpath")
        if relpath:
            rel = Path(relpath)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"非法 artwork library_relpath: {relpath}")
            _validate_artwork_relpath(item, relpath, index)
            key = str(rel).replace("\\", "/")
            if key in seen_relpaths:
                raise ValueError(f"重复 artwork library_relpath: {relpath}")
            seen_relpaths.add(key)
        method = item.get("method")
        if method not in ("tmdb", "frame"):
            raise ValueError(f"plan.artwork[{index}] 未知 method: {method}")
        if method == "tmdb" and not str(item.get("url") or "").strip():
            raise ValueError(f"plan.artwork[{index}] method=tmdb 时必须提供 url")
        if method == "frame":
            fallback = item.get("fallback_video_path")
            if not fallback:
                raise ValueError(
                    f"plan.artwork[{index}] method=frame 时必须提供 fallback_video_path"
                )
            fallback_path = Path(normalize_path(fallback))
            if not fallback_path.is_absolute():
                raise ValueError(
                    f"plan.artwork[{index}] fallback_video_path 必须是绝对路径: {fallback}"
                )
            if not fallback_path.is_file():
                raise FileNotFoundError(
                    f"plan.artwork[{index}] fallback_video_path 不存在或不是普通文件: {fallback_path}"
                )
    return artwork


def _path_key(path: str | Path) -> str:
    """生成不解析符号链接的本机路径比较键，兼容 Windows 大小写。"""
    return os.path.normcase(os.path.normpath(str(Path(normalize_path(path)))))


def _validate_tmdb_still_priority(plan: dict, artwork: list[dict], media_paths: list,
                                   *, plan_type: str, primary_video=None) -> None:
    """有 TMDB still 证据时，强制对应 thumb 使用该 still，不得静默截帧。"""
    raw_entries = (list(plan.get("episodes") or []) if plan_type == "tv"
                  else list(plan.get("extras") or []))
    expected: list[tuple[Path, str]] = []
    if plan_type == "movie":
        movie = plan.get("movie") or plan.get("show") or {}
        if primary_video and movie.get("tmdb_still_url"):
            video = Path(normalize_path(primary_video))
            expected.append((video.with_name(video.stem + "-thumb.jpg"),
                             str(movie["tmdb_still_url"])))
    for raw, media_path in zip(raw_entries, media_paths):
        if not media_path or not raw.get("tmdb_still_url"):
            continue
        video = Path(normalize_path(media_path))
        expected.append((video.with_name(video.stem + "-thumb.jpg"),
                         str(raw["tmdb_still_url"])))

    by_source = {
        _path_key(item.get("source_path", "")): item
        for item in artwork
        if item.get("source_path")
    }
    errors: list[str] = []
    for thumb_path, still_url in expected:
        item = by_source.get(_path_key(thumb_path))
        if item is None:
            errors.append(f"{thumb_path}: 缺少对应 artwork")
            continue
        if item.get("method") != "tmdb" or str(item.get("url") or "") != still_url:
            errors.append(
                f"{thumb_path}: 已有 TMDB still 证据但 artwork 未使用该 URL"
            )
    if errors:
        raise ValueError("TMDB still 优先护栏失败：" + "；".join(errors[:10]) +
                         ("…" if len(errors) > 10 else ""))


def _validate_tmdb_special_identity(plan: dict, *, plan_type: str) -> dict:
    """确认 Season 0 的 TMDB 身份，并报告远程图或本地截帧策略。"""
    raw_entries = (list(plan.get("episodes") or []) if plan_type == "tv"
                   else list(plan.get("extras") or []))
    season_zero = [
        raw for raw in raw_entries
        if isinstance(raw, dict) and raw.get("season") == 0
    ]
    remote_entries = [
        raw for raw in season_zero
        if raw.get("tmdb_still_url") or raw.get("tmdb_match_status") == "matched"
    ]
    if not remote_entries:
        identity = plan.get("tmdb_identity")
        if isinstance(identity, dict):
            identity_status = str(identity.get("status") or "")
            if identity_status in {"ambiguous", "unknown"}:
                return {
                    "status": "fallback",
                    "checked_count": len(season_zero),
                    "identity_status": identity_status,
                    "selection": "frame",
                    "reason": str(identity.get("reason") or "TMDB 身份未验证"),
                }
            if identity_status == "verified":
                return {
                    "status": "passed",
                    "checked_count": len(season_zero),
                    "identity_status": identity_status,
                    "selection": "frame_no_still",
                }
        return {"status": "not_required", "checked_count": 0}

    identity = plan.get("tmdb_identity")
    if not isinstance(identity, dict):
        raise ValueError(
            "Season 0 使用 TMDB matched/still 时必须声明 plan.tmdb_identity；"
            "无法确认跨作合并条目的作品归属"
        )
    tmdb_id = identity.get("id")
    identity_status = str(identity.get("status") or "")
    first_air_date = str(identity.get("first_air_date") or "")
    if (type(tmdb_id) is not int or tmdb_id <= 0
            or identity_status not in {"verified", "ambiguous", "unknown"}
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", first_air_date)):
        raise ValueError(
            "plan.tmdb_identity 必须包含正整数 id、verified/ambiguous/unknown status "
            "和 YYYY-MM-DD first_air_date"
        )

    subject = plan.get("show") if plan_type == "tv" else plan.get("movie")
    local_premiered = str((subject or {}).get("premiered") or "")
    local_year = local_premiered[:4] if re.match(r"^\d{4}", local_premiered) else ""
    remote_year = first_air_date[:4]
    risks = []
    if identity_status != "verified":
        risks.append(f"identity.status={identity_status}")
    if local_year and local_year != remote_year:
        risks.append(f"首播年份不一致(local={local_year}, tmdb={remote_year})")
    if risks:
        labels = "、".join(str(raw.get("title") or raw.get("episode") or "Season 0")
                           for raw in remote_entries[:10])
        raise ValueError(
            "TMDB Season 0 作品身份护栏失败：" + "；".join(risks)
            + f"；禁止把远程 still/简介匹配到 {labels}，请保留 TMDB 查询记录后改用本地截帧并标记 unknown/not_found"
        )
    return {
        "status": "passed",
        "checked_count": len(remote_entries),
        "tmdb_id": tmdb_id,
        "identity_status": identity_status,
        "first_air_date": first_air_date,
    }


def _validate_library_projection(plan: dict, *, enabled: bool,
                                 root: str | None) -> dict:
    """plan 固化最终建库模式和目标，防止 dry-run 后配置漂移。"""
    snapshot = plan.get("library_projection")
    if snapshot is None:
        raise ValueError("plan 必须声明 library_projection")
    if not isinstance(snapshot, dict):
        raise ValueError("plan.library_projection 必须是对象")
    recorded_enabled = snapshot.get("hardlinks_enabled")
    if type(recorded_enabled) is not bool:
        raise ValueError("plan.library_projection.hardlinks_enabled 必须是布尔值")
    if recorded_enabled != enabled:
        raise ValueError("plan 硬链接开关与本次 config/CLI 最终模式不一致")
    recorded_root = snapshot.get("link_root")
    if enabled:
        if not str(recorded_root or "").strip():
            raise ValueError("启用硬链接的 plan 必须记录 library_projection.link_root")
        if not Path(normalize_path(recorded_root)).is_absolute():
            raise ValueError("plan.library_projection.link_root 必须是绝对路径")
        if not root or _path_key(recorded_root) != _path_key(root):
            raise ValueError("plan 硬链接目标与本次 config/CLI 目标不一致")
    elif recorded_root is not None and recorded_root != "":
        raise ValueError("关闭硬链接的 plan 不得记录 library_projection.link_root")
    return {"status": "passed", "hardlinks_enabled": enabled, "link_root": root}


def _source_preflight(plan: dict, artwork: list[dict], media_paths: list,
                      *, plan_type: str, primary_video=None,
                      multimodal_enabled: bool | None = None) -> dict:
    """两种落盘模式共用的零副作用源侧验证。"""
    artwork_review = _validate_artwork_review(
        plan, artwork, multimodal_enabled=multimodal_enabled
    )
    output_dir = plan.get("output_dir")
    if not output_dir:
        raise ValueError("plan 缺少 output_dir")
    output_path = Path(normalize_path(output_dir))
    if not output_path.is_absolute():
        raise ValueError(f"plan.output_dir 必须是绝对路径: {output_dir}")
    if output_path.name.casefold() in FORBIDDEN_OUTPUT_DIR_NAMES:
        raise ValueError(
            "plan.output_dir 末级目录不得恰为 meta 或 metadata；"
            "单元独占时应直接使用片源根，共享根时使用可识别单元的目录名"
        )
    if output_path.exists() and not output_path.is_dir():
        raise NotADirectoryError(f"plan.output_dir 已存在但不是目录: {output_path}")

    raw_media = ([primary_video] if primary_video else []) + [
        path for path in media_paths if path
    ]
    checked_media: list[Path] = []
    for index, raw_path in enumerate(raw_media):
        path = Path(normalize_path(raw_path))
        if not path.is_absolute():
            raise ValueError(f"媒体路径必须是绝对路径: {raw_path}")
        if not path.is_file():
            raise FileNotFoundError(f"媒体路径不存在或不是普通文件: {path}")
        checked_media.append(path)

    expected_thumbs = [
        path.with_name(path.stem + "-thumb.jpg") for path in checked_media
    ]
    artwork_sources = {_path_key(item["source_path"]) for item in artwork}
    missing_thumbs = [
        path for path in expected_thumbs if _path_key(path) not in artwork_sources
    ]
    if missing_thumbs:
        rendered = ", ".join(str(path) for path in missing_thumbs[:10])
        if len(missing_thumbs) > 10:
            rendered += "…"
        raise ValueError(
            f"plan.artwork 缺少 {len(missing_thumbs)} 个源侧同 stem thumb: {rendered}"
        )
    tmdb_identity = _validate_tmdb_special_identity(plan, plan_type=plan_type)
    _validate_tmdb_still_priority(
        plan, artwork, media_paths, plan_type=plan_type, primary_video=primary_video
    )

    return {
        "kind": plan_type,
        "mode": "source-preflight",
        "output_dir": str(output_path),
        "media_paths": [str(path) for path in checked_media],
        "expected_thumb_paths": [str(path) for path in expected_thumbs],
        "artwork_source_paths": [str(item["source_path"]) for item in artwork],
        "artwork_review": artwork_review,
        "tmdb_identity": tmdb_identity,
    }


def _materialize_artwork(plan: dict, *, skip_existing: bool = True,
                         tmdb_workers: int | None = None,
                         ffmpeg_workers: int | None = None) -> list[dict]:
    """只在源目录实体化图片；库侧稍后由 staging 投影硬链接。"""
    artwork = _artwork(plan)
    images.materialize_artwork_batch(
        artwork,
        skip_existing=skip_existing,
        image_workers=tmdb_workers,
        frame_workers=ffmpeg_workers,
    )
    return artwork


def _source_tv_nfos(show, plan: dict, video_paths: list) -> dict[str, Path]:
    written = nfo.write_all(show, plan.get("output_dir"),
                            episode_paths=[Path(p) if p else None for p in video_paths])
    mapping: dict[str, Path] = {"show": Path(plan.get("output_dir")) / "tvshow.nfo"}
    for index, raw_path in enumerate(video_paths):
        if raw_path:
            mapping[f"episode:{index}"] = Path(raw_path).with_suffix(".nfo")
    if set(mapping.values()) - set(written):
        raise OSError("源侧 TV NFO 写入不完整")
    return mapping


def _source_movie_nfos(show, plan: dict, video_path, extras_paths: list) -> dict[str, Path]:
    movie_nfo = nfo.write_movie(show, video_path=video_path, output_dir=plan.get("output_dir"))
    mapping: dict[str, Path] = {"movie": movie_nfo}
    for index, (episode, extra_path) in enumerate(zip(show.episodes, extras_paths)):
        if not extra_path:
            continue
        path = Path(extra_path).with_suffix(".nfo")
        nfo.write_nfo_text(path, nfo.build_episode_nfo(episode))
        mapping[f"extra:{index}"] = path
    return mapping


def _run_tv(plan: dict, args) -> int:
    _validate_tv_primary_content(plan)
    _validate_specials_poster_distinctness(_artwork(plan, require_library_relpath=bool(args.link_root)))
    show, video_paths = match.assemble(plan)
    if not args.dry_run:
        _validate_show_plot(plan)
        _validate_show_staff(plan)
        _validate_episode_plots(plan)
    match.validate_special_airdates(show, video_paths)
    skipped = [ep.title or ep.anidb_epno or f"S{ep.season}E{ep.episode}"
               for ep, path in zip(show.episodes, video_paths) if not path]
    refresh = _refresh_artwork(plan, args)
    skip_existing = not refresh

    if not args.link_root:
        artwork = _artwork(plan, require_library_relpath=False)
        preflight = _source_preflight(
            plan, artwork, video_paths, plan_type="tv",
            multimodal_enabled=args.multimodal_review_enabled,
        )
        if args.dry_run:
            actions = {
                "write_source_nfos": [str(Path(plan.get("output_dir")) / "tvshow.nfo"),
                                      *[str(Path(p).with_suffix(".nfo"))
                                        for p in video_paths if p]],
                "materialize_source_artwork": [a.get("source_path") for a in artwork],
                "refresh_artwork": refresh,
                "project_flat_mixed_root": None,
            }
            return _emit_dry_run_report(
                plan, show, video_paths, artwork, args,
                plan_type="tv", actions=actions, preflight=preflight,
            )
        written = nfo.write_all(show, plan.get("output_dir"),
                                episode_paths=[Path(p) if p else None for p in video_paths])
        _materialize_artwork(
            plan, skip_existing=skip_existing,
            tmdb_workers=args.artwork_workers["tmdb_workers"],
            ffmpeg_workers=args.artwork_workers["ffmpeg_workers"],
        )
        print(f"✓ 写出 {len(written)} 个源侧 NFO、实体化 {len(artwork)} 张源侧图片 → {plan.get('output_dir')}",
              file=sys.stderr)
        if _plan_original_cache_dir(plan):
            print(f"原图缓存目录: {_plan_original_cache_dir(plan)}", file=sys.stderr)
        _print_skipped(skipped)
        return 0

    artwork = _artwork(plan, require_library_relpath=True)
    source_preflight = _source_preflight(
        plan, artwork, video_paths, plan_type="tv",
        multimodal_enabled=args.multimodal_review_enabled,
    )
    preflight = link_library.preflight_tv_tree(show, video_paths, args.link_root, artwork=artwork)
    preflight["artwork_review"] = source_preflight["artwork_review"]
    if args.dry_run:
        report = {"preflight": preflight, "actions": {
            "write_source_nfos": [str(Path(plan.get("output_dir")) / "tvshow.nfo"),
                                  *[str(Path(p).with_suffix(".nfo")) for p in video_paths if p]],
            "materialize_source_artwork": [a.get("source_path") for a in artwork],
            "refresh_artwork": refresh,
            "project_flat_mixed_root": preflight["final_dir"],
        }}
        return _emit_dry_run_report(
            plan, show, video_paths, artwork, args,
            plan_type="tv", actions=report["actions"], preflight=preflight,
        )

    source_nfos = _source_tv_nfos(show, plan, video_paths)
    _materialize_artwork(
        plan, skip_existing=skip_existing,
        tmdb_workers=args.artwork_workers["tmdb_workers"],
        ffmpeg_workers=args.artwork_workers["ffmpeg_workers"],
    )
    written, tree_skipped, n_video, n_sub, show_dir = link_library.build_tv_tree(
        show, video_paths, args.link_root, source_nfo_paths=source_nfos, artwork=artwork)
    print(f"✓ 建立扁平混合库作品树: {show_dir}  (硬链接视频 {n_video}、字幕 {n_sub}、"
          f"NFO {len(written)}、图片 {len(artwork)})", file=sys.stderr)
    if _plan_original_cache_dir(plan):
        print(f"原图缓存目录: {_plan_original_cache_dir(plan)}", file=sys.stderr)
    _print_skipped(tree_skipped)
    return 0


def _run_movie(plan: dict, args) -> int:
    show, extras_paths = match.assemble_movie(plan)
    if not args.dry_run:
        _validate_show_plot(plan)
        _validate_show_staff(plan)
        _validate_episode_plots(plan)
    match.validate_special_airdates(show, extras_paths)
    video = (plan.get("movie") or plan.get("show") or {}).get("video_path")
    refresh = _refresh_artwork(plan, args)
    skip_existing = not refresh

    if not args.link_root:
        artwork = _artwork(plan, require_library_relpath=False)
        preflight = _source_preflight(
            plan, artwork, extras_paths, plan_type="movie", primary_video=video,
            multimodal_enabled=args.multimodal_review_enabled,
        )
        if args.dry_run:
            action_nfos = ([str(Path(video).with_suffix(".nfo"))] if video else
                           [str(Path(plan.get("output_dir")) / "movie.nfo")])
            action_nfos.extend(str(Path(p).with_suffix(".nfo")) for p in extras_paths if p)
            actions = {
                "write_source_nfos": action_nfos,
                "materialize_source_artwork": [a.get("source_path") for a in artwork],
                "refresh_artwork": refresh,
                "project_flat_mixed_root": None,
            }
            return _emit_dry_run_report(
                plan, show, extras_paths, artwork, args,
                plan_type="movie", actions=actions, preflight=preflight,
                primary_video=video,
            )
        mapping = _source_movie_nfos(show, plan, video, extras_paths)
        _materialize_artwork(
            plan, skip_existing=skip_existing,
            tmdb_workers=args.artwork_workers["tmdb_workers"],
            ffmpeg_workers=args.artwork_workers["ffmpeg_workers"],
        )
        print(f"✓ 写出 {len(mapping)} 个源侧电影/特典 NFO、实体化 {len(artwork)} 张源侧图片",
              file=sys.stderr)
        if _plan_original_cache_dir(plan):
            print(f"原图缓存目录: {_plan_original_cache_dir(plan)}", file=sys.stderr)
        return 0

    artwork = _artwork(plan, require_library_relpath=True)
    source_preflight = _source_preflight(
        plan, artwork, extras_paths, plan_type="movie", primary_video=video,
        multimodal_enabled=args.multimodal_review_enabled,
    )
    preflight = link_library.preflight_movie_tree(show, video, args.link_root, extras_paths,
                                                   artwork=artwork)
    preflight["artwork_review"] = source_preflight["artwork_review"]
    if args.dry_run:
        action_nfos = []
        if video:
            action_nfos.append(str(Path(video).with_suffix(".nfo")))
        else:
            action_nfos.append(str(Path(plan.get("output_dir")) / "movie.nfo"))
        action_nfos.extend(str(Path(p).with_suffix(".nfo")) for p in extras_paths if p)
        actions = {
            "write_source_nfos": action_nfos,
            "materialize_source_artwork": [a.get("source_path") for a in artwork],
            "refresh_artwork": refresh,
            "project_flat_mixed_root": preflight["final_dir"],
        }
        return _emit_dry_run_report(
            plan, show, extras_paths, artwork, args,
            plan_type="movie", actions=actions, preflight=preflight,
            primary_video=video,
        )

    source_nfos = _source_movie_nfos(show, plan, video, extras_paths)
    _materialize_artwork(
        plan, skip_existing=skip_existing,
        tmdb_workers=args.artwork_workers["tmdb_workers"],
        ffmpeg_workers=args.artwork_workers["ffmpeg_workers"],
    )
    written, skipped, n_video, n_sub, movie_dir = link_library.build_movie_tree(
        show, video, args.link_root, extras_paths=extras_paths, source_nfo_paths=source_nfos,
        artwork=artwork)
    print(f"✓ 建立扁平混合库电影树: {movie_dir}  (硬链接视频 {n_video}、字幕 {n_sub}、"
          f"NFO {len(written)}、图片 {len(artwork)})", file=sys.stderr)
    if _plan_original_cache_dir(plan):
        print(f"原图缓存目录: {_plan_original_cache_dir(plan)}", file=sys.stderr)
    _print_skipped(skipped)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="按 unit plan 写源侧 NFO/图片；可选投影到扁平 Jellyfin 混合库"
    )
    parser.add_argument("--plan", required=True, help="plan JSON 文件路径，或 - 表示 stdin")
    parser.add_argument("--dry-run", action="store_true", help="执行完整只读预检并输出动作清单")
    parser.add_argument("--report-file", metavar="PATH",
                        help="dry-run 完整报告路径；默认写系统临时目录")
    parser.add_argument("--verbose", action="store_true",
                        help="dry-run 时额外把完整报告打印到终端（大 plan 可能触发输出截断）")
    parser.add_argument(
        "--link-root", nargs="?", const="", metavar="DIR",
        help="可选 DIR 覆盖 config library.hardlinks.root；不改变总开关",
    )
    parser.add_argument(
        "--no-hardlinks", action="store_true",
        help="仅在用户本次明确要求不建库时，覆盖配置并关闭硬链接",
    )
    parser.add_argument(
        "--source-only", action="store_true",
        help="按 plan/config 校验硬链接配置，但本次只落盘源侧 NFO/图片",
    )
    parser.add_argument("--refresh-artwork", action="store_true",
                        help="强制重下/重截源侧图片（默认：已有有效图则跳过）")
    args = parser.parse_args(argv)

    if not args.dry_run and (args.report_file or args.verbose):
        parser.error("--report-file/--verbose 仅与 --dry-run 一起使用")

    plan = _load_plan(args.plan)
    _require_plan_schema(plan)
    _validate_plan_type(plan)
    _validate_lockdata(plan)
    _validate_sorttitle(plan)
    _validate_song_evidence(plan)
    _validate_special_order(plan)
    _validate_special_titles(plan)
    if args.no_hardlinks and args.link_root is not None:
        parser.error("--no-hardlinks 与 --link-root 不能同时使用")
    if args.source_only and (args.no_hardlinks or args.link_root is not None):
        parser.error("--source-only 不能与 --no-hardlinks/--link-root 同时使用")

    if args.source_only:
        configured_root = str(link_root(required=True))
        args.link_root = None
        args.hardlink_mode_source = "explicit_source_only"
        args.library_projection = _validate_library_projection(
            plan, enabled=True, root=configured_root
        )
    elif args.no_hardlinks:
        args.link_root = None
        args.hardlink_mode_source = "explicit_user_opt_out"
    else:
        enabled = hardlink_library_enabled()
        if not enabled:
            if args.link_root is not None:
                parser.error("config 已关闭硬链接，--link-root 不能绕过总开关")
            args.link_root = None
            args.hardlink_mode_source = "config_disabled"
        else:
            override = args.link_root or None
            args.link_root = str(link_root(override=override, required=True))
            args.hardlink_mode_source = (
                "cli_root_override" if override is not None else "config_enabled"
            )

    if not args.source_only:
        args.library_projection = _validate_library_projection(
            plan, enabled=bool(args.link_root), root=args.link_root
        )
    args.multimodal_review_enabled = multimodal_artwork_review_enabled()
    args.artwork_workers = images.artwork_worker_config()
    if plan.get("type") == "movie":
        result = _run_movie(plan, args)
    else:
        result = _run_tv(plan, args)
    if result == 0:
        _write_progress_ledger(args, plan)
    return result


PROGRESS_SCHEMA = "anime-scraper-progress-v1"


def progress_ledger_path(plan_path: "str | Path") -> Path:
    """plan.json → 同目录 plan.progress.json。"""
    p = Path(plan_path)
    return p.with_name(p.stem + ".progress.json")


def _write_progress_ledger(args, plan: dict) -> None:
    """成功节点后由脚本更新进度账本；best-effort，绝不使已成功的运行失败。

    账本是压缩/中断后的唯一重入依据：dry_run/source/library 三个阶段各记完成
    时刻，Agent 重入时先读账本再做廉价校验，禁止重做账本已记完成的阶段。
    """
    if args.plan == "-":
        return                                    # stdin plan 无落盘位置，不记账
    try:
        path = progress_ledger_path(args.plan)
        ledger: dict = {}
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and existing.get("schema") == PROGRESS_SCHEMA:
                ledger = existing
        unit = plan.get("show") or plan.get("movie") or {}
        stages = ledger.setdefault("stages", {})
        now = _utc_now()
        if args.dry_run:
            stages["dry_run"] = {"at": now,
                                 "report_file": getattr(args, "report_file", None)}
        else:
            stages["source"] = {"at": now}
            if not args.source_only and args.link_root:
                stages["library"] = {"at": now, "link_root": args.link_root}
        ledger.update({
            "schema": PROGRESS_SCHEMA,
            "plan_path": str(Path(args.plan).resolve()),
            "type": plan.get("type"),
            "title": unit.get("title") or "",
            "updated_at": now,
        })
        atomic_write_json(path, ledger)
    except Exception as exc:                      # noqa: BLE001
        print(f"警告: 进度账本写入失败(不影响本次结果): {exc}")


if __name__ == "__main__":
    raise SystemExit(main())

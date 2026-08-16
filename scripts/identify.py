"""BDRip 文件识别:扫描视频、摆出【结构素材】,供 agent 判断单元边界与文件归类。

职责收敛:本模块**只做确定性机械活 + 正则初判**,
【不做】切单元和文件归类的最终判断——那些由 agent 在运行时读本模块吐出的
"结构素材(目录树 + 集号分布 + 文件名 hint)"后做跨语言语义推理完成。

  1. scan_video_files()      扫出所有视频文件(自动排除 CD/扫图/字幕等非视频)
  2. parse_bdrip_filename()  从文件名做**正则初判**(番名/集号/类型),放进 hint —— 仅供参考
  3. scan_tree()             递归吐目录树 + 每文件相对路径/hint(+可选时长),给 agent 切单元

**切单元不在这里**:一个大包分成
几个单元、每个单元是哪部作品、flat 平铺包按集号重置点怎么切——全部由 agent 读
`scan_tree()` 的结构素材判断。parse 的 hint 只是"机械初判",agent **有权推翻**:
规范命名(如 `[组] 番名 [12][规格]`)多半判得对,异常命名(番名带 ' - '、罗马数字
集号、跨语言、flat 多季混排…)正则会判错,以 agent 的语义复核为准。

离线自测:python identify.py <源目录>
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

from _common import atomic_write_json, normalize_path, source_root

# 只认视频;.mka(纯音频)、.ass/.lrc(字幕)、.flac/.jpg/.webp(CD/扫图)等自动被排除
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".m4v", ".webm"}

# 文件名里的特殊集关键词 → 归一化类型(仅用于生成 hint 初判,不是最终判据)
SPECIAL_KEYWORDS = {
    "nced": "credit", "ncop": "credit", "creditless": "credit",
    "clean": "credit", "textless": "credit",
    "menu": "special", "special": "special", "sp": "special", "特典": "special",
    "ova": "special", "oad": "special",
    "pv": "trailer", "cm": "trailer", "trailer": "trailer", "preview": "trailer", "预告": "trailer",
    "interview": "other", "talk": "other", "event": "other",
}

# 子文件夹名 → 特殊集提示(压制组常把特典单独放这些目录)
_SPECIAL_SUBDIRS = {"sps", "sp", "specials", "ova", "oad", "extras", "menu"}

# 技术规格标签(噪音),用于从标签里剔除
_TECH_PATTERNS = [
    r'^\d+p$', r'^x\d{3}', r'^h\.?26[45]', r'^hevc', r'^avc',
    r'^aac', r'^flac', r'^opus', r'^truehd', r'^dts', r'^pcm', r'^ac3',
    r'^10.?bit', r'^ma10p', r'^hi10p', r'^\d+bit',
    r'^bluray', r'^web.?dl', r'^bdrip', r'^tvrip', r'^dvdrip', r'^x26[45]',
]


# ── 扫描 ───────────────────────────────────────────────────────

def scan_video_files(directory: str | Path) -> list[Path]:
    """递归扫描视频文件,按路径排序。非视频(CD/扫图/字幕)天然被扩展名过滤掉。

    路径先过 normalize_path:反斜杠 UNC(\\\\server\\share)会被 Path.resolve
    错误解析成 C:\\server\\...,正斜杠形式两平台都安全。
    """
    d = Path(normalize_path(directory))
    if not d.is_dir():
        raise NotADirectoryError(f"目录不存在: {d}")
    return sorted(
        p for p in d.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )


# ── 文件名解析(正则初判 → hint,仅供 agent 参考)──────────────

def _normalize_token(raw: str) -> tuple[int | None, str, bool]:
    """把集号 token 归一化为 (集号|None, 类型, 是否特殊)。**这是初判,非最终判据。**

    规则:
      12 / 12END / 12 Fin   → 正片 12(剥完结词)
      12v2 / 12(v2)         → 正片 12(剥压制版本号)
      12.5 / 11.5           → 特殊(幕间/总集篇,数字但不在正片序列)
      OVA / NCED / SP1…     → 特殊(关键词)
    """
    s = (raw or "").strip().lower()
    if not s:
        return None, "normal", False

    # 关键词优先(OVA/NCED/SP…)
    for kw, etype in SPECIAL_KEYWORDS.items():
        if s == kw or s.startswith(kw):
            rest = s[len(kw):].strip()
            digits = re.findall(r'\d+', rest)
            return (int(digits[0]) if digits else None), etype, True

    # 小数集号 → 特殊(如 12.5 幕间)
    if re.match(r'^\d+\.\d+$', s):
        return int(s.split('.')[0]), "special", True

    # 剥完结词 / 版本后缀后取整数 → 正片
    core = re.sub(r'\s*(end|fin|完|終|终)\s*$', '', s)
    m = re.match(r'^(\d+)(?:\s*v\d+)?$', core)
    if m:
        return int(m.group(1)), "normal", False

    # 认不出 → 交给 agent(标记为需判定的特殊件)
    return None, "unknown", True


def parse_bdrip_filename(path: Path) -> dict:
    """对 BDRip 文件名做**正则初判**,返回 {original_path, original_stem, hint}。

    - `original_path` / `original_stem` 是**硬事实**(放顶层)。
    - `hint` 里的 group/series_name/episode_raw/episode_number/episode_type/is_special
      /technical_tags 全是【机械初判、仅供参考】:规范命名多半判得对,但异常命名
      (番名自带 ' - '、罗马数字集号、`SxxExx`、跨语言、flat 多季…)会判错。
      **最终归类由 agent 复核 hint + 目录上下文决定,agent 有权推翻。**

    支持 `[组] 番名 - 集号 [规格]` 与 `[组] 番名 [集号][规格]`。
    """
    stem = path.stem

    all_tags = re.findall(r'\[([^\]]*)\]', stem)
    body = re.sub(r'\s*\[[^\]]*\]\s*', ' ', stem).strip()
    body = re.sub(r'\s+', ' ', body)

    group = all_tags[0] if all_tags else ""
    remaining = all_tags[1:]

    # 从后续标签里找:纯数字集号 / 特殊关键词 / 其余当技术标签
    episode_from_tag = special_from_tag = None
    technical_tags: list[str] = []
    for t in remaining:
        tl = t.lower()
        if re.match(r'^\d{1,4}$', t) and episode_from_tag is None:
            episode_from_tag = t
        elif any(tl == kw or tl.startswith(kw) for kw in SPECIAL_KEYWORDS) and special_from_tag is None:
            special_from_tag = t
        else:
            technical_tags.append(t)

    # 集号来源:body 里的 " - NN" > 方括号数字 > 方括号特殊词
    episode_raw = ""
    series_name = body
    seps = list(re.finditer(r'\s+-\s+', body))
    if seps:
        after = body[seps[-1].end():].strip()
        if _looks_like_episode(after):
            series_name = body[:seps[-1].start()].strip()
            episode_raw = after
    if not episode_raw and episode_from_tag is not None:
        episode_raw = episode_from_tag
    if not episode_raw and special_from_tag is not None:
        episode_raw = special_from_tag

    number, etype, is_special = _normalize_token(episode_raw)

    return {
        # ── 硬事实 ──
        "original_path": path,
        "original_stem": stem,
        # ── 正则初判(仅供 agent 参考,可被推翻)──
        "hint": {
            "group": group,
            "series_name": series_name,
            "episode_raw": episode_raw,
            "episode_number": number,
            "episode_type": etype,
            "is_special": is_special,
            "technical_tags": technical_tags,
        },
    }


def _looks_like_episode(s: str) -> bool:
    """判断 ' - ' 之后的文本是否像集号/特殊词(避免把副标题误当集号)。"""
    sl = (s or "").strip().lower()
    if not sl:
        return False
    if re.match(r'^\d+(\.\d+)?(\s*v\d+)?(\s*(end|fin|完|終|终))?$', sl):
        return True
    return any(sl.startswith(kw) for kw in SPECIAL_KEYWORDS)


# ── 结构素材:目录树 + 集号分布 + hint(给 agent 切单元)────────

def _build_tree_text(root: Path, videos: list[Path]) -> str:
    """基于扫到的视频文件相对路径,画一棵**只含"有视频的目录"**的 ASCII 目录树。

    噪音目录(CDs/Scans 等)没有视频、天然不出现;让 agent 一眼看清作品/季/特典
    的目录布局,据此判断单元边界。
    """
    tree: dict = {}
    for v in videos:
        rel = v.relative_to(root)
        node = tree
        for part in rel.parts[:-1]:          # 逐级目录
            node = node.setdefault(part, {})
        node.setdefault("", []).append(rel.parts[-1])   # "" 键存本目录下的文件名

    lines = ["."]

    def walk(node: dict, prefix: str) -> None:
        subdirs = sorted(k for k in node if k != "")
        fnames = sorted(node.get("", []))
        items = [(d, True) for d in subdirs] + [(f, False) for f in fnames]
        for idx, (name, is_dir) in enumerate(items):
            last = idx == len(items) - 1
            branch = "└── " if last else "├── "
            lines.append(prefix + branch + (name + "/" if is_dir else name))
            if is_dir:
                lines_ext = "    " if last else "│   "
                walk(node[name], prefix + lines_ext)

    walk(tree, "")
    return "\n".join(lines)


def scan_tree(input_dir: str | Path | None = None, probe_fn=None) -> dict:
    """扫描输入大目录,吐出【结构素材】供 agent 判断单元边界与文件归类(本模块不判)。

    这是"切单元"这一步的**素材提供者**:不做"顶层子文件夹=一个单元"的机械假设
    (对 flat 平铺大包直接失效)。改由 agent 读这里给的
    目录树 + 各目录集号分布 + 逐文件 hint,自己判断:
      - 这堆文件分成几个单元、每个是哪部作品/哪一季/哪部剧场版;
      - flat 平铺包按**集号重置点 + 标题变化**在哪里切;
      - 每个文件是正片第几集,还是什么特殊集(复核 hint,有权推翻)。

    参数:
      input_dir:动画源目录；省略/None 时读取 config ``paths.source_root``。
      probe_fn(path)->秒|None:可选注入 ffprobe 读时长(如 probe_duration.probe_duration)。
        不给则各文件 duration=None(纯结构、只读、无 ffmpeg 依赖)。

    返回:
      {
        "root": 根目录绝对路径,
        "video_count": 视频总数,
        "tree_text": ASCII 目录树(仅含有视频的目录),
        "by_dir": { 相对目录: [该目录下正片集号升序] },   # 帮 agent 看 flat 包的季度重置点
        "files": [ {
            "rel_path":     相对根的完整相对路径(含子目录),
            "top_folder":   顶层子文件夹名(根下文件为 ""),
            "sub_dirs":     [中间子目录...],
            "stem":         文件名去扩展名(硬事实),
            "subdir_special_hint": 是否在 SPs/OVA/Specials 类目录(初判提示),
            "duration":     时长秒(probe_fn 提供时)或 None,
            "hint":         parse_bdrip_filename 的正则初判(仅供参考,agent 复核),
        } ]
      }
    """
    effective_input = input_dir if input_dir is not None else source_root()
    root = Path(normalize_path(effective_input))
    videos = scan_video_files(root)

    files: list[dict] = []
    by_dir: dict[str, list[int]] = {}
    for f in videos:
        rel = f.relative_to(root)
        parts = rel.parts
        top_folder = parts[0] if len(parts) > 1 else ""     # 顶层子文件夹;根下文件归 ""
        sub_parts = list(parts[1:-1])                        # 去掉 top_folder 和文件名
        info = parse_bdrip_filename(f)
        h = info["hint"]
        files.append({
            "rel_path": str(rel),
            "top_folder": top_folder,
            "sub_dirs": sub_parts,
            "stem": info["original_stem"],
            "subdir_special_hint": any(p.lower() in _SPECIAL_SUBDIRS for p in sub_parts),
            "duration": (probe_fn(f) if probe_fn else None),
            "hint": h,
        })
        # 集号分布:按【文件所在目录】收集正片集号,帮 agent 判断 flat 包在哪重置/换季
        dkey = str(rel.parent) if str(rel.parent) != "." else ""
        n = h["episode_number"]
        if n is not None and not h["is_special"]:
            by_dir.setdefault(dkey, []).append(n)

    for k in by_dir:
        by_dir[k].sort()

    return {
        "root": str(root),
        "video_count": len(files),
        "tree_text": _build_tree_text(root, videos),
        "by_dir": by_dir,
        "files": files,
    }


# ── CLI:完整 manifest 留盘,终端只给有界摘要 ─────────────────────

_SUMMARY_CANDIDATE_LIMIT = 30
_SUMMARY_ANOMALY_LIMIT = 20


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_output_path(kind: str, requested: str | None = None) -> Path:
    """返回本机报告路径；默认放系统临时目录，不写片源/NAS。"""
    if requested:
        path = Path(normalize_path(requested)).expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = (Path(tempfile.gettempdir()) / "anime-scraper" / kind /
                f"{stamp}-{os.getpid()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _scan_summary(data: dict) -> dict:
    """生成可审计但有界展示的摘要；完整证据仍保留在 manifest。"""
    buckets: dict[str, list[dict]] = {}
    for rec in data["files"]:
        key = rec["top_folder"] or "(根)"
        buckets.setdefault(key, []).append(rec)

    candidates = []
    for key, records in sorted(buckets.items()):
        normals = [r["hint"]["episode_number"] for r in records
                   if r["hint"]["episode_number"] is not None
                   and not r["hint"]["is_special"]]
        special_count = sum(
            bool(r["hint"]["is_special"] or r["subdir_special_hint"])
            for r in records
        )
        unknown_count = sum(r["hint"]["episode_type"] == "unknown" for r in records)
        series_hints = sorted({
            str(r["hint"].get("series_name") or "").strip()
            for r in records if str(r["hint"].get("series_name") or "").strip()
        })
        candidates.append({
            "path": key,
            "video_count": len(records),
            "normal_episode_min": min(normals) if normals else None,
            "normal_episode_max": max(normals) if normals else None,
            "normal_episode_count": len(normals),
            "special_hint_count": special_count,
            "unknown_hint_count": unknown_count,
            "series_hints": series_hints,
        })

    anomalies: list[dict] = []
    for directory, numbers in sorted(data["by_dir"].items()):
        counts = Counter(numbers)
        duplicates = sorted(number for number, count in counts.items() if count > 1)
        if duplicates:
            anomalies.append({
                "kind": "duplicate_episode",
                "path": directory or "(根)",
                "details": f"重复集号 {duplicates}",
            })
        unique = sorted(counts)
        if len(unique) >= 2:
            gaps = sorted(set(range(unique[0], unique[-1] + 1)) - set(unique))
            if gaps:
                anomalies.append({
                    "kind": "episode_gap",
                    "path": directory or "(根)",
                    "details": f"缺失集号 {gaps}",
                })

    by_parent: dict[str, set[str]] = {}
    for rec in data["files"]:
        parent = str(Path(rec["rel_path"]).parent)
        parent = "(根)" if parent == "." else parent
        hint = str(rec["hint"].get("series_name") or "").strip()
        if hint:
            by_parent.setdefault(parent, set()).add(hint)
        if rec["hint"]["episode_type"] == "unknown":
            anomalies.append({
                "kind": "unknown_hint",
                "path": rec["rel_path"],
                "details": "文件名无法可靠识别，需 agent 复核",
            })
    for parent, hints in sorted(by_parent.items()):
        if len(hints) > 1:
            anomalies.append({
                "kind": "title_change",
                "path": parent,
                "details": f"同目录出现 {len(hints)} 个 series_hint",
                "series_hints": sorted(hints),
            })

    special_files = [
        rec["rel_path"] for rec in data["files"]
        if rec["hint"]["is_special"] or rec["subdir_special_hint"]
    ]
    unknown_files = [
        rec["rel_path"] for rec in data["files"]
        if rec["hint"]["episode_type"] == "unknown"
    ]
    return {
        "root": data["root"],
        "video_count": data["video_count"],
        "directory_candidates": candidates,
        "special_hint_count": len(special_files),
        "special_hint_files": special_files,
        "unknown_hint_count": len(unknown_files),
        "unknown_hint_files": unknown_files,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
    }


def _probe_special_hints(data: dict) -> int:
    """仅 probe 机械初判的特殊件；正片仍保持未 probe。"""
    from probe_duration import probe_duration

    root = Path(data["root"])
    count = 0
    for rec in data["files"]:
        if not (rec["hint"]["is_special"] or rec["subdir_special_hint"]):
            continue
        rec["duration"] = probe_duration(root / rec["rel_path"])
        count += 1
    return count


def _write_manifest(data: dict, requested: str | None = None) -> tuple[Path, dict]:
    path = _default_output_path("manifests", requested)
    payload = {
        "schema": "anime-scraper-scan-manifest-v1",
        "generated_at": _utc_now(),
        "summary": _scan_summary(data),
        "scan": data,
    }
    atomic_write_json(path, payload)
    return path, payload


def _print_summary(payload: dict, manifest_path: Path, probed_count: int) -> None:
    summary = payload["summary"]
    candidates = summary["directory_candidates"]
    print(f"扫描完成: {summary['root']}")
    print(f"视频 {summary['video_count']}；目录候选 {len(candidates)}（仅线索，不等同最终单元）")
    for item in candidates[:_SUMMARY_CANDIDATE_LIMIT]:
        if item["normal_episode_min"] is None:
            episode_span = "正片集号 ?"
        else:
            episode_span = (f"正片 {item['normal_episode_min']}-{item['normal_episode_max']}"
                            f"/{item['normal_episode_count']} 件")
        print(f"  - {item['path']}: {item['video_count']} 视频，{episode_span}，"
              f"SP提示 {item['special_hint_count']}，未知 {item['unknown_hint_count']}")
    if len(candidates) > _SUMMARY_CANDIDATE_LIMIT:
        print(f"  …其余 {len(candidates) - _SUMMARY_CANDIDATE_LIMIT} 个候选见 manifest")

    anomalies = summary["anomalies"]
    print(f"特殊集提示 {summary['special_hint_count']}；未知 {summary['unknown_hint_count']}；"
          f"异常线索 {len(anomalies)}；本次 probe {probed_count} 个特殊件")
    for item in anomalies[:_SUMMARY_ANOMALY_LIMIT]:
        print(f"  ! [{item['kind']}] {item['path']}: {item['details']}")
    if len(anomalies) > _SUMMARY_ANOMALY_LIMIT:
        print(f"  …其余 {len(anomalies) - _SUMMARY_ANOMALY_LIMIT} 项见 manifest")
    print(f"完整 manifest: {manifest_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="扫描动画目录并保存完整结构 manifest")
    parser.add_argument("folder", nargs="?", help="源目录；省略时读取 config paths.source_root")
    parser.add_argument("--manifest", metavar="PATH", help="完整 manifest 路径；默认写系统临时目录")
    parser.add_argument("--probe-specials", action="store_true",
                        help="仅对正则/目录初判的特殊件读取时长；默认不 probe")
    parser.add_argument("--verbose", action="store_true",
                        help="显式把完整 manifest 也打印到终端（大包可能触发输出截断）")
    args = parser.parse_args(argv)

    data = scan_tree(args.folder)
    probed_count = _probe_special_hints(data) if args.probe_specials else 0
    manifest_path, payload = _write_manifest(data, args.manifest)
    _print_summary(payload, manifest_path, probed_count)
    if args.verbose:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

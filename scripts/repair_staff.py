"""增量修复作品级 Bangumi staff，只写 plan 与作品级 NFO。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import bangumi
import match
import nfo


def _same_inode(left: Path, right: Path) -> bool:
    a = os.stat(left)
    b = os.stat(right)
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _show_section(plan: dict) -> dict:
    section = plan.get("movie") if plan.get("type") == "movie" else plan.get("show")
    if not isinstance(section, dict):
        raise ValueError("plan 缺少可维护的 show/movie 对象")
    return section


def _load_cache(path: str, kind: str) -> list[dict]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    normalizer = (bangumi.normalize_characters if kind == "characters"
                  else bangumi.normalize_persons)
    return normalizer(raw)


def _primary_nfo_path(plan: dict) -> Path:
    if plan.get("type") == "movie":
        section = _show_section(plan)
        video_path = section.get("video_path")
        if video_path:
            return Path(video_path).with_suffix(".nfo")
        return Path(plan["output_dir"]) / "movie.nfo"
    return Path(plan["output_dir"]) / "tvshow.nfo"


def _build_nfo(plan: dict) -> str:
    if plan.get("type") == "movie":
        show, _ = match.assemble_movie(plan)
        return nfo.build_movie_nfo(show)
    show, _ = match.assemble(plan)
    return nfo.build_tvshow_nfo(show)


def _write_plan_after_nfo(plan_path: Path, payload: str, nfo_path: Path,
                          expected_nfo: str, library_nfo: Path | None) -> None:
    temporary = plan_path.with_name(plan_path.name + ".staff.tmp")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    try:
        if nfo_path.read_text(encoding="utf-8") != expected_nfo:
            nfo.write_nfo_text(nfo_path, expected_nfo)
        if nfo_path.read_text(encoding="utf-8") != expected_nfo:
            raise OSError(f"作品级 NFO 写入后内容不一致: {nfo_path}")
        if library_nfo is not None:
            if not _same_inode(nfo_path, library_nfo):
                raise OSError("源侧与库侧 tvshow.nfo 不再是同一硬链接 inode")
            if library_nfo.read_text(encoding="utf-8") != expected_nfo:
                raise OSError("库侧 tvshow.nfo 内容与期望不一致")
        os.replace(temporary, plan_path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, help="已有 unit plan JSON")
    parser.add_argument("--persons-cache", required=True,
                        help="Bangumi persons_<bgm_id>.json 缓存")
    parser.add_argument("--characters-cache",
                        help="Bangumi characters_<bgm_id>.json 缓存；提供时同时恢复声优")
    parser.add_argument("--offline-cache", action="store_true",
                        help="只使用给定缓存中的原名和头像，不请求人员/角色 detail")
    parser.add_argument("--library-nfo", help="已存在的库侧 tvshow/movie NFO，用于硬链接校验")
    parser.add_argument("--apply", action="store_true", help="写回 plan 与作品级 NFO")
    args = parser.parse_args(argv)

    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    persons = _load_cache(args.persons_cache, "persons")
    characters = (_load_cache(args.characters_cache, "characters")
                  if args.characters_cache else None)
    section = _show_section(plan)
    before = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    unresolved: list[dict] = []
    match.populate_show_staff(
        section, persons, characters=characters,
        localize=not args.offline_cache, unresolved=unresolved,
    )
    after = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    nfo_path = _primary_nfo_path(plan)
    library_nfo = Path(args.library_nfo) if args.library_nfo else None
    if library_nfo is not None:
        if not nfo_path.exists() or not library_nfo.exists():
            raise FileNotFoundError("源侧和库侧作品级 NFO 都必须存在才能增量修复")
        if not _same_inode(nfo_path, library_nfo):
            raise OSError("增量修复要求源侧与库侧作品级 NFO 先共享同一硬链接 inode")
    expected_nfo = _build_nfo(plan)

    print(f"voice={sum((c.get('type') or 'Actor') == 'Actor' for c in section.get('actors', []) if isinstance(c, dict))}; "
          f"staff_status={section.get('staff_status')}; "
          f"crew={sum((c.get('type') or 'Actor') != 'Actor' for c in section.get('actors', []) if isinstance(c, dict))}; "
          f"staff_note={'present' if section.get('staff_note') else 'empty'}; "
          f"mappable_crew={section.get('staff_audit', {}).get('mappable_crew_count')}; "
          f"unresolved_names={len(unresolved)}; "
          f"staff_note_in_nfo={bool(section.get('staff_note') and section['staff_note'] in expected_nfo)}")
    print(f"变更范围: plan 1 个、作品级 NFO 1 个；图片/视频/字幕/分集 NFO=0")
    if not args.apply:
        return 0
    if before == after and nfo_path.read_text(encoding="utf-8") == expected_nfo:
        print("无需写入：staff 与作品级 NFO 已是期望状态")
        return 0
    _write_plan_after_nfo(plan_path, after, nfo_path, expected_nfo, library_nfo)
    print(f"已写入: {plan_path}")
    print(f"已更新: {nfo_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

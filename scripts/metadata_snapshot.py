"""Collect selected Bangumi/AniDB/TMDB metadata into one local JSON snapshot.

The caller supplies already-confirmed identifiers.  This CLI only orchestrates
the existing cached, rate-limited module functions; it does not identify a
series, choose metadata, or modify media files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import anidb_episodes
import bangumi
import tmdb
from _common import atomic_write_json, load_config


SCHEMA = "anime-scraper-metadata-v1"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ID 必须是正整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("ID 必须是正整数")
    return parsed


def _unique(values: list[int], label: str) -> list[int]:
    result = list(dict.fromkeys(values))
    if len(result) != len(values):
        raise ValueError(f"{label} 不得重复")
    return result


def _unique_pairs(values: list[tuple[int, int]], label: str) -> list[tuple[int, int]]:
    result = list(dict.fromkeys(values))
    if len(result) != len(values):
        raise ValueError(f"{label} 不得重复")
    return result


def _parse_tv_season(value: str) -> tuple[int, int]:
    raw_tv, separator, raw_season = value.partition(":")
    if not separator or not raw_tv or not raw_season:
        raise argparse.ArgumentTypeError("季参数必须是 TV_ID:SEASON，例如 65329:0")
    try:
        tv_id = int(raw_tv)
        season = int(raw_season)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "季参数必须是 TV_ID:SEASON，例如 65329:0"
        ) from exc
    if tv_id < 1 or season < 0:
        raise argparse.ArgumentTypeError(
            "季参数必须是正 TV_ID 加非负 SEASON，例如 65329:0"
        )
    return tv_id, season


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="快照 JSON 输出路径")
    parser.add_argument(
        "--bgm-id", type=_positive_int, action="append", default=[],
        help="已确认的 Bangumi subject ID，可重复传入",
    )
    parser.add_argument(
        "--anidb-aid", type=_positive_int, action="append", default=[],
        help="已确认的 AniDB aid，可重复传入",
    )
    parser.add_argument(
        "--tmdb-tv-id", type=_positive_int, action="append", default=[],
        help="已确认的 TMDB TV ID，可重复传入；读取详情",
    )
    parser.add_argument(
        "--tmdb-movie-id", type=_positive_int, action="append", default=[],
        help="已确认的 TMDB movie ID，可重复传入；读取详情",
    )
    parser.add_argument(
        "--tmdb-season", type=_parse_tv_season, action="append", default=[],
        metavar="TV_ID:SEASON",
        help="读取指定 TMDB 季分集，例如 65329:0；可重复传入",
    )
    parser.add_argument(
        "--tmdb-tv-images", type=_positive_int, action="append", default=[],
        metavar="TV_ID", help="读取 TMDB TV 图片候选",
    )
    parser.add_argument(
        "--tmdb-season-images", type=_parse_tv_season, action="append", default=[],
        metavar="TV_ID:SEASON", help="读取指定 TMDB 季图片候选",
    )
    parser.add_argument(
        "--tmdb-movie-images", type=_positive_int, action="append", default=[],
        metavar="MOVIE_ID", help="读取 TMDB movie 图片候选",
    )
    return parser.parse_args(argv)


def _new_snapshot(args: argparse.Namespace) -> dict:
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "bgm_ids": args.bgm_id,
            "anidb_aids": args.anidb_aid,
            "tmdb_tv_ids": args.tmdb_tv_id,
            "tmdb_movie_ids": args.tmdb_movie_id,
            "tmdb_seasons": [
                {"tv_id": tv_id, "season": season}
                for tv_id, season in args.tmdb_season
            ],
            "tmdb_tv_images": args.tmdb_tv_images,
            "tmdb_season_images": [
                {"tv_id": tv_id, "season": season}
                for tv_id, season in args.tmdb_season_images
            ],
            "tmdb_movie_images": args.tmdb_movie_images,
        },
        "bangumi": {"subjects": {}},
        "anidb": {"episodes": {}},
        "tmdb": {"tv": {}, "movie": {}},
    }


def collect(args: argparse.Namespace, cfg: dict | None = None) -> dict:
    """Collect only the explicitly requested resources through source modules."""
    args.bgm_id = _unique(args.bgm_id, "bgm-id")
    args.anidb_aid = _unique(args.anidb_aid, "anidb-aid")
    args.tmdb_tv_id = _unique(args.tmdb_tv_id, "tmdb-tv-id")
    args.tmdb_movie_id = _unique(args.tmdb_movie_id, "tmdb-movie-id")
    args.tmdb_season = _unique_pairs(args.tmdb_season, "tmdb-season")
    args.tmdb_tv_images = _unique(args.tmdb_tv_images, "tmdb-tv-images")
    args.tmdb_season_images = _unique_pairs(
        args.tmdb_season_images, "tmdb-season-images"
    )
    args.tmdb_movie_images = _unique(args.tmdb_movie_images, "tmdb-movie-images")
    if not any((
        args.bgm_id, args.anidb_aid, args.tmdb_tv_id, args.tmdb_movie_id,
        args.tmdb_season, args.tmdb_tv_images, args.tmdb_season_images,
        args.tmdb_movie_images,
    )):
        raise ValueError("至少提供一个 Bangumi/AniDB/TMDB ID 或季参数")

    cfg = cfg or load_config()
    snapshot = _new_snapshot(args)

    for subject_id in args.bgm_id:
        snapshot["bangumi"]["subjects"][str(subject_id)] = {
            "subject": bangumi.get_subject(subject_id, cfg),
            "episodes": bangumi.get_episodes(subject_id, cfg),
            "characters": bangumi.get_characters(subject_id, cfg),
            "persons": bangumi.get_persons(subject_id, cfg),
            "themes": bangumi.get_theme_songs(subject_id, cfg),
        }

    for aid in args.anidb_aid:
        snapshot["anidb"]["episodes"][str(aid)] = anidb_episodes.get_episodes(aid, cfg)

    for tv_id in args.tmdb_tv_id:
        snapshot["tmdb"]["tv"][str(tv_id)] = {"detail": tmdb.get_tv_detail(tv_id, cfg)}

    for tv_id, season in args.tmdb_season:
        entry = snapshot["tmdb"]["tv"].setdefault(str(tv_id), {})
        seasons = entry.setdefault("seasons", {})
        get_episodes = (
            tmdb.get_optional_season_episodes
            if season == 0 else tmdb.get_season_episodes
        )
        seasons[str(season)] = {
            "episodes": get_episodes(tv_id, season, cfg),
        }

    for tv_id in args.tmdb_tv_images:
        entry = snapshot["tmdb"]["tv"].setdefault(str(tv_id), {})
        entry["images"] = tmdb.get_tv_images(tv_id, cfg)

    for tv_id, season in args.tmdb_season_images:
        entry = snapshot["tmdb"]["tv"].setdefault(str(tv_id), {})
        seasons = entry.setdefault("seasons", {})
        get_images = (
            tmdb.get_optional_season_images
            if season == 0 else tmdb.get_season_images
        )
        seasons.setdefault(str(season), {})["images"] = get_images(tv_id, season, cfg)

    for movie_id in args.tmdb_movie_id:
        snapshot["tmdb"]["movie"][str(movie_id)] = {
            "detail": tmdb.get_movie_detail(movie_id, cfg),
        }

    for movie_id in args.tmdb_movie_images:
        entry = snapshot["tmdb"]["movie"].setdefault(str(movie_id), {})
        entry["images"] = tmdb.get_movie_images(movie_id, cfg)

    return snapshot


def _summary(snapshot: dict) -> str:
    bangumi_count = len(snapshot["bangumi"]["subjects"])
    anidb_count = len(snapshot["anidb"]["episodes"])
    tv_count = len(snapshot["tmdb"]["tv"])
    movie_count = len(snapshot["tmdb"]["movie"])
    return (
        f"Bangumi {bangumi_count}；AniDB {anidb_count}；"
        f"TMDB TV {tv_count}；TMDB movie {movie_count}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    snapshot = collect(args)
    output = atomic_write_json(args.output, snapshot, sort_keys=True)
    print(f"元数据快照完成: {output}")
    print(f"  {_summary(snapshot)}")
    print("  只读取显式 ID；数据源模块自行复用缓存与限流")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

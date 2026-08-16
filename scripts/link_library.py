"""将已确认的 plan 投影为 Jellyfin 混合库的安全硬链接树。

库根是扁平的 Mixed Movies and Shows 根：
    <库根>/<剧名 (年)>/Season 01/... 或 Specials/...
    <库根>/<电影名 (年)>/... 或 extras/...

媒体、字幕、NFO 和图片（由调用方提供时）均只能硬链接；跨卷、目标冲突或
SMB/文件系统不支持硬链接时立即停止，绝不复制。为避免半成品，先在库根内的
staging 目录建立完整树，验证后才原子提升为正式作品目录。
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import images
import nfo
from _common import normalize_path

SUB_EXTS = {".ass", ".ssa", ".srt", ".sup", ".vtt", ".sub", ".idx"}
_ILLEGAL = '\\/:*?"<>|'


def _sanitize(name: str) -> str:
    return "".join(c for c in (name or "") if c not in _ILLEGAL).strip().rstrip(". ")


def _year(show) -> str:
    premiered = show.premiered or ""
    return premiered[:4] if len(premiered) >= 4 else ""


def _show_folder(show) -> str:
    year = _year(show)
    return _sanitize(f"{show.title} ({year})" if year else show.title)


def _path(value: "str | Path") -> Path:
    return Path(normalize_path(value))


def _nearest_existing(path: Path) -> Path:
    """返回 path 自身或其最接近的已存在祖先；不创建目录。"""
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(f"找不到可用于同卷检查的目标祖先目录: {path}")
        current = parent
    return current


def _same_volume(src: Path, destination: Path) -> bool:
    return os.stat(src).st_dev == os.stat(_nearest_existing(destination)).st_dev


def _find_subs(video: Path) -> list[tuple[Path, str]]:
    """找与视频同 stem 的外挂字幕，返回 (路径, 视频 stem 后的后缀)。"""
    found: list[tuple[Path, str]] = []
    try:
        for item in video.parent.iterdir():
            if (item.is_file() and item.suffix.lower() in SUB_EXTS
                    and item.name.startswith(video.stem) and item.name != video.name):
                found.append((item, item.name[len(video.stem):]))
    except OSError as exc:
        raise OSError(f"无法枚举字幕目录 {video.parent}: {exc}") from exc
    return found


def _collect_media(video_path: "str | Path") -> list[Path]:
    video = _path(video_path)
    if not video.is_file():
        raise FileNotFoundError(f"源视频不存在或不是普通文件: {video}")
    return [video, *[sub for sub, _ in _find_subs(video)]]


def _check_sources_same_volume(sources: list[Path], link_root: Path) -> list[dict]:
    """预检源文件与库根同卷；返回可序列化报告，不写任何文件。"""
    root_anchor = _nearest_existing(link_root)
    root_dev = os.stat(root_anchor).st_dev
    entries = []
    errors = []
    for src in sources:
        if not src.is_file():
            errors.append(f"源文件不存在或不是普通文件: {src}")
            continue
        src_dev = os.stat(src).st_dev
        same = src_dev == root_dev
        entries.append({"source": str(src), "source_dev": src_dev,
                        "target_anchor": str(root_anchor), "target_dev": root_dev,
                        "same_volume": same})
        if not same:
            errors.append(f"跨卷硬链接被阻断: {src} (dev={src_dev}) → {link_root} (dev={root_dev})")
    if errors:
        raise OSError("; ".join(errors))
    return entries


def _assert_new_destination(destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"拒绝覆盖既有作品目录: {destination}。1.0 不提供既有库结构迁移。")


def _link_new(src: Path, dst: Path) -> None:
    """仅向不存在的 staging 目标创建硬链接，绝不删除或复制。"""
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"staging 目标意外已存在: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as exc:
        raise OSError(
            f"硬链接失败: {src} → {dst}: {exc}。请确认源与库根同卷且 SMB 支持硬链接；不会复制回退。"
        ) from exc


def _stage_dir(link_root: Path, folder: str) -> Path:
    return link_root / f".{folder}.anime-scraper-staging-{uuid.uuid4().hex}"


def _promote(stage: Path, final: Path) -> None:
    """只提升全新作品目录；既有目录一律拒绝，避免无审计覆盖。"""
    _assert_new_destination(final)
    try:
        os.replace(stage, final)
    except OSError as exc:
        raise OSError(f"无法提升 staging 目录 {stage} → {final}: {exc}") from exc


def _verify_link(src: Path, dst: Path) -> None:
    s1, s2 = os.stat(src), os.stat(dst)
    if s1.st_dev != s2.st_dev or s1.st_ino != s2.st_ino:
        raise OSError(f"硬链接验证失败: {src} → {dst}")


def _episode_dir(root: Path, season: int) -> Path:
    return root / ("Specials" if season == 0 else f"Season {season:02d}")


def _episode_base(show, episode) -> str:
    return _sanitize(f"{show.title} S{episode.season:02d}E{episode.episode:02d}")


def _source_nfo_for(source_nfo_paths, key, fallback_text: str | None = None) -> tuple[Path | None, str | None]:
    """返回已写入源 NFO 的路径；直接调用树构建函数时保留文本回退。"""
    if source_nfo_paths and key in source_nfo_paths:
        path = _path(source_nfo_paths[key])
        if not path.is_file():
            raise FileNotFoundError(f"源 NFO 尚未生成: {path}")
        return path, None
    return None, fallback_text


def _write_or_link_nfo(source: Path | None, text: str | None, destination: Path) -> None:
    if source:
        _link_new(source, destination)
    elif text is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    else:
        raise ValueError(f"NFO 来源缺失: {destination}")


def _norm_rel(relpath: str) -> str:
    return str(relpath).replace("\\", "/").lstrip("./")


def _is_episode_thumb_relpath(relpath: str) -> bool:
    """是否为分集/extras/电影主片 thumb（排除作品级 thumb.jpg）。"""
    rel = _norm_rel(relpath)
    name = Path(rel).name
    if name == "thumb.jpg" or not name.endswith("-thumb.jpg"):
        return False
    # 电影主片: 根目录「片名 (年)-thumb.jpg」；分集/extras: 在季目录或 extras 下
    parent = Path(rel).parent.as_posix()
    if parent in (".", ""):
        return True
    return parent == "extras" or parent == "Specials" or parent.startswith("Season ")


def _expected_thumb_relpaths(show, *, kind: str, video_paths=None,
                             extras_paths=None, movie_video=None) -> set[str]:
    """库侧视频 stem 对应的分集/电影 thumb 相对路径集合。

    TV 视频 stem = ``{title} SxxExx``（**无年份**，见 ``_episode_base``）；
    电影主片 stem = ``{title} (年)``（与文件夹名相同，见 ``_show_folder``）。
    """
    expected: set[str] = set()
    if kind == "tv":
        paths = list(video_paths or [])
        for index, episode in enumerate(show.episodes):
            if index < len(paths) and not paths[index]:
                continue
            # video_paths 与 episodes 对齐；缺省时仍按有 video 的契约生成期望
            if video_paths is not None and (index >= len(paths) or not paths[index]):
                continue
            base = _episode_base(show, episode)
            season_dir = "Specials" if episode.season == 0 else f"Season {episode.season:02d}"
            expected.add(f"{season_dir}/{base}-thumb.jpg")
    else:
        folder = _show_folder(show)
        if movie_video:
            expected.add(f"{folder}-thumb.jpg")
        for index, episode in enumerate(show.episodes):
            if extras_paths is not None:
                if index >= len(extras_paths) or not extras_paths[index]:
                    continue
            base = _episode_base(show, episode)
            expected.add(f"extras/{base}-thumb.jpg")
    return expected


def validate_thumb_library_relpaths(show, artwork: list[dict] | None, *, kind: str,
                                    video_paths=None, extras_paths=None,
                                    movie_video=None) -> None:
    """硬规则：分集 thumb 的 library_relpath stem 必须等于库侧视频 stem。

    若 plan 写成 ``片名 (年) S01E01-thumb.jpg``，而 ``link_library`` 视频是
    ``片名 S01E01.mkv``，Jellyfin 将按 stem 对不上图。
    dry-run / preflight 阶段直接 ValueError，禁止落盘。

    只拦「写错名」；缺 thumb 仍由 agent/阶段四负责（本函数不强制 artwork 完备）。
    """
    expected = _expected_thumb_relpaths(
        show, kind=kind, video_paths=video_paths,
        extras_paths=extras_paths, movie_video=movie_video,
    )
    errors: list[str] = []
    for item in artwork or []:
        relpath = item.get("library_relpath")
        if not relpath:
            continue
        rel = _norm_rel(relpath)
        if not _is_episode_thumb_relpath(rel):
            continue
        if rel not in expected:
            stem = Path(rel).name[: -len("-thumb.jpg")]
            yearish = " (" in stem and stem.rstrip(")").split(" (")[-1].isdigit()
            hint = (
                "（疑似把带年份的文件夹名写进了分集 thumb；TV 视频 stem 不含年份）"
                if yearish and kind == "tv"
                else ""
            )
            sample = next(iter(sorted(expected)), "(无对应视频的期望路径)")
            errors.append(
                f"分集 thumb library_relpath 与库侧视频 stem 不一致: {rel}；"
                f"期望例如 {sample}{hint}"
            )
    if errors:
        raise ValueError("；".join(errors))


def validate_thumb_completeness(show, artwork: list[dict] | None, *, kind: str,
                                video_paths=None, extras_paths=None,
                                movie_video=None) -> None:
    """硬规则：每个有 video_path 的 episode 在 artwork 中必须有对应 thumb。

    龙与虎教训：agent 只给有 TMDB still 的特殊集写了 artwork，漏掉
    tmdb_match_status=not_found 的 9 个应截帧的 → 库里视频旁无 thumb，
    Jellyfin 联网乱拉图。dry-run 即 ValueError，禁止落盘。
    """
    expected = _expected_thumb_relpaths(
        show, kind=kind, video_paths=video_paths,
        extras_paths=extras_paths, movie_video=movie_video,
    )
    actual = {_norm_rel(item.get("library_relpath", ""))
              for item in (artwork or [])
              if _is_episode_thumb_relpath(_norm_rel(item.get("library_relpath", "")))}
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(
            f"artwork 缺少 {len(missing)} 个分集 thumb（每个有 video_path 的视频"
            f"必须有对应 thumb）：{', '.join(missing[:10])}"
            + ("…" if len(missing) > 10 else ""))


def _artwork_sources(artwork: list[dict] | None, link_root: Path) -> list[Path]:
    """校验 artwork 路径；图片尚未下载时预检其源目录同卷，落盘后再校验实体。"""
    paths: list[Path] = []
    root_dev = os.stat(_nearest_existing(link_root)).st_dev
    seen_relpaths: set[str] = set()
    for item in artwork or []:
        source = item.get("source_path")
        relpath = item.get("library_relpath")
        if not source or not relpath:
            raise ValueError("artwork 必须同时提供 source_path 和 library_relpath")
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts or str(rel) in seen_relpaths:
            raise ValueError(f"非法或重复 artwork library_relpath: {relpath}")
        seen_relpaths.add(str(rel))
        path = _path(source)
        parent = _nearest_existing(path.parent)
        if os.stat(parent).st_dev != root_dev:
            raise OSError(f"跨卷图片源目录被阻断: {path.parent} → {link_root}")
        if path.exists():
            if not path.is_file():
                raise FileNotFoundError(f"源图片不是普通文件: {path}")
            paths.append(path)
    return paths


def _link_artwork(stage: Path, artwork: list[dict] | None) -> int:
    count = 0
    for item in artwork or []:
        rel = Path(item["library_relpath"])
        images.link_image(_path(item["source_path"]), stage / rel)
        count += 1
    return count


def _preflight(show, video_paths: list, link_root, kind: str, source_nfo_paths=None,
               artwork=None) -> dict:
    root = _path(link_root)
    folder = _show_folder(show)
    if not folder:
        raise ValueError("作品标题不能为空，无法生成库目录")
    final = root / folder
    _assert_new_destination(final)

    sources: list[Path] = []
    skipped: list[str] = []
    for episode, raw_path in zip(show.episodes, video_paths):
        if not raw_path:
            skipped.append(episode.title or episode.anidb_epno or f"S{episode.season}E{episode.episode}")
            continue
        sources.extend(_collect_media(raw_path))
    if kind == "movie" and video_paths and video_paths[0]:
        # movie main video is represented by first explicit input, not show.episodes.
        pass
    for path in (source_nfo_paths or {}).values():
        nfo_path = _path(path)
        if nfo_path.is_file():
            sources.append(nfo_path)
    sources.extend(_artwork_sources(artwork, root))
    volume = _check_sources_same_volume(sources, root) if sources else []
    return {
        "kind": kind,
        "link_root": str(root),
        "folder": folder,
        "final_dir": str(final),
        "same_volume": True,
        "sources": volume,
        "skipped": skipped,
    }


def preflight_tv_tree(show, video_paths, link_root, source_nfo_paths=None, artwork=None) -> dict:
    paths = list(video_paths)
    validate_thumb_library_relpaths(show, artwork, kind="tv", video_paths=paths)
    validate_thumb_completeness(show, artwork, kind="tv", video_paths=paths)
    return _preflight(show, paths, link_root, "tv", source_nfo_paths, artwork)


def preflight_movie_tree(show, video_path, link_root, extras_paths=None, source_nfo_paths=None,
                         artwork=None) -> dict:
    root = _path(link_root)
    folder = _show_folder(show)
    if not folder:
        raise ValueError("作品标题不能为空，无法生成库目录")
    final = root / folder
    _assert_new_destination(final)
    extras = list(extras_paths or [])
    validate_thumb_library_relpaths(
        show, artwork, kind="movie", extras_paths=extras, movie_video=video_path)
    validate_thumb_completeness(
        show, artwork, kind="movie", extras_paths=extras, movie_video=video_path)
    sources: list[Path] = []
    skipped: list[str] = []
    if video_path:
        sources.extend(_collect_media(video_path))
    else:
        skipped.append("主电影视频")
    for episode, extra_path in zip(show.episodes, extras):
        if extra_path:
            sources.extend(_collect_media(extra_path))
        else:
            skipped.append(episode.title or episode.anidb_epno or f"S{episode.season}E{episode.episode}")
    for path in (source_nfo_paths or {}).values():
        nfo_path = _path(path)
        if nfo_path.is_file():
            sources.append(nfo_path)
    sources.extend(_artwork_sources(artwork, root))
    volume = _check_sources_same_volume(sources, root) if sources else []
    return {"kind": "movie", "link_root": str(root), "folder": folder,
            "final_dir": str(final), "same_volume": True, "sources": volume,
            "skipped": skipped}


def link_media(video_path: "str | Path", dest_dir: "str | Path", base: str,
               dry_run: bool = False) -> tuple[int, int]:
    """将一个视频及其同 stem 字幕硬链接到 staging 目录；不支持复制回退。"""
    video = _path(video_path)
    destination = _path(dest_dir)
    media = _collect_media(video)
    _check_sources_same_volume(media, _nearest_existing(destination))
    if dry_run:
        return 1, len(media) - 1
    _link_new(video, destination / (base + video.suffix))
    for subtitle, rest in _find_subs(video):
        _link_new(subtitle, destination / (base + rest))
    return 1, len(media) - 1


def build_tv_tree(show, video_paths, link_root, dry_run=False, source_nfo_paths=None,
                  artwork=None):
    """建立扁平 Mixed 根下的 TV staging 树并提升；返回五元组。"""
    report = preflight_tv_tree(show, video_paths, link_root, source_nfo_paths, artwork)
    final = Path(report["final_dir"])
    skipped = report["skipped"]
    written: list[Path] = [final / "tvshow.nfo"]
    for episode, raw_path in zip(show.episodes, video_paths):
        if raw_path:
            written.append(_episode_dir(final, episode.season) / (_episode_base(show, episode) + ".nfo"))
    if dry_run:
        return written, skipped, sum(bool(p) for p in video_paths), 0, final

    stage = _stage_dir(_path(link_root), report["folder"])
    n_video = n_sub = 0
    try:
        source, text = _source_nfo_for(source_nfo_paths, "show", nfo.build_tvshow_nfo(show))
        _write_or_link_nfo(source, text, stage / "tvshow.nfo")
        for index, (episode, raw_path) in enumerate(zip(show.episodes, video_paths)):
            if not raw_path:
                continue
            season_dir = _episode_dir(stage, episode.season)
            base = _episode_base(show, episode)
            video = _path(raw_path)
            _link_new(video, season_dir / (base + video.suffix))
            n_video += 1
            for subtitle, rest in _find_subs(video):
                _link_new(subtitle, season_dir / (base + rest))
                n_sub += 1
            source, text = _source_nfo_for(source_nfo_paths, f"episode:{index}",
                                            nfo.build_episode_nfo(episode))
            _write_or_link_nfo(source, text, season_dir / (base + ".nfo"))
        _link_artwork(stage, artwork)
        _promote(stage, final)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise

    for path in written:
        if not path.exists():
            raise OSError(f"建库后缺失 NFO: {path}")
    return written, skipped, n_video, n_sub, final
def build_movie_tree(show, video_path, link_root, dry_run=False, extras_paths=None,
                     source_nfo_paths=None, artwork=None):
    """建立扁平 Mixed 根下的电影 staging 树并提升；不支持复制回退。"""
    report = preflight_movie_tree(show, video_path, link_root, extras_paths, source_nfo_paths, artwork)
    final = Path(report["final_dir"])
    folder = report["folder"]
    skipped = report["skipped"]
    main_nfo_name = f"{folder}.nfo" if video_path else "movie.nfo"
    written: list[Path] = [final / main_nfo_name]
    for index, (episode, extra_path) in enumerate(zip(show.episodes, extras_paths or [])):
        if extra_path:
            written.append(final / "extras" / (_episode_base(show, episode) + ".nfo"))
    if dry_run:
        return written, skipped, (1 if video_path else 0) + sum(bool(p) for p in extras_paths or []), 0, final

    stage = _stage_dir(_path(link_root), folder)
    n_video = n_sub = 0
    try:
        if video_path:
            video = _path(video_path)
            _link_new(video, stage / (folder + video.suffix))
            n_video += 1
            for subtitle, rest in _find_subs(video):
                _link_new(subtitle, stage / (folder + rest))
                n_sub += 1
            source, text = _source_nfo_for(source_nfo_paths, "movie", nfo.build_movie_nfo(show))
            _write_or_link_nfo(source, text, stage / (folder + ".nfo"))
        else:
            source, text = _source_nfo_for(source_nfo_paths, "movie", nfo.build_movie_nfo(show))
            _write_or_link_nfo(source, text, stage / "movie.nfo")

        for index, (episode, extra_path) in enumerate(zip(show.episodes, extras_paths or [])):
            if not extra_path:
                continue
            extras_dir = stage / "extras"
            base = _episode_base(show, episode)
            video = _path(extra_path)
            _link_new(video, extras_dir / (base + video.suffix))
            n_video += 1
            for subtitle, rest in _find_subs(video):
                _link_new(subtitle, extras_dir / (base + rest))
                n_sub += 1
            source, text = _source_nfo_for(source_nfo_paths, f"extra:{index}",
                                            nfo.build_episode_nfo(episode))
            _write_or_link_nfo(source, text, extras_dir / (base + ".nfo"))
        _link_artwork(stage, artwork)
        _promote(stage, final)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise

    for path in written:
        if not path.exists():
            raise OSError(f"建库后缺失 NFO: {path}")
    return written, skipped, n_video, n_sub, final

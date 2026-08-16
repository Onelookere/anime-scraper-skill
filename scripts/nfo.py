"""输出 Kodi/Jellyfin/Emby 通用的 .nfo。

- tvshow.nfo:作品级
- 每集 .nfo:episodedetails

正常模式:正片放 Season 01,特殊内容放 Specials(Season 00),SxxExx 命名。

auto-identify 模式(episode_paths):nfo 与视频文件同名同目录,媒体库自动关联。
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path

from match import MergedShow, MergedEpisode


def write_nfo_text(path: str | Path, text: str) -> Path:
    """Write and sync one NFO while preserving an existing hardlinked inode."""
    target = Path(path)
    with target.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    return target


def _pretty(elem: ET.Element) -> str:
    """缩进美化后加 XML 声明。"""
    ET.indent(elem, space="  ")
    body = ET.tostring(elem, encoding="unicode")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n"


def _sub(parent: ET.Element, tag: str, text) -> None:
    """仅在有值时写入子节点,避免大量空标签。"""
    if text is None or text == "":
        return
    el = ET.SubElement(parent, tag)
    el.text = str(text)


def _clean_plot(text: str) -> str:
    """折叠多余空行(3+ 连续换行 → 至多 1 个空行),去行尾空白。

    注意：staff 行剥离、非剧情元数据过滤等语义判断由 agent 在组 plan 时完成，
    不在此函数处理。此函数只做格式清洗。
    """
    if not text:
        return text or ""
    lines = [ln.rstrip() for ln in
             str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list = []
    blank = 0
    for ln in lines:
        if ln.strip():
            blank = 0
            out.append(ln)
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


def _compose_plot(show: "MergedShow") -> str:
    """清洗剧情，并在末尾保留作品级 staff 审计行。"""
    plot = _clean_plot(show.plot)
    staff = _clean_plot(getattr(show, "staff_note", ""))
    if not staff:
        return plot
    if not plot:
        return staff
    if staff in plot:
        return plot
    return f"{plot}\n\n{staff}"


def _add_people(root: ET.Element, show: "MergedShow") -> None:
    """写人员字段；声优与 crew 统一是带头像的 actor 卡片。"""
    actors = getattr(show, "actors", None) or []
    for i, a in enumerate(actors):
        name = a.get("name") if isinstance(a, dict) else None
        if not name:
            continue
        el = ET.SubElement(root, "actor")
        _sub(el, "name", name)                          # 声优 / 职员名
        _sub(el, "role", a.get("role"))                 # 角色 / Bangumi 原职位名
        _sub(el, "thumb", a.get("thumb"))               # 头像 URL
        _sub(el, "order", i)
        _sub(el, "type", a.get("type") or "Actor")      # 声优=Actor;crew=真实类型


def build_tvshow_nfo(show: MergedShow) -> str:
    root = ET.Element("tvshow")
    _sub(root, "title", show.title)
    _sub(root, "sorttitle", getattr(show, "sorttitle", ""))
    _sub(root, "rating", getattr(show, "rating", None))
    _sub(root, "plot", _compose_plot(show))
    _sub(root, "premiered", show.premiered)
    _sub(root, "studio", show.studio)
    if getattr(show, "lockdata", False):
        _sub(root, "lockdata", "true")
    _add_people(root, show)
    if show.anidb_aid:
        uid = ET.SubElement(root, "uniqueid", {"type": "anidb"})
        uid.text = str(show.anidb_aid)
    if show.bgm_id:
        uid = ET.SubElement(root, "uniqueid", {"type": "bangumi"})
        uid.text = str(show.bgm_id)
    return _pretty(root)


def build_episode_nfo(ep: MergedEpisode) -> str:
    root = ET.Element("episodedetails")
    _sub(root, "title", ep.title)
    _sub(root, "season", ep.season)
    _sub(root, "episode", ep.episode)
    # TMDB 状态描述交叉认证结果，不决定 XML 节点是否存在。任何空简介都
    # 省略 <plot>，避免生成 <plot />；有简介时照常写入并做格式清洗。
    _sub(root, "plot", _clean_plot(ep.plot))
    _sub(root, "aired", ep.airdate)
    _sub(root, "thumb", getattr(ep, "thumb", None))
    if ep.runtime:
        _sub(root, "runtime", ep.runtime)
    # 每集人员(脚本按集不同):写进本集 <director>/<credits>
    for d in (getattr(ep, "directors", None) or []):
        _sub(root, "director", d)
    for w in (getattr(ep, "writers", None) or []):
        _sub(root, "credits", w)
    # 溯源标记,便于排查(媒体库会忽略未知标签)
    _sub(root, "anidb_epno", ep.anidb_epno)
    return _pretty(root)


def build_movie_nfo(show: MergedShow) -> str:
    """剧场版:输出 <movie> 结构(Kodi/Jellyfin/Emby 通用)。

    一部电影 = 一个 MergedShow(标题/简介取自 Bangumi 该 subject)。
    """
    root = ET.Element("movie")
    _sub(root, "title", show.title)
    _sub(root, "sorttitle", getattr(show, "sorttitle", ""))
    _sub(root, "rating", getattr(show, "rating", None))
    _sub(root, "plot", _compose_plot(show))
    _sub(root, "premiered", show.premiered)
    if show.premiered and len(show.premiered) >= 4:
        _sub(root, "year", show.premiered[:4])
    _sub(root, "studio", show.studio)
    if getattr(show, "lockdata", False):
        _sub(root, "lockdata", "true")
    _add_people(root, show)
    if show.anidb_aid:
        uid = ET.SubElement(root, "uniqueid", {"type": "anidb"})
        uid.text = str(show.anidb_aid)
    if show.bgm_id:
        uid = ET.SubElement(root, "uniqueid", {"type": "bangumi"})
        uid.text = str(show.bgm_id)
    return _pretty(root)


def _season_dir(base: Path, season: int) -> Path:
    return base / ("Specials" if season == 0 else f"Season {season:02d}")


def _episode_basename(ep: MergedEpisode) -> str:
    """占位文件名:SxxExx - 标题。"""
    safe = "".join(c for c in ep.title if c not in r'\/:*?"<>|').strip()
    return f"S{ep.season:02d}E{ep.episode:02d} - {safe}".rstrip(" -")


def write_all(show: MergedShow, output_dir: str | Path,
              episode_paths: list[Path] | None = None) -> list[Path]:
    """把 tvshow.nfo 和每集 nfo 写到 output_dir。

    参数:
      show:        合并后的剧集数据
      output_dir:  输出根目录(tvshow.nfo 放在这里)
      episode_paths: 可选,与 show.episodes 一一对应的原始视频文件路径。
                     提供时,nfo 使用视频文件的同名(不同扩展名)并写在
                     视频文件同目录下;不提供时使用 SxxExx 占位名。

    返回写出的路径列表。
    """
    base = Path(output_dir)
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # ---- tvshow.nfo ----
    tvshow = base / "tvshow.nfo"
    write_nfo_text(tvshow, build_tvshow_nfo(show))
    written.append(tvshow)

    # ---- 每集 nfo ----
    # episode_paths 提供 = "文件为中心"模式:只给有匹配视频的集写(与视频同名同目录),
    #   None 项跳过(由上层记账);不提供 = 标准模式:所有集用 SxxExx 占位名按季分目录。
    file_centric = episode_paths is not None
    for i, ep in enumerate(show.episodes):
        vpath = episode_paths[i] if (file_centric and i < len(episode_paths)) else None
        if file_centric:
            if not vpath:
                continue                       # 无匹配视频 → 跳过,不写占位
            nfo_path = Path(vpath).with_suffix(".nfo")
            nfo_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            d = _season_dir(base, ep.season)
            d.mkdir(parents=True, exist_ok=True)
            nfo_path = d / (_episode_basename(ep) + ".nfo")

        write_nfo_text(nfo_path, build_episode_nfo(ep))
        written.append(nfo_path)

    return written


def write_movie(show: MergedShow, video_path: str | Path | None = None,
                output_dir: str | Path | None = None) -> Path:
    """写单部剧场版 nfo。

    - video_path 提供时:nfo 与视频同名同目录(媒体库自动关联);
    - 否则写到 output_dir/movie.nfo。
    """
    if video_path:
        video_path = Path(video_path)
        nfo_path = video_path.with_suffix(".nfo")
        nfo_path.parent.mkdir(parents=True, exist_ok=True)
    elif output_dir:
        base = Path(output_dir)
        base.mkdir(parents=True, exist_ok=True)
        nfo_path = base / "movie.nfo"
    else:
        raise ValueError("write_movie 需要 video_path 或 output_dir 之一")

    write_nfo_text(nfo_path, build_movie_nfo(show))
    return nfo_path

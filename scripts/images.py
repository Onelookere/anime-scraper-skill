"""图片落地工具：源目录图片实体化、库侧硬链接与阶段四验证。

图片的物理实体只能放在源视频所在目录。Jellyfin 库树中的 poster、fanart、logo、
season 图和每集 thumb 全部由本模块建立硬链接，跨卷或链接失败绝不复制。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _common import decode_image_size, load_config, normalize_path

TMDB_IMG_BASE = "https://image.tmdb.org/t/p/original"
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".m4v", ".webm"}
_UA = "Mozilla/5.0 (anime-scraper)"
DEFAULT_IMAGE_WORKERS = 6
DEFAULT_FRAME_WORKERS = 3
DEFAULT_DOWNLOAD_ATTEMPTS = 4
DEFAULT_DOWNLOAD_BACKOFF = 0.75
DEFAULT_DOWNLOAD_TIMEOUT = 45
DEFAULT_DOWNLOAD_THROTTLE = 0.15
MAX_IMAGE_WORKERS = 16
MAX_FRAME_WORKERS = 8
MAX_DOWNLOAD_BACKOFF = 8.0
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


class _IncompleteDownload(OSError):
    """The server closed the stream before the advertised payload completed."""


def _worker_count(value, *, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"config artwork.{field} 必须是 1..{maximum} 的整数")
    return value


def artwork_worker_config(cfg: dict | None = None) -> dict[str, int]:
    """读取图片并发配置；缺失配置使用偏积极但有上限的默认值。"""
    if cfg is None:
        try:
            cfg = load_config()
        except FileNotFoundError:
            cfg = {}
    artwork = cfg.get("artwork") or {}
    if not isinstance(artwork, dict):
        raise ValueError("config artwork 必须是对象")
    return {
        "tmdb_workers": _worker_count(
            artwork.get("tmdb_workers", DEFAULT_IMAGE_WORKERS),
            field="tmdb_workers", maximum=MAX_IMAGE_WORKERS,
        ),
        "ffmpeg_workers": _worker_count(
            artwork.get("ffmpeg_workers", DEFAULT_FRAME_WORKERS),
            field="ffmpeg_workers", maximum=MAX_FRAME_WORKERS,
        ),
    }


def tmdb_image_url(file_path: str) -> str:
    """TMDB 相对图片路径转 original URL；完整 URL 原样返回。"""
    if not file_path:
        return ""
    if file_path.startswith("http"):
        return file_path
    return TMDB_IMG_BASE + (file_path if file_path.startswith("/") else "/" + file_path)


def _valid_image(path: Path) -> bool:
    """检查图片存在、非空且可由 Pillow 完整解码。"""
    try:
        decode_image_size(path)
    except (OSError, ValueError, EOFError):
        return False
    return True


def _atomic_replace(temp: Path, destination: Path) -> tuple[int, int]:
    """校验临时图片并原子提升，同时返回已校验的实际尺寸。"""
    size = decode_image_size(temp)
    os.replace(temp, destination)
    return size


def _download_temp_path(destination: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return destination.with_name(f".{destination.name}.download-{digest}.part")


def _retry_after_seconds(error: HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _response_status(response) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return int(status or 200)


def _content_range(response) -> tuple[int, int | None] | None:
    value = response.headers.get("Content-Range")
    if not value:
        return None
    match = _CONTENT_RANGE.match(value.strip())
    if not match:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), total


def _content_length(response) -> int | None:
    value = response.headers.get("Content-Length")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _write_response_to_temp(url: str, temp: Path, timeout: int, *, resume: bool = True) -> None:
    offset = temp.stat().st_size if resume and temp.exists() else 0
    headers = {"User-Agent": _UA, "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = Request(url, headers=headers)

    with urlopen(request, timeout=timeout) as response:
        status = _response_status(response)
        resumed = offset > 0 and status == 206
        if offset and status not in {200, 206}:
            raise _IncompleteDownload(
                f"无法恢复图片下载，HTTP status={status}: {url}"
            )

        expected_total = None
        if resumed:
            content_range = _content_range(response)
            if content_range is None or content_range[0] != offset:
                # A stale or mismatched partial file must never be appended to.
                temp.unlink(missing_ok=True)
                return _write_response_to_temp(url, temp, timeout)
            expected_total = content_range[1]
            mode = "ab"
        else:
            # A server that ignores Range returns 200; restart safely.
            mode = "wb"
            temp.parent.mkdir(parents=True, exist_ok=True)
            expected_total = (
                _content_range(response)[1]
                if _content_range(response) is not None
                else _content_length(response)
            )

        with temp.open(mode) as handle:
            while True:
                try:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                except IncompleteRead as error:
                    if error.partial:
                        handle.write(error.partial)
                        handle.flush()
                        os.fsync(handle.fileno())
                    raise
                if not chunk:
                    break
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())

    if expected_total is not None and temp.stat().st_size != expected_total:
        raise _IncompleteDownload(
            f"图片下载不完整: got={temp.stat().st_size} expected={expected_total} url={url}"
        )


def _retry_delay(attempt: int, backoff: float, retry_after: float | None) -> float:
    base = retry_after if retry_after is not None else min(
        MAX_DOWNLOAD_BACKOFF, backoff * (2 ** (attempt - 1))
    )
    return max(0.0, base) + random.uniform(0.0, min(0.25, max(0.0, base) * 0.25))


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, HTTPError):
        return error.code in _RETRYABLE_HTTP_STATUS or error.code == 416
    return isinstance(
        error,
        (_IncompleteDownload, IncompleteRead, URLError, TimeoutError,
         socket.timeout, ConnectionError),
    )


def download_image(url: str, dst: "str | Path", skip_existing: bool = True,
                   throttle: float = DEFAULT_DOWNLOAD_THROTTLE,
                   timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
                   max_attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
                   backoff: float = DEFAULT_DOWNLOAD_BACKOFF,
                   resume: bool = True,
                   return_size: bool = False) -> "bool | tuple[int, int]":
    """下载一张图到源目录，使用同目录临时文件后原子替换。

    返回是否实际下载；已有有效图片且允许跳过时返回 False。无 URL 或下载失败抛异常，
    以阻止不完整库树被 promote。``return_size=True`` 时返回已完整解码的实际尺寸；
    该尺寸来自图片字节，不使用 TMDB API 的 width/height hint。
    """
    if not url:
        raise ValueError("图片 URL 不能为空")
    if max_attempts < 1:
        raise ValueError("max_attempts 必须至少为 1")
    if backoff < 0:
        raise ValueError("backoff 不能为负数")
    destination = Path(normalize_path(dst))
    if skip_existing:
        try:
            existing_size = decode_image_size(destination)
        except (OSError, ValueError, EOFError):
            pass
        else:
            return existing_size if return_size else False
    destination.parent.mkdir(parents=True, exist_ok=True)

    temp = _download_temp_path(destination, url) if resume else destination.with_name(
        f".{destination.name}.download-{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}.part"
    )
    actual_size: tuple[int, int] | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _write_response_to_temp(url, temp, timeout, resume=resume)
            try:
                actual_size = _atomic_replace(temp, destination)
            except OSError as error:
                raise _IncompleteDownload(str(error)) from error
            break
        except HTTPError as error:
            if error.code == 416 and temp.exists() and _valid_image(temp):
                actual_size = _atomic_replace(temp, destination)
                break
            if not _is_retryable(error):
                temp.unlink(missing_ok=True)
                raise
            if error.code == 416:
                temp.unlink(missing_ok=True)
            if attempt >= max_attempts:
                if not resume:
                    temp.unlink(missing_ok=True)
                raise
            retry_after = _retry_after_seconds(error)
            time.sleep(_retry_delay(attempt, backoff, retry_after))
        except (IncompleteRead, URLError, TimeoutError, socket.timeout,
                ConnectionError, _IncompleteDownload) as error:
            if not _is_retryable(error):
                temp.unlink(missing_ok=True)
                raise
            if attempt >= max_attempts:
                if not resume:
                    temp.unlink(missing_ok=True)
                raise
            time.sleep(_retry_delay(attempt, backoff, None))
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    else:
        temp.unlink(missing_ok=True)
        raise OSError(f"图片下载失败: {url}")

    if throttle > 0:
        time.sleep(throttle)
    if actual_size is None:
        raise OSError(f"图片下载失败且未得到实际尺寸: {url}")
    return actual_size if return_size else True


def download_bytes(url: str, *, timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
                   max_attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
                   backoff: float = DEFAULT_DOWNLOAD_BACKOFF,
                   return_size: bool = False) -> bytes | tuple[bytes, tuple[int, int]]:
    """下载并校验图片；可复用首次解码得到的实际尺寸。"""
    with tempfile.TemporaryDirectory(prefix="anime-scraper-image-") as raw_dir:
        path = Path(raw_dir) / "download.image"
        actual_size = download_image(
            url,
            path,
            skip_existing=False,
            throttle=0,
            timeout=timeout,
            max_attempts=max_attempts,
            backoff=backoff,
            return_size=True,
        )
        if not isinstance(actual_size, tuple):
            raise OSError(f"图片下载未返回实际尺寸: {url}")
        payload = path.read_bytes()
        return (payload, actual_size) if return_size else payload


def ffmpeg_thumb(video: "str | Path", thumb: "str | Path", ss: int = 5,
                 skip_existing: bool = True, timeout: int = 60,
                 return_size: bool = False) -> "bool | tuple[int, int]":
    """从源视频截帧到源目录，检查 ffmpeg 退出码后原子替换。"""
    destination = Path(normalize_path(thumb))
    if skip_existing:
        try:
            existing_size = decode_image_size(destination)
        except (OSError, ValueError, EOFError):
            pass
        else:
            return existing_size if return_size else True
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.frame-{uuid.uuid4().hex}.jpg")
    actual_size: tuple[int, int] | None = None
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ss), "-i", normalize_path(video),
             "-vframes", "1", "-q:v", "2", str(temp)],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "ffmpeg 未提供错误信息").strip()
            raise OSError(f"ffmpeg 截帧失败({result.returncode}): {detail}")
        actual_size = _atomic_replace(temp, destination)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"  [ffmpeg_thumb] {video} 失败: {exc}", file=sys.stderr)
        return False
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
    if actual_size is None:
        return False
    return actual_size if return_size else True


def link_image(source: "str | Path", destination: "str | Path") -> None:
    """建立库侧图片硬链接；目标必须不存在，绝不覆盖或复制。"""
    src = Path(normalize_path(source))
    dst = Path(normalize_path(destination))
    if not _valid_image(src):
        raise FileNotFoundError(f"源图片缺失或无效: {src}")
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"拒绝覆盖既有库侧图片: {dst}")
    anchor = dst
    while not anchor.exists():
        if anchor.parent == anchor:
            raise FileNotFoundError(f"找不到目标图片的已有祖先目录: {dst}")
        anchor = anchor.parent
    if os.stat(src).st_dev != os.stat(anchor).st_dev:
        raise OSError(f"跨卷图片硬链接被阻断: {src} → {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as exc:
        raise OSError(f"图片硬链接失败: {src} → {dst}: {exc}；不会复制回退") from exc


def materialize_artwork(item: dict, *, skip_existing: bool = True) -> Path:
    """按 manifest 在源目录生成一张图片，不允许直接写库目录。

    item 必须有 source_path；method=tmdb 需 URL，method=frame 需 fallback_video_path。
    物理实体只落源侧；库侧由 link_library 硬链接投影。

    skip_existing=True（默认）：源侧已有有效图则跳过。
    skip_existing=False（刷新）：强制重下/重截，覆盖源侧契约路径。
    """
    source = Path(normalize_path(item.get("source_path", "")))
    if not source:
        raise ValueError("artwork 缺少 source_path")
    if skip_existing and _valid_image(source):
        return source
    method = item.get("method")
    actual_size: tuple[int, int] | None = None
    if method == "tmdb":
        result = download_image(
            item.get("url", ""), source, skip_existing=False, return_size=True
        )
        if not isinstance(result, tuple):
            raise OSError(f"图片下载未返回实际尺寸: {source}")
        actual_size = result
    elif method == "frame":
        result = ffmpeg_thumb(
            item.get("fallback_video_path", ""), source,
            skip_existing=False, return_size=True,
        )
        if not isinstance(result, tuple):
            raise OSError(f"无法生成截帧缩略图: {source}")
        actual_size = result
    else:
        raise ValueError(f"未知 artwork method: {method}")
    if actual_size is None or actual_size[0] <= 0 or actual_size[1] <= 0:
        raise OSError(f"artwork 实体化后没有有效尺寸: {source}")
    return source


def _artwork_source_key(item: dict) -> str:
    return os.path.normcase(os.path.normpath(normalize_path(item.get("source_path", ""))))


def _artwork_signature(item: dict) -> tuple:
    return (
        item.get("method"),
        item.get("url"),
        item.get("fallback_video_path"),
    )


def _run_artwork_group(items: list[dict], *, method: str, workers: int,
                       skip_existing: bool, progress: bool) -> list[Path]:
    if not items:
        return []
    maximum = MAX_IMAGE_WORKERS if method == "tmdb" else MAX_FRAME_WORKERS
    _worker_count(workers, field=f"{method}_workers", maximum=maximum)
    results: list[Path | None] = [None] * len(items)

    def run(index: int, item: dict) -> Path:
        try:
            return materialize_artwork(item, skip_existing=skip_existing)
        except Exception as error:
            source = item.get("source_path", "")
            raise OSError(f"{method} artwork 失败: {source}: {error}") from error

    executor = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    futures = {
        executor.submit(run, index, item): index
        for index, item in enumerate(items)
    }
    try:
        completed = 0
        for future in as_completed(futures):
            index = futures[future]
            results[index] = future.result()
            completed += 1
            if progress:
                print(
                    f"  artwork[{method}] {completed}/{len(items)}: "
                    f"{Path(items[index].get('source_path', '')).name}",
                    file=sys.stderr,
                )
    except BaseException:
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return [path for path in results if path is not None]


def materialize_artwork_batch(items: list[dict], *, skip_existing: bool = True,
                              image_workers: int | None = None,
                              frame_workers: int | None = None,
                              progress: bool = True) -> list[Path]:
    """实体化 artwork，按 TMDB/截帧分别限制并发并恢复中断下载。

    同一 source_path 可能被 poster 与 season poster 共享，先去重避免并发写同一
    文件；不同 method/url 的冲突则直接拒绝。源侧所有任务完成后，调用方才可建库。
    """
    configured = artwork_worker_config()
    image_workers = (configured["tmdb_workers"]
                     if image_workers is None else
                     _worker_count(image_workers, field="tmdb_workers",
                                   maximum=MAX_IMAGE_WORKERS))
    frame_workers = (configured["ffmpeg_workers"]
                     if frame_workers is None else
                     _worker_count(frame_workers, field="ffmpeg_workers",
                                   maximum=MAX_FRAME_WORKERS))

    unique: dict[str, tuple[dict, list[int]]] = {}
    for index, item in enumerate(items):
        key = _artwork_source_key(item)
        existing = unique.get(key)
        if existing is None:
            unique[key] = (item, [index])
            continue
        first, indexes = existing
        if _artwork_signature(first) != _artwork_signature(item):
            raise ValueError(f"同一 artwork source_path 的下载契约冲突: {item.get('source_path')}")
        indexes.append(index)

    grouped: dict[str, list[tuple[int, dict, list[int]]]] = {"tmdb": [], "frame": []}
    for item, indexes in unique.values():
        method = item.get("method")
        if method not in grouped:
            raise ValueError(f"未知 artwork method: {method}")
        grouped[method].append((len(grouped[method]), item, indexes))

    results: list[Path | None] = [None] * len(items)
    for method, workers in (("tmdb", image_workers), ("frame", frame_workers)):
        group = grouped[method]
        group_items = [item for _, item, _ in group]
        paths = _run_artwork_group(
            group_items, method=method, workers=workers,
            skip_existing=skip_existing, progress=progress,
        )
        for path, (_, _, indexes) in zip(paths, group):
            for index in indexes:
                results[index] = path

    if any(path is None for path in results):
        raise OSError("artwork 批量实体化结果不完整")
    return [path for path in results if path is not None]


def verify_thumbs(*dirs: "str | Path", recursive: bool = False) -> list[Path]:
    """返回没有有效 ``-thumb.jpg`` 的视频；不存在目录同样作为明确错误。"""
    missing: list[Path] = []
    for directory in dirs:
        root = Path(normalize_path(directory))
        if not root.is_dir():
            raise FileNotFoundError(f"缩略图验证目录不存在: {root}")
        iterator = root.rglob("*") if recursive else root.iterdir()
        for item in iterator:
            if item.is_file() and item.suffix.lower() in VIDEO_EXTS:
                if not _valid_image(item.parent / (item.stem + "-thumb.jpg")):
                    missing.append(item)
    return missing


def verify_hardlink(src: "str | Path", dst: "str | Path") -> bool:
    """确认 dst 与 src 是同一文件：st_dev 与 st_ino 均一致。"""
    s1 = os.stat(normalize_path(src))
    s2 = os.stat(normalize_path(dst))
    return s1.st_dev == s2.st_dev and s1.st_ino == s2.st_ino


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="*", help="directory to verify recursively")
    args = parser.parse_args(argv)
    for arg in args.root:
        missing = verify_thumbs(arg, recursive=True)
        if missing:
            print(f"[缺 thumb] {len(missing)} 个:")
            for video in missing:
                print(f"  {video}")
        else:
            print(f"[OK] {arg} 内所有视频均有 -thumb.jpg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""公用工具:配置加载、缓存目录、两档请求限流(防 AniDB ban)。

anidb.py / bangumi.py 共用,避免重复。保持无副作用、易读。

额外承担两个 Windows 实操自愈:
- 导入本模块即把 stdout/stderr 切成 UTF-8,防 GBK 控制台打日文/中文炸
  UnicodeEncodeError(无需再记 `python -X utf8`);
- normalize_path() 把 UNC 反斜杠形式统一为正斜杠形式——反斜杠 UNC 会使
  scan_tree(被解析成 C:\\server\\...)、pathlib.write_text(FileNotFoundError)、
  --link-root(WinError 17)三处出错,一律先过这个函数。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


def ensure_utf8_stdout() -> None:
    """把 stdout/stderr 重配为 UTF-8(幂等)。Windows GBK 控制台的自愈。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and stream.encoding and stream.encoding.lower() != "utf-8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # 非常规流(如被测试替换)时静默跳过


ensure_utf8_stdout()   # 导入即生效:所有 import _common 的脚本自动获得 UTF-8 输出


def normalize_path(p: "str | Path") -> str:
    r"""UNC/本地路径统一为正斜杠字符串形式。

    - `\\server\share\...` → `//server/share/...`(反斜杠 UNC 的三处炸点见模块头)
    - 普通本地路径也统一正斜杠,Windows API 两种都认,正斜杠不会被吞。
    """
    s = str(p)
    if s.startswith("\\\\"):
        s = "//" + s[2:]
    return s.replace("\\", "/")


def atomic_write_bytes(path: "str | Path", payload: bytes) -> Path:
    """Write bytes beside the target, then replace it atomically."""
    target = Path(normalize_path(path)).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def atomic_write_json(path: "str | Path", payload, *, ensure_ascii: bool = False,
                      sort_keys: bool = False, default=None) -> Path:
    """Serialize JSON before atomically replacing an existing file."""
    dump_options = {
        "ensure_ascii": ensure_ascii,
        "indent": 2,
        "sort_keys": sort_keys,
    }
    if default is not None:
        dump_options["default"] = default
    body = (json.dumps(payload, **dump_options) + "\n").encode("utf-8")
    return atomic_write_bytes(path, body)


def decode_image_size(source: "bytes | str | Path") -> tuple[int, int]:
    """Fully decode an image from bytes or a path and return its pixel size."""
    from io import BytesIO

    from PIL import Image

    if isinstance(source, bytes):
        if not source:
            raise OSError("image payload is empty")
        image_source = BytesIO(source)
    else:
        path = Path(normalize_path(source)).expanduser()
        if not path.is_file() or path.stat().st_size <= 0:
            raise OSError(f"image file is empty or missing: {path}")
        image_source = path
    with Image.open(image_source) as image:
        image.load()
        size = image.size
    if size[0] <= 0 or size[1] <= 0:
        raise OSError("image dimensions are invalid")
    return size


# skill 根目录 = 本文件的上一级(scripts/ 的父目录)
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LINK_DIRNAME = "_Jellyfin"


def config_path() -> Path:
    """返回 skill 根目录的公开 config.json。"""
    return ROOT / "config.json"


def load_config() -> dict:
    """读取 skill 根目录的 config.json；不存在则提示直接创建并填写。"""
    path = config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"未找到配置文件 {path}\n"
            "请在该路径创建 config.json 并填写配置。"
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def multimodal_artwork_review_enabled(cfg: dict | None = None) -> bool:
    """返回海报多模态开关；缺失默认关闭，且只接受 JSON 布尔值。"""
    effective_cfg = cfg if cfg is not None else load_config()
    value = (effective_cfg.get("artwork") or {}).get("multimodal_review", False)
    if type(value) is not bool:
        raise ValueError("config artwork.multimodal_review 必须是 true 或 false")
    return value


def artwork_cache_enabled(cfg: dict | None = None) -> bool:
    """返回人工原图缓存开关；缺失默认关闭，且只接受 JSON 布尔值。"""
    effective_cfg = cfg if cfg is not None else load_config()
    artwork = effective_cfg.get("artwork") or {}
    if not isinstance(artwork, dict):
        raise ValueError("config artwork 必须是对象")
    value = artwork.get("artwork_cache", False)
    if type(value) is not bool:
        raise ValueError("config artwork.artwork_cache 必须是 true 或 false")
    return value


def hardlink_library_enabled(cfg: dict | None = None) -> bool:
    """返回硬链接库总开关；缺失默认开启，且只接受 JSON 布尔值。"""
    if cfg is None:
        try:
            cfg = load_config()
        except FileNotFoundError:
            # 显式 --link-root 的离线调用仍可继续；link_root() 会校验目标。
            cfg = {}
    library = cfg.get("library") or {}
    if not isinstance(library, dict):
        raise ValueError("config library.hardlinks 必须是对象")
    hardlinks = library.get("hardlinks") or {}
    if not isinstance(hardlinks, dict):
        raise ValueError("config library.hardlinks 必须是对象")
    value = hardlinks.get("enabled", True)
    if type(value) is not bool:
        raise ValueError("config library.hardlinks.enabled 必须是 true 或 false")
    return value


def source_root(cfg: dict | None = None, override: "str | Path | None" = None,
                *, required: bool = True) -> Path | None:
    """返回动画源根目录；显式参数优先，否则读取 ``paths.source_root``。"""
    if override is not None and str(override).strip():
        return Path(normalize_path(override)).expanduser()
    effective_cfg = cfg if cfg is not None else load_config()
    configured = (effective_cfg.get("paths") or {}).get("source_root")
    raw = configured
    if raw is None or not str(raw).strip():
        if required:
            raise ValueError(
                "未配置动画源目录：请在 skill 根目录的 config.json 中填写 paths.source_root，"
                "或在命令行显式传入源目录。"
            )
        return None
    return Path(normalize_path(raw)).expanduser()


def link_root(cfg: dict | None = None, override: "str | Path | None" = None,
              *, resolved_source_root: "str | Path | None" = None,
              required: bool = True) -> Path | None:
    """返回硬链接库根。

    优先级：显式参数 > ``library.hardlinks.root`` > ``<source_root>/_Jellyfin``。
    空 root 表示使用片源同级回退。
    """
    if override is not None and str(override).strip():
        candidate = Path(normalize_path(override)).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"硬链接目标目录必须是绝对路径: {override}")
        return candidate
    effective_cfg = cfg if cfg is not None else load_config()
    library = effective_cfg.get("library") or {}
    if not isinstance(library, dict):
        raise ValueError("config library.hardlinks 必须是对象")
    hardlinks = library.get("hardlinks") or {}
    if not isinstance(hardlinks, dict):
        raise ValueError("config library.hardlinks 必须是对象")
    configured = hardlinks.get("root")
    if configured is not None and str(configured).strip():
        candidate = Path(normalize_path(configured)).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"config library.hardlinks.root 必须是绝对路径: {configured}")
        return candidate

    src = (Path(normalize_path(resolved_source_root)).expanduser()
           if resolved_source_root is not None and str(resolved_source_root).strip()
           else source_root(effective_cfg, required=required))
    if src is None:
        return None
    return src / DEFAULT_LINK_DIRNAME


def cache_dir(cfg: dict, sub: str) -> Path:
    """返回并创建可随 skill 迁移的 API 缓存子目录。"""
    configured = cfg.get("cache_dir")
    if configured and str(configured).strip():
        candidate = Path(normalize_path(configured)).expanduser()
        base = candidate if candidate.is_absolute() else ROOT / candidate
    else:
        base = ROOT / "cache"
    d = base / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


class RateLimiter:
    """AniDB 友好的两档间隔限流,思路移植自 ShokoServer 的 HttpRateLimiter。

    为什么不是简单 sleep 固定秒数:
      - 平时两次请求至少隔 short_delay 秒(够用、不拖慢);
      - 但只要"持续活跃"(连续刮)累计超过 slow_after 秒,就说明在批量猛刮,
        自动切到更慢的 long_delay,大幅降低被 ban 概率;
      - 一旦空闲超过 reset_after 秒,复位回快档。
    再在算出的等待上多加一点 buffer 冗余。ShokoServer 实测 HTTP ban = 12 小时,
    宁可慢一点也别踩线。

    重要:本类靠实例状态(上次请求时刻)工作,**必须跨请求复用同一个实例**,
    否则两档逻辑失效、限流形同虚设。见 anidb.py / bangumi.py 的模块级单例。
    """

    def __init__(self, short_delay: float = 2.0, long_delay: float = 6.0,
                 slow_after: float = 30.0, reset_after: float = 60.0,
                 buffer: float = 0.05):
        self.short_delay = float(short_delay)
        self.long_delay = float(long_delay)
        self.slow_after = float(slow_after)
        self.reset_after = float(reset_after)
        self.buffer = float(buffer)
        self._last_request: float | None = None   # 上次请求的时刻(单调时钟)
        self._active_since: float | None = None    # 本轮"连续活跃"的起点

    @classmethod
    def from_config(cls, cfg: dict | None) -> "RateLimiter":
        """从 config 的 rate 段构建。"""
        cfg = cfg or {}
        r = cfg.get("rate", {}) or {}
        short = r.get("base_interval_sec", 2.0)
        return cls(
            short_delay=short,
            long_delay=r.get("long_interval_sec", float(short) * 3),
            slow_after=r.get("slow_after_sec", 30.0),
            reset_after=r.get("reset_after_sec", 60.0),
        )

    def wait(self) -> None:
        """在发起真实请求前调用:按当前档位补足间隔。"""
        now = time.monotonic()

        # 首次请求:无需等待,开始计活跃期
        if self._last_request is None:
            self._last_request = now
            self._active_since = now
            return

        idle = now - self._last_request
        # 空闲够久 → 复位活跃期,回到快档
        if idle >= self.reset_after:
            self._active_since = now

        # 连续活跃超过阈值就用慢档,否则快档
        active = now - (self._active_since or now)
        delay = self.long_delay if active > self.slow_after else self.short_delay

        wait_time = delay - idle + self.buffer
        if wait_time > 0:
            time.sleep(wait_time)
        self._last_request = time.monotonic()

"""Create and validate the skill-local Python environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List

from _common import atomic_write_json, config_path


ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
MODULES = ("requests", "PIL", "imagehash")
MIN_PYTHON = (3, 10)
STEP_ZERO_SCHEMA_VERSION = 1


def python_supported(version=sys.version_info) -> bool:
    return tuple(version[:2]) >= MIN_PYTHON


def require_supported_python() -> None:
    if not python_supported():
        required = ".".join(map(str, MIN_PYTHON))
        actual = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise RuntimeError(f"需要 Python {required}+，当前为 {actual}")


def runtime_root() -> Path:
    """Return disposable runtime storage inside the skill directory."""
    return ROOT / ".runtime" / "venvs"


def pip_cache_root() -> Path:
    """Keep pip downloads disposable with the rest of the skill runtime."""
    return ROOT / ".runtime" / "pip-cache"


def step_zero_marker_path() -> Path:
    """Keep the successful preflight marker local and disposable."""
    return ROOT / ".runtime" / "step-zero-complete.json"


def machine_fingerprint() -> str:
    """Return a stable, non-reversible identifier for this host."""
    identity = "\0".join((platform.system(), platform.node(), str(uuid.getnode())))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def step_zero_completed() -> bool:
    try:
        marker = json.loads(step_zero_marker_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        marker.get("schema_version") == STEP_ZERO_SCHEMA_VERSION
        and marker.get("machine_fingerprint") == machine_fingerprint()
    )


def mark_step_zero_complete() -> Path:
    """Persist a completed Step 0 only for the current machine."""
    marker_path = step_zero_marker_path()
    payload = {
        "schema_version": STEP_ZERO_SCHEMA_VERSION,
        "machine_fingerprint": machine_fingerprint(),
    }
    atomic_write_json(marker_path, payload, ensure_ascii=True, sort_keys=True)
    return marker_path


def environment_key() -> str:
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()[:12]
    return f"py{sys.version_info.major}{sys.version_info.minor}-{digest}"


VENV_DIR = runtime_root() / environment_key()


def venv_python() -> Path:
    relative = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    return VENV_DIR / relative


def missing_modules(python: Path) -> List[str]:
    probe = (
        "import importlib.util\n"
        f"names = {MODULES!r}\n"
        "print(' '.join(name for name in names "
        "if importlib.util.find_spec(name) is None))\n"
    )
    result = subprocess.run(
        [str(python), "-c", probe], check=True, text=True, capture_output=True
    )
    return result.stdout.split()


def validated_run_args(run_args: List[str]) -> List[str]:
    """Allow the managed interpreter to run only skill-local Python files."""
    if not run_args:
        raise ValueError("--run 后必须提供脚本路径")

    raw_script, *script_args = run_args
    if raw_script.startswith("-"):
        raise ValueError("--run 只接受 skill 内的 Python 脚本，不能传解释器选项")

    candidate = Path(raw_script)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        script = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"脚本不存在: {raw_script}") from exc

    allowed_roots = ((ROOT / "scripts").resolve(), (ROOT / "tests").resolve())
    if (
        not script.is_file()
        or script.suffix.lower() != ".py"
        or not any(script.is_relative_to(root) for root in allowed_roots)
    ):
        raise ValueError("--run 只允许执行 skill 的 scripts/ 或 tests/ 下的 .py 文件")
    return [str(script), *script_args]


def install() -> Path:
    require_supported_python()
    python = venv_python()
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    pip_env = os.environ.copy()
    pip_env["PIP_CACHE_DIR"] = str(pip_cache_root())
    pip_env["PYTHONUTF8"] = "1"
    pip_env["PYTHONIOENCODING"] = "utf-8"
    subprocess.run(
        [str(python), "-m", "pip", "install", "-r", str(REQUIREMENTS)],
        check=True,
        env=pip_env,
    )
    return python


def main() -> int:
    require_supported_python()
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report missing dependencies")
    group.add_argument("--install", action="store_true", help="create a skill-local venv and install requirements")
    group.add_argument("--python", action="store_true", help="print the interpreter for skill scripts")
    group.add_argument("--config-path", action="store_true", help="print the active config path")
    group.add_argument("--step-zero-status", action="store_true", help="report whether Step 0 is complete on this machine")
    group.add_argument("--mark-step-zero-complete", action="store_true", help="record a completed Step 0 for this machine")
    group.add_argument("--run", nargs=argparse.REMAINDER, help="run a script with the managed interpreter")
    args = parser.parse_args()

    if args.config_path:
        print(config_path())
        return 0

    if args.step_zero_status:
        if step_zero_completed():
            print("Step 0 已完成（本机）")
            return 0
        print("Step 0 未完成或标记不属于本机")
        return 1

    if args.mark_step_zero_complete:
        print(f"已记录本机 Step 0 完成状态: {mark_step_zero_complete()}")
        return 0

    python = venv_python()
    if args.install:
        python = install()
        missing = missing_modules(python)
        if missing:
            raise RuntimeError(f"依赖安装后仍缺少: {', '.join(missing)}")
        print(f"依赖已就绪: {python}")
        return 0

    if args.python:
        print(python)
        return 0 if python.exists() else 1

    if args.run is not None:
        try:
            run_args = validated_run_args(args.run)
        except ValueError as exc:
            print(f"拒绝执行: {exc}")
            return 2
        if not python.exists():
            print("缺少 skill 本地虚拟环境")
            print("请在获授权后运行: python scripts/bootstrap.py --install")
            return 1
        return subprocess.run([str(python), *run_args], check=False).returncode

    if not python.exists():
        print("缺少 skill 本地虚拟环境")
        print("请在获授权后运行: python scripts/bootstrap.py --install")
        return 1
    missing = missing_modules(python)
    if missing:
        print(f"缺少依赖: {', '.join(missing)}")
        print("请在获授权后运行: python scripts/bootstrap.py --install")
        return 1
    print(f"依赖已就绪: {python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

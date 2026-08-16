"""Complete offline test entrypoint; runs all three routed test suites."""
from __future__ import annotations

from smoke_contract import run as run_contract
from smoke_integration import run as run_integration
from smoke_core import run as run_core


def main() -> int:
    print("离线冒烟测试(无需密钥/联网):")
    run_core()
    run_integration()
    run_contract()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

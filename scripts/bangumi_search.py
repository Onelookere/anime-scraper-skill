"""通过 Bangumi 名称搜索动画 subject；只查询并写入 API 缓存，不修改媒体。"""

from __future__ import annotations

import argparse
import json
import sys

from bangumi import search_subjects


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit 必须是正整数") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("limit 必须是正整数")
    return parsed


def _display_name(result: dict) -> str:
    name = (result.get("name") or "").strip()
    name_cn = (result.get("name_cn") or "").strip()
    if name_cn and name and name_cn != name:
        return f"{name_cn} / {name}"
    return name_cn or name or "(untitled)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", nargs="+", help="Bangumi 动画名称关键词")
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=10,
        help="最多显示结果数（默认：10）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="将精简结果以 JSON 输出",
    )
    args = parser.parse_args(argv)
    keyword = " ".join(args.keyword).strip()
    if not keyword:
        parser.error("keyword 不能为空")

    results = search_subjects(keyword, limit=args.limit)
    if args.as_json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"Bangumi search: {keyword}")
    if not results:
        print("  (no results)")
        return 0
    for result in results:
        score = result.get("score")
        score_text = str(score) if score is not None else "-"
        print(
            f"  [{result.get('id')}] {_display_name(result)}"
            f"  {result.get('date') or '-'}  score={score_text}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""Fetch authenticated map markers from the terminal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..paths import PROJECT_ROOT
from ..services.marks import (
    fetch_auth,
    hg_content_path,
    load_json,
    prompt_content,
    read_hg_content,
    validate,
)


def build_parser(prog: str = "nav-grid-editor fetch-marks") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="用账号凭证抓取地图标记（含玩家自建结构）",
    )
    ap.add_argument(
        "content",
        nargs="?",
        help="hg/check 响应里的 data.content（省略则按下面的顺序找）",
    )
    ap.add_argument(
        "--content-file",
        default="",
        help=f"从文件读 content（默认 {hg_content_path()}）",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="输出目录（默认 <仓库>/assets/items/map_auth）",
    )
    ap.add_argument(
        "--exclude-slacklines",
        action="store_true",
        help="把滑索架这类场景装饰滤掉（默认不排除，会收进来）",
    )
    ap.add_argument(
        "--per-level",
        action="store_true",
        help="逐 level 查询（默认只按 mapId 查，实测已覆盖全部 level）",
    )
    ap.add_argument("--raw", action="store_true", help="额外把原始 mark/list 响应 dump 下来")
    ap.add_argument("--skip-validate", action="store_true", help="跳过校验")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir = (
        Path(args.out).resolve()
        if args.out
        else PROJECT_ROOT / "assets" / "items" / "map_auth"
    )

    # 优先级：位置参数 > --content-file > configs/hg_content.txt > 交互输入
    content = (args.content or "").strip()
    if not content and args.content_file:
        try:
            content = (
                Path(args.content_file)
                .read_text(encoding="utf-8")
                .strip()
                .strip('"')
                .strip()
            )
        except OSError as e:
            print(f"读不了 --content-file: {e}", file=sys.stderr)
            return 1
    if not content:
        content = read_hg_content()
        if content:
            print(f"（用 {hg_content_path()} 里的凭证）")
    if not content:
        content = prompt_content()
    if not content:
        print("content 为空", file=sys.stderr)
        return 1

    old_summary = load_json(out_dir / "summary.json")
    old_names = (
        set(load_json(out_dir / "item_names.json") or [])
        if old_summary
        else None
    )

    try:
        stats = fetch_auth(
            out_dir,
            content,
            log=print,
            per_level=args.per_level,
            exclude_slacklines=args.exclude_slacklines,
            raw_dir=(out_dir / "raw") if args.raw else None,
        )
    except Exception as e:  # noqa: BLE001
        print(f"抓取失败: {e}", file=sys.stderr)
        return 1

    if not args.skip_validate:
        summary = load_json(out_dir / "summary.json") or {}
        errors = validate(stats, summary, old_summary, old_names)
        if errors:
            print("\n校验未通过：", file=sys.stderr)
            for error in errors:
                print("  - " + error, file=sys.stderr)
            return 2

    print()
    print("=" * 60)
    print(f"Roles            : {stats['roles']}")
    print(f"Requests         : {stats['requests']}")
    print(f"Item Types       : {stats['items']}")
    print(f"Total Points     : {stats['points']}")
    print(f"saveMarks 条数    : {stats['saved_marks']}")
    print(f"重复坐标         : {stats['duplicates']}")
    print(f"排除滑索架       : {'是' if args.exclude_slacklines else '否（含滑索架）'}")
    print("-" * 60)
    for map_id, subtypes in sorted(stats["structures"].items()):
        for subtype, count in sorted(subtypes.items()):
            print(f"  {map_id:8} {subtype:8}: {count:4} 个")
    print(f"玩家结构点合计   : {stats['structure_points']}")
    print("-" * 60)
    for filename in stats["files"]:
        print(f"Saved            : {out_dir / filename}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

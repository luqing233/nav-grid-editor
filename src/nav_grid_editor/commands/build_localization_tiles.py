"""Export a standalone localization dataset from full Q90 presets and original floor PNGs."""
import argparse
from pathlib import Path

from ..services.maps.localization_tiles import build_localization_tiles, verify_localization_tiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--presets", type=Path, default=Path("assets/tiles/presets"))
    parser.add_argument("--snapshot", type=Path, required=True, help="v1 整图数据包，提供标定、分层目录和导航数据")
    parser.add_argument("--original-layers", type=Path, required=True, help="原始 PNG 分层图目录")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-legacy", action="store_true", help="解码旧整图并生成接缝差异报告")
    args = parser.parse_args()
    build_localization_tiles(args.presets, args.snapshot, args.original_layers, args.output, compare=args.compare_legacy)


if __name__ == "__main__":
    main()

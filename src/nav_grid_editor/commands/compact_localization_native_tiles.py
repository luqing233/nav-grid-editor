"""Export v3 native tiles from a v2 bundle, preserving fine-match pixels exactly."""
import argparse
import json
from pathlib import Path

from ..services.maps.localization_native_compaction import compact_localization_native_tiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compact_localization_native_tiles(args.source, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != "images"}, indent=2))


if __name__ == "__main__":
    main()

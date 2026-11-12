"""Export v2 search rasters by losslessly trimming zero borders from a v1 bundle."""
import argparse
import json
from pathlib import Path

from ..services.maps.localization_search_compaction import compact_localization_tiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = compact_localization_tiles(args.source, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != "images"}, indent=2))


if __name__ == "__main__":
    main()

"""Export the editor's saved navigation and map resources."""
import argparse
import json
import time
from pathlib import Path

from ..services.maps.dataset_export import DatasetExport


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nav-grid-editor export-dataset", description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path.cwd())
    parser.add_argument("--tiles-root", type=Path)
    parser.add_argument("--grid2d-dir", type=Path)
    parser.add_argument("--format", choices=("tiled-v3", "legacy"), default="tiled-v3")
    parser.add_argument("--output", default="")
    args = parser.parse_args(argv)
    job = DatasetExport(args.data_root, args.tiles_root or args.data_root / "assets/tiles",
                        args.grid2d_dir or args.data_root / "assets/nav-grid-data")
    result = job.start(args.output, export_format=args.format)
    if not result["ok"]:
        parser.exit(1, result["error"] + "\n")
    while job.status()["running"]:
        time.sleep(0.1)
    status = job.status()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return int(status["error"] is not None)

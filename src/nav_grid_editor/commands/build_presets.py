"""Build a reproducible Q90 tile preset from local captures without network access."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from ..services.maps.io import preferred_image, save_webp
from ..services.maps.settings import TILE_SIZE, valid_map_zoom
from ..services.maps.tiles import TileStore


def build_presets(tiles_root: Path, output: Path) -> dict:
    tiles_root, output = tiles_root.resolve(), output.resolve()
    if output.exists() or tiles_root == output or tiles_root.is_relative_to(output):
        raise ValueError("预设输出必须是新目录，且不能覆盖素材目录或其父目录")
    store = TileStore(tiles_root)
    plans, maps = [], []
    for name, zooms in sorted(store.list_maps().items()):
        for zoom in sorted(zooms):
            if zoom != '4' or not valid_map_zoom(name, zoom):
                continue
            tiles = list(store.iter_tiles(name, zoom))
            if not tiles:
                continue
            xs, ys = [t[0] for t in tiles], [t[1] for t in tiles]
            composite = preferred_image(tiles_root / 'maps' / name / zoom, f'{name}_{zoom}')
            if composite is None:
                raise ValueError(f"缺少总图: {name}/{zoom}")
            with Image.open(composite) as image:
                expected = ((max(xs)-min(xs)+1)*TILE_SIZE, (max(ys)-min(ys)+1)*TILE_SIZE)
                if image.size != expected:
                    raise ValueError(f"总图与瓦片坐标边界不一致: {name}/{zoom}")
            maps.append({"map": name, "zoom": zoom, "minX": min(xs), "maxX": max(xs),
                         "minY": min(ys), "maxY": max(ys),
                         "count": (max(xs)-min(xs)+1)*(max(ys)-min(ys)+1)})
            plans.append((maps[-1], composite))
    if not plans:
        raise ValueError("没有可发布的瓦片")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".preset-build-", dir=output.parent) as temp:
        stage = Path(temp).resolve()
        assert stage.is_relative_to(output.parent)
        bundle = stage / "bundle"
        bundle.mkdir()

        files = []
        for entry, source in plans:
            with Image.open(source) as image:
                image.load()
                def encode(position):
                    x, y = position
                    relative = f"{entry['map']}/{entry['zoom']}/{x}_{y}.webp"
                    destination = bundle / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    left, top = (x-entry['minX'])*TILE_SIZE, (y-entry['minY'])*TILE_SIZE
                    with image.crop((left, top, left+TILE_SIZE, top+TILE_SIZE)) as part:
                        save_webp(part, destination)
                    data = destination.read_bytes()
                    return {"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

                positions = ((x, y) for y in range(entry['minY'], entry['maxY']+1)
                             for x in range(entry['minX'], entry['maxX']+1))
                with ThreadPoolExecutor(max_workers=4) as executor:
                    files.extend(executor.map(encode, positions))
        manifest = {"schema_version": 1, "kind": "nav-grid-editor-tile-presets", "tile_size": TILE_SIZE,
                    "encoding": {"format": "webp", "quality": 90, "method": 4},
                    "maps": maps, "files": files}
        (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8", newline="\n")
        os.rename(bundle, output)
    return {"maps": len(maps), "tiles": len(files), "bytes": sum(f["size"] for f in files),
            "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tiles-root", type=Path, default=Path("assets/tiles"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build_presets(args.tiles_root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()

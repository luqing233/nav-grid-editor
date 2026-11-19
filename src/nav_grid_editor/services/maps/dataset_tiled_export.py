"""Build the release bundle from the editor's frozen source files."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .dataset_export import _build as build_metadata, _export_grid_meta, _json, _record, _relative, _write_json
from .links import normalize_link
from .settings import disk_tier
from .localization_native_compaction import _build as compact_native, _tile_pixels
from .prepared_localization import (
    PACKAGE_MARKER,
    PREPARED_DIRNAME,
    build_prepared_localization,
    verify_prepared_localization,
)
from .localization_search_compaction import _build as compact_search, _read_verified
from .localization_tiles import _build as build_tiles, _decode, verify_localization_tiles


def _verify_v3_pixels(before, after):
    old, new = before / "localization", after / "localization"
    manifest = _json(old / "manifest.json")
    catalog = _json(old / manifest["layer_catalog"])
    paths = [(row["image"], False) for row in manifest["base_maps"]]
    paths.extend((f"{manifest['layers_root']}/{row['path']}", True) for row in catalog["layers"])
    tested = set()
    tile_count = search_count = 0
    for path, layer in paths:
        a, b = _json(_relative(old, path)), _json(_relative(new, path))
        if any(a[key] != b[key] for key in a.keys() - {"tiles", "schema_version"}):
            raise ValueError(f"瓦片裁边改变了图像标定或搜索索引: {path}")
        for source, target in zip(a["tiles"], b["tiles"], strict=True):
            if any(source[key] != target[key] for key in ("column", "row", "width", "height")):
                raise ValueError(f"瓦片坐标或完整尺寸改变: {path}")
            tile_count += 1
            identity = (source["path"], target["path"], tuple(target["roi"]), target["storage"], layer)
            if identity in tested:
                continue
            tested.add(identity)
            raw = _read_verified(old, source)
            expected = _tile_pixels(raw, layer)
            restored = np.zeros((target["height"], target["width"], 3), np.uint8)
            l, t, r, bottom = target["roi"]
            if target["storage"] == "full":
                if raw != _read_verified(new, target):
                    raise ValueError(f"原样保留的瓦片字节改变: {path}")
                restored[t:bottom, l:r] = expected[t:bottom, l:r]
            elif target["storage"] == "roi":
                restored[t:bottom, l:r] = _decode(_read_verified(new, target), cv2.IMREAD_COLOR)
            if not np.array_equal(restored, expected):
                raise ValueError(f"精匹配 BGR 还原不一致: {path} {source['column']},{source['row']}")
            if layer:
                original = _decode(raw)
                alpha = original[:, :, 3] if original.shape[2] == 4 else np.full(original.shape[:2], 255, np.uint8)
                restored_alpha = np.zeros_like(alpha)
                if target["storage"] == "full":
                    restored_alpha[:] = alpha  # Payload equality was verified above.
                elif target["storage"] == "roi":
                    restored_alpha[t:bottom, l:r] = 255
                if not np.array_equal(restored_alpha, alpha):
                    raise ValueError(f"楼层 alpha 还原不一致: {path} {source['column']},{source['row']}")
        for entry in (*a["coarse"], a["floor_rank"]):
            if entry is not None:
                if entry["path"] is not None and _read_verified(old, entry) != _read_verified(new, entry):
                    raise ValueError(f"裁边改变了搜索文件: {entry['path']}")
                search_count += 1
    return {"images": len(paths), "tiles_pixel_equal": tile_count, "floor_alpha_equal": True,
            "unique_tile_decodes": len(tested),
            "search_records_byte_equal": search_count}


def verify_tiled_release(root):
    root = Path(root).resolve()
    data_root = root / "navigation"
    if not data_root.is_dir():
        raise ValueError("发布包缺少 navigation 数据根")
    verify_localization_tiles(data_root)
    dataset = _json(data_root / "dataset.json")
    manifest = _json(data_root / "localization/manifest.json")
    if manifest["schema_version"] != 3:
        raise ValueError("发布候选须使用定位瓦片 v3")
    expected = {
        "navigation/dataset.json",
        "navigation/localization/manifest.json",
        "navigation/validation.json",
        "navigation/schemas/dataset.schema.json",
        "README.md",
        ".gitattributes",
    }
    if (data_root / PACKAGE_MARKER).is_file():
        expected.add(f"navigation/{PACKAGE_MARKER}")
    expected.update(record["path"] for record in dataset["navigation_files"])
    expected = {
        item if item.startswith(("navigation/", "README.md", ".gitattributes")) else f"navigation/{item}"
        for item in expected
    }
    expected.update(f"navigation/localization/{record['path']}" for record in manifest["files"])
    prepared = data_root / "localization" / PREPARED_DIRNAME
    if prepared.is_dir():
        verify_prepared_localization(prepared)
        expected.update(
            path.relative_to(root).as_posix()
            for path in prepared.rglob("*")
            if path.is_file()
        )
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_relative_to(root / ".git")
    }
    if expected != actual:
        raise ValueError(f"发布包有缺项或孤立文件: {sorted(expected ^ actual)[:10]}")
    grids = {}
    for record in dataset["navigation_files"]:
        if record["role"] == "grid":
            meta = _export_grid_meta(_relative(data_root, record["path"]))
            grids[record["path"]] = tuple(str(meta.get(key, "")) for key in ("map_name", "tier", "zoom"))
    bound = set()
    for entry in dataset["registrations"]:
        name = entry["grid"]
        if name is not None:
            if name in bound or grids.get(name) != tuple(entry[key] for key in ("map_name", "tier", "zoom")):
                raise ValueError(f"网格注册身份不符: {name}")
            bound.add(name)
    if bound != grids.keys():
        raise ValueError("存在未注册的导航网格")
    link_ids = set()
    for raw in _json(data_root / "links.json")["links"]:
        link = normalize_link(raw)
        if link["id"] in link_ids:
            raise ValueError("发布包存在重复连接 ID")
        link_ids.add(link["id"])
        for side in ("from", "to"):
            end = link[side]
            count = sum(mid == end["map"] and tier == disk_tier(end["tier"])
                        and (not end["zoom"] or zoom == end["zoom"]) for mid, tier, zoom in grids.values())
            if count != 1:
                raise ValueError(f"连接端点缺网格或 zoom 不明确: {link['id']} / {side}")


def build_tiled_export(frozen, inputs, stage, progress):
    catalog = _json(frozen["localization/layers/catalog.json"])
    sources = {Path(row["path"]).with_suffix(".webp").as_posix(): frozen[f"localization/layers/{row['path']}"]
               for row in catalog["layers"]}
    with tempfile.TemporaryDirectory(prefix=".v3-build-", dir=stage) as temporary:
        work = Path(temporary).resolve()
        if work.parent != stage.resolve():
            raise ValueError("瓦片中间目录超出暂存范围")
        metadata, v1, v2 = (work / name for name in ("metadata", "v1", "v2"))
        summary = build_metadata(frozen, metadata, progress, metadata_only=True)
        progress("生成主图瓦片和索引")
        build_tiles(inputs / "presets", metadata, None, v1, False, floor_sources=sources, progress=progress)
        progress("裁切全局搜索索引")
        search_report = compact_search(v1, v2)
        progress("整理精匹配瓦片")
        report = compact_native(v2, stage)
        progress("逐像素核对精匹配瓦片")
        report["verification"] = _verify_v3_pixels(v2, stage)
        progress("生成预编译定位资源")
        report["prepared_localization"] = build_prepared_localization(stage)

    source_records = [_record(inputs, path) for path in sorted(frozen.values())]
    fingerprint = hashlib.sha256(json.dumps(source_records, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    report["source_baseline"] = {"kind": "editor-frozen-files", "sha256": fingerprint, "files": source_records}
    report["search_compaction"] = search_report["summary"]
    report["missing_layers"] = [
        {"id": layer, "catalog_generated_at": catalog.get("generated_at"),
         "reason": "采集目录将该层列为缺少有效配置或 bounds，未提供可标定的楼层图"}
        for layer in sorted(set(catalog.get("missing_logic_ids", [])))
    ]
    _write_json(stage / "validation.json", report)
    (stage / "schemas").mkdir()
    shutil.copyfile(Path(__file__).with_name("dataset.schema.json"), stage / "schemas/dataset.schema.json")
    (stage.parent / ".gitattributes").write_text(
        "*.webp filter=lfs diff=lfs merge=lfs -text\n*.grid.npz filter=lfs diff=lfs merge=lfs -text\n"
        "*.json !filter !diff !merge text eol=lf\n", encoding="utf-8", newline="\n")
    (stage.parent / "README.md").write_text(
        "# nav-grid-data\n\n"
        "`navigation/` 是导航业务的唯一运行根，包含网格、层声明、跨层连接、视觉定位载荷、"
        "预编译竞争资源和根发布标记。请完整下载并解压整个仓库；只复制其中一部分不构成可用数据包。\n\n"
        "```text\n"
        "navigation/\n"
        "├── dataset.json\n"
        "├── *.grid.npz\n"
        "├── levels.json\n"
        "├── links.json\n"
        "├── validation.json\n"
        "├── prepared-package.json\n"
        "├── schemas/\n"
        "└── localization/\n"
        "    ├── manifest.json\n"
        "    ├── maps/\n"
        "    ├── layers/\n"
        "    ├── objects/\n"
        "    └── prepared/\n"
        "```\n\n"
        "`dataset.json`、`localization/manifest.json` 和 "
        "`localization/prepared/manifest.json` 由 `prepared-package.json` 与 `READY` "
        "互相绑定。prepared 直接复用 localization 中的楼层排序图，不复制第二份图片。"
        "运行端只读取这些静态资源，不会在首次定位时编译派生图片。\n\n"
        "完整验证、来源摘要及缺项见 `navigation/validation.json`。"
        "WebP 和 NPZ 使用 Git LFS；发布或更新数据前必须取得实体文件，不能提交 LFS 指针文本。\n",
        encoding="utf-8",
        newline="\n",
    )
    return {**summary, "format": "tiled-v3", "release_id": report["release_id"],
            "tiles": report["verification"]["tiles_pixel_equal"], "source_sha256": fingerprint}

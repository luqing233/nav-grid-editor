"""Build an isolated, color WebP nav-grid-data snapshot; never perform Git writes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np

from .grid import _load_grid_npz, _normalize_floor_regions
from .io import _path_lock, preferred_image, save_webp
from .layers import build_level_index
from .links import normalize_link
from .settings import Image, disk_tier, valid_grid_tier, valid_map_zoom


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _record(root: Path, path: Path) -> dict:
    return {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
            "sha256": _digest(path)}


def _relative(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"非法资源路径: {name!r}")
    path = Path(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError(f"非法资源路径: {name!r}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"资源路径超出目录: {name}")
    return root / path


def _read_image(path: Path, flags: int) -> np.ndarray:
    # imread on Windows does not support every Unicode path on every build.
    image = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), flags)
    if image is None:
        raise ValueError(f"无法解码图片: {path.name}")
    return image


def _color_webp(source: Path, target: Path, *, layer: bool) -> tuple[int, int]:
    """Publish Q90 RGB/RGBA at original dimensions; alpha remains lossless."""
    with Image.open(source) as original:
        transparent = "A" in original.getbands() or "transparency" in original.info
        image = original.convert("RGBA" if transparent else "RGB")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        save_webp(image, target)
        with Image.open(target) as decoded:
            if decoded.format != "WEBP" or decoded.size != image.size:
                raise ValueError(f"彩色图片读回校验失败: {source.name}")
            if transparent and not np.array_equal(np.asarray(image.getchannel("A")),
                                                  np.asarray(decoded.convert("RGBA").getchannel("A"))):
                raise ValueError(f"透明通道读回校验失败: {source.name}")
        return image.size
    finally:
        image.close()


def _calibration(mapping: dict, size: tuple[int, int]) -> None:
    try:
        actual = mapping["image_size"]
        if (actual["width"], actual["height"]) != size:
            raise ValueError("图片尺寸与 image_size 不匹配")
        a = np.asarray(mapping["matrix"], dtype=float)
        b = np.asarray(mapping["inverse_matrix"], dtype=float)
        if a.shape != (2, 3) or b.shape != (2, 3):
            raise ValueError("标定矩阵必须为 2×3")
        a = np.vstack((a, [0, 0, 1]))
        b = np.vstack((b, [0, 0, 1]))
        if not (np.isfinite(a).all() and np.isfinite(b).all()
                and np.allclose(a @ b, np.eye(3), atol=1e-7, rtol=0)
                and np.allclose(b @ a, np.eye(3), atol=1e-7, rtol=0)):
            raise ValueError("标定矩阵非有限或不互逆")
    except (KeyError, TypeError) as exc:
        raise ValueError("标定文件缺少尺寸或仿射矩阵") from exc


def _plan(tiles: Path, grids: Path, *, tiled: bool = False) -> dict[str, Path]:
    catalog = tiles / "layers/catalog.json"
    if not catalog.is_file():
        raise ValueError("缺少分层 catalog.json，请先执行 fetch-layers；不会用内置兜底覆盖完整目录")
    data = _json(catalog)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("layers"), list):
        raise ValueError("分层目录必须为 schema_version=1 的对象")
    missing = data.get("missing_logic_ids", [])
    if not isinstance(missing, list) or not all(isinstance(tier, str) for tier in missing):
        raise ValueError("missing_logic_ids 必须是层 ID 数组")
    plan = {"localization/layers/catalog.json": catalog}
    image_outputs = set()
    for entry in data["layers"]:
        if not isinstance(entry, dict):
            raise ValueError("分层目录条目必须是对象")
        source = _relative(tiles / "layers", entry.get("path"))
        key = "localization/layers/" + entry["path"]
        output_key = str(Path(key).with_suffix(".webp").as_posix())
        if source.suffix not in (".png", ".webp"):
            raise ValueError(f"分层图片必须为 PNG 或 WebP: {key}")
        if key in plan or output_key in image_outputs:
            raise ValueError(f"重复的分层图片路径: {key}")
        image_outputs.add(output_key)
        original = source.with_suffix(".png")
        if tiled and original.is_file():
            source = _relative(tiles / "layers", Path(entry["path"]).with_suffix(".png").as_posix())
        plan[key] = source
    for directory in sorted((tiles / "maps").glob("*/4")):
        mid = directory.parent.name
        mapping = directory / f"{mid}_4_mapping.json"
        if not mapping.is_file():
            continue  # Uncalibrated captures cannot be published as world-space base maps.
        if not valid_map_zoom(mid, "4"):
            raise ValueError(f"非法地图 ID: {mid}")
        if tiled:
            plan[f"localization/maps/{mid}/4/{mapping.name}"] = mapping
            continue
        image = preferred_image(directory, f"{mid}_4")
        if image is None:
            raise ValueError(f"缺少已标定地图图片: {mid}_4.webp / .png")
        for path in (image, mapping):
            plan[f"localization/maps/{mid}/4/{path.name}"] = path
    if not any(key.startswith("localization/maps/") for key in plan):
        raise ValueError("没有已标定的 zoom 4 基础地图")
    if not grids.is_dir():
        raise ValueError("导航网格源目录不存在")
    for path in sorted(grids.glob("*.grid.npz")):
        plan[path.name] = path
    for name in ("links.json", "levels.json"):
        if (grids / name).is_file():
            plan[name] = grids / name
    if tiled:
        preset_root = tiles / "presets"
        manifest = preset_root / "manifest.json"
        plan["presets/manifest.json"] = manifest
        for record in _json(manifest)["files"]:
            relative = record["path"]
            key = "presets/" + relative
            if key in plan or not relative.endswith(".webp"):
                raise ValueError(f"预设路径重复或格式无效: {relative}")
            plan[key] = _relative(preset_root, relative)
    return plan


def _space(entry: dict) -> str:
    return "scene:" + entry["level_id"] if entry["map_category"] == "independent" else entry["map_name"]


def _export_grid_meta(path: Path) -> dict:
    _arr, meta, _warnings = _load_grid_npz(path)
    # General reads may tolerate optional metadata; publishing may not.
    try:
        _normalize_floor_regions(meta.get("floor_regions"))
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    return meta


def _build(frozen: dict[str, Path], out: Path, progress, *, metadata_only: bool = False) -> dict:
    # Reject malformed NPZ metadata before spending time converting large images.
    validated_grids = {key: _export_grid_meta(path) for key, path in frozen.items()
                       if key.endswith(".grid.npz")}
    catalog = _json(frozen["localization/layers/catalog.json"])
    entries = catalog["layers"]
    by_tier = {}
    spaces = {}
    registrations = {}
    base_maps = []
    visual_files = []
    grid_meta = {}
    for entry in entries:
        try:
            mid, tier, level = entry["map_name"], entry["id"], entry["level_id"]
            bounds = entry["bounds"]
            values = [bounds[k] for k in ("west", "east", "south", "north")]
            valid = (valid_map_zoom(mid, "4") and valid_grid_tier(tier) and bool(tier)
                     and disk_tier(tier) == tier and bool(level)
                     and entry["map_category"] in ("layered", "independent")
                     and all(type(x) in (int, float) and np.isfinite(x) for x in values)
                     and values[0] < values[1] and values[2] < values[3])
            if not valid or tier in by_tier:
                raise ValueError("非法或重复的层 ID、分类或 bounds")
            by_tier[tier] = entry
        except (KeyError, TypeError) as exc:
            raise ValueError("分层目录缺少身份、尺寸或 bounds") from exc
        sid = _space(entry)
        spaces[sid] = {"id": sid, "kind": "independent" if entry["map_category"] == "independent" else "world",
                       "map_name": mid, "unit": "game_world_unit"}
        if entry["map_category"] == "independent":
            spaces[sid]["level_id"] = level

    # Preserve declarations without images/grids, including manually declared new regions.
    declared = dict(by_tier)
    if "levels.json" in frozen:
        index = _json(frozen["levels.json"])
        if not isinstance(index, dict) or index.get("schema_version") != 1 or not isinstance(index.get("layers"), list):
            raise ValueError("levels.json 版本或结构非法")
        seen = set()
        for row in index["layers"]:
            if not isinstance(row, dict):
                raise ValueError("层声明必须是对象")
            tier, mid, level = row.get("id"), row.get("map_name"), row.get("level_id")
            if not (isinstance(tier, str) and tier and disk_tier(tier) == tier and valid_grid_tier(tier)
                    and isinstance(mid, str) and valid_map_zoom(mid, "4") and isinstance(level, str) and level):
                raise ValueError("层声明身份非法")
            if tier in seen:
                raise ValueError("重复的层声明")
            seen.add(tier)
            old = declared.get(tier)
            if old and any(row.get(k, old.get(k)) != old.get(k) for k in ("map_name", "level_id", "map_category")):
                raise ValueError(f"levels 与 catalog 身份冲突: {tier}")
            declared[tier] = {**(old or {}), **row}

    image_keys = [name for name in frozen if name.startswith("localization/") and name.endswith((".png", ".webp"))]
    if metadata_only:
        image_keys.extend(name.removesuffix("_mapping.json") + ".webp" for name in frozen
                          if name.startswith("localization/maps/") and name.endswith("_mapping.json"))
    for number, key in enumerate(image_keys, 1):
        progress("校验标定和楼层" if metadata_only else "彩色 WebP Q90", number, len(image_keys))
        layer = key.startswith("localization/layers/")
        output_key = str(Path(key).with_suffix(".webp").as_posix())
        if not metadata_only:
            size = _color_webp(frozen[key], out / output_key, layer=layer)
        elif layer:
            with Image.open(frozen[key]) as image:
                size = image.size
        else:
            mapping_key = str(Path(key).with_suffix("").as_posix()) + "_mapping.json"
            dims = _json(frozen[mapping_key])["image_size"]
            size = (dims["width"], dims["height"])
        if layer:
            entry = next(e for e in entries if "localization/layers/" + e["path"] == key)
            if size != (entry["width"], entry["height"]):
                raise ValueError(f"分层图片尺寸不符: {entry['id']}")
            mid, tier, sid = entry["map_name"], entry["id"], _space(entry)
            entry["path"] = output_key.removeprefix("localization/layers/")
            if "filename" in entry:
                entry["filename"] = Path(output_key).name
            visual = {"kind": "layer", "image": output_key, "catalog_entry": tier}
        else:
            mid, tier = key.split("/")[2], ""
            sid = mid
            mapping_key = str(Path(key).with_suffix("").as_posix()) + "_mapping.json"
            mapping = _json(frozen[mapping_key])
            _calibration(mapping, size)
            if "map_name" in mapping:
                mapping["map_name"] = Path(output_key).name
            _write_json(out / mapping_key, mapping)
            visual_files.append(out / mapping_key)
            spaces[sid] = {"id": sid, "kind": "world", "map_name": mid, "unit": "game_world_unit"}
            visual = {"kind": "base", "image": output_key, "mapping": mapping_key}
            base_maps.append({"map_id": mid, "zoom": "4", "image": output_key.removeprefix("localization/"),
                              "mapping": mapping_key.removeprefix("localization/"), "width": size[0],
                              "height": size[1], "coordinate_space": sid})
        registrations[(sid, mid, tier, "4")] = {"coordinate_space_id": sid, "map_name": mid,
                                               "tier": tier, "zoom": "4", "visual": visual, "grid": None}
        if not metadata_only:
            visual_files.append(out / output_key)

    navigation_files = []
    for key, source in frozen.items():
        if not key.endswith(".grid.npz"):
            continue
        meta = validated_grids[key]
        mid, tier, zoom = str(meta.get("map_name", "")), str(meta.get("tier", "")), str(meta.get("zoom", ""))
        expected = f"{mid}__{tier}_{zoom}.grid.npz" if tier else f"{mid}_{zoom}.grid.npz"
        if (not valid_map_zoom(mid, zoom) or not valid_grid_tier(tier)
                or disk_tier(tier) != tier or key != expected):
            raise ValueError(f"网格文件名与 meta 身份不符: {key}")
        if tier and (tier not in declared or declared[tier]["map_name"] != mid
                     or declared[tier].get("map_category") not in ("layered", "independent")):
            raise ValueError(f"网格层未在目录中声明: {key}")
        sid = _space(declared[tier]) if tier else mid
        if tier and sid not in spaces:
            entry = declared[tier]
            spaces[sid] = {"id": sid, "kind": "independent" if entry["map_category"] == "independent" else "world",
                           "map_name": mid, "unit": "game_world_unit"}
            if entry["map_category"] == "independent":
                spaces[sid]["level_id"] = entry["level_id"]
        spaces.setdefault(sid, {"id": sid, "kind": "world", "map_name": mid, "unit": "game_world_unit"})
        identity = (sid, mid, tier, zoom)
        reg = registrations.setdefault(identity, {"coordinate_space_id": sid, "map_name": mid, "tier": tier,
                                                  "zoom": zoom, "visual": None, "grid": None})
        reg["grid"] = key
        grid_meta[(mid, tier, zoom)] = meta
        shutil.copyfile(source, out / key)
        navigation_files.append({**_record(out, out / key), "role": "grid"})

    if "links.json" in frozen:
        links = _json(frozen["links.json"])
        if links.get("schema_version") not in (1, 2, 3) or not isinstance(links.get("links"), list):
            raise ValueError("links.json 版本或结构非法")
        ids = set()
        for raw in links["links"]:
            link = normalize_link(raw)
            if link["id"] in ids:
                raise ValueError("重复的连接 ID")
            ids.add(link["id"])
            for side in ("from", "to"):
                end = link[side]
                candidates = [k for k in grid_meta if k[0] == end["map"] and k[1] == disk_tier(end["tier"])
                              and (not end["zoom"] or k[2] == end["zoom"])]
                if len(candidates) != 1:
                    raise ValueError(f"连接端点缺网格或 zoom 不明确: {link['id']} / {side}")
        if metadata_only:
            shutil.copyfile(frozen["links.json"], out / "links.json")
        else:
            _write_json(out / "links.json", links)
    else:
        _write_json(out / "links.json", {"schema_version": 3, "links": []})
    # Derive declarations from the full catalog, never from the set of NPZ files.
    normalized_levels = build_level_index(list(declared.values()), catalog.get("generated_at", ""))
    saved_levels = _json(frozen["levels.json"]) if metadata_only and "levels.json" in frozen else None
    if saved_levels is not None and saved_levels == {**normalized_levels, "generated_at": saved_levels.get("generated_at")}:
        shutil.copyfile(frozen["levels.json"], out / "levels.json")
    else:
        _write_json(out / "levels.json", normalized_levels)
    for name, role in (("levels.json", "levels"), ("links.json", "links")):
        navigation_files.append({**_record(out, out / name), "role": role})
    target_catalog = out / "localization/layers/catalog.json"
    target_catalog.parent.mkdir(parents=True, exist_ok=True)
    _write_json(target_catalog, catalog)
    visual_files.append(target_catalog)
    local_root = out / "localization"
    manifest = {
        "schema_version": 1, "kind": "okef-visual-localization-input" if metadata_only else "okef-visual-localization",
        "maps_root": "maps", "layers_root": "layers",
        "layer_catalog": "layers/catalog.json", "base_maps": sorted(base_maps, key=lambda x: x["map_id"]),
        "coordinates": {"axes": ["x", "z"], "height_available": False},
        "coverage": {"layer_count": len(entries), "independent_layer_count": sum(e["map_category"] == "independent" for e in entries),
                     "missing_logic_ids": catalog.get("missing_logic_ids", []),
                     "catalog_generated_at": catalog.get("generated_at")},
        "provenance": {"producer": "nav-grid-editor", "processing": "Color WebP Q90; Pillow quality=90 method=4; original dimensions; lossless alpha; no resampling"},
        "files": sorted((_record(local_root, p) for p in visual_files), key=lambda x: x["path"]),
    }
    if set(by_tier).intersection(manifest["coverage"]["missing_logic_ids"]):
        raise ValueError("目录同时将同一层标为存在和缺失")
    _write_json(local_root / "manifest.json", manifest)
    dataset = {"schema_version": 1, "kind": "okef-nav-dataset", "dataset_id": "endfield-nav",
               "release_id": str(uuid4()), "parent_release_id": None,
               "coordinate_spaces": sorted(spaces.values(), key=lambda x: x["id"]),
               "declarations": "levels.json", "navigation_files": sorted(navigation_files, key=lambda x: x["path"]),
               "localization_manifest": _record(out, local_root / "manifest.json"),
               "registrations": [registrations[k] for k in sorted(registrations)]}
    _write_json(out / "dataset.json", dataset)
    if metadata_only:
        return {"grids": len(grid_meta), "images": len(image_keys), "layers": len(entries)}
    schema = Path(__file__).with_name("dataset.schema.json")
    (out / "schemas").mkdir()
    shutil.copyfile(schema, out / "schemas/dataset.schema.json")
    (out / ".gitattributes").write_text(
        "*.webp filter=lfs diff=lfs merge=lfs -text\n*.png filter=lfs diff=lfs merge=lfs -text\n*.grid.npz filter=lfs diff=lfs merge=lfs -text\n"
        "*.json !filter !diff !merge text eol=lf\n*.md !filter !diff !merge text eol=lf\n", encoding="utf-8")
    (out / "README.md").write_text(
        "# nav-grid-data 导出包\n\n完整磁盘快照，图片为原尺寸彩色 WebP Q90（method=4），保留透明通道，网格保持原字节。\n"
        "dataset.json 是统一发布清单，localization/manifest.json 是定位资源清单。\n"
        "编辑用彩色素材未修改；新增区域中的未知格仍为未知，缺失高度不推断。\n"
        "提交前安装 Git LFS；WebP/PNG/NPZ 使用 LFS，不要通过网页逐文件上传。\n"
        "导出器不执行 commit/push。接入 ok-end-field 仍需更新子模块指针并发布。\n", encoding="utf-8")
    verify_dataset(out)
    return {"grids": len(grid_meta), "images": len(image_keys), "layers": len(entries),
            "files": len(navigation_files) + len(visual_files) + 1, "release_id": dataset["release_id"]}


def verify_dataset(root: Path) -> None:
    """Check file closure and bindings in addition to per-file checksums."""
    data = _json(root / "dataset.json")
    child = _relative(root, data["localization_manifest"]["path"])
    local = _json(child)
    records = [*data["navigation_files"], data["localization_manifest"]]
    records += [{**item, "path": child.parent.relative_to(root).as_posix() + "/" + item["path"]}
                for item in local["files"]]
    managed = set()
    for item in records:
        path = _relative(root, item["path"])
        if item["path"] in managed or path.stat().st_size != item["size"] or _digest(path) != item["sha256"]:
            raise ValueError(f"清单校验失败: {item['path']}")
        if item.get("role") == "grid":
            _export_grid_meta(path)
        managed.add(item["path"])
    keys = set()
    spaces = {space["id"] for space in data["coordinate_spaces"]}
    for reg in data["registrations"]:
        key = tuple(reg[k] for k in ("coordinate_space_id", "map_name", "tier", "zoom"))
        if key in keys or key[0] not in spaces:
            raise ValueError("重复资源身份或未知坐标系")
        keys.add(key)
        if reg["grid"] and reg["grid"] not in managed:
            raise ValueError("网格引用不在快照内")
        visual = reg["visual"]
        if visual:
            for field in (("image", "mapping") if visual["kind"] == "base" else ("image",)):
                if visual[field] not in managed:
                    raise ValueError("定位资源引用不在快照内")


class DatasetExport:
    def __init__(self, data_root: Path, tiles: Path, grids: Path):
        self.data_root, self.tiles, self.grids = Path(data_root).resolve(), Path(tiles).resolve(), Path(grids).resolve()
        self._lock = threading.Lock()
        self._state = {"running": False, "phase": "未导出", "error": None, "output": None}

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "default_parent": str(self.data_root / "exports")}

    def _progress(self, phase: str, done=0, total=0):
        with self._lock:
            self._state.update(phase=phase, done=done, total=total)

    def _target(self, value: str) -> Path:
        if not isinstance(value, str):
            raise ValueError("目标目录必须是字符串")
        if value.strip():
            path = Path(value.strip()).expanduser()
            target = (path if path.is_absolute() else self.data_root / path).resolve()
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = self.data_root / "exports" / f"nav-grid-data-{stamp}-{uuid4().hex[:6]}"
        for source in (self.data_root, self.tiles, self.grids):
            if target == source or source.is_relative_to(target):
                raise ValueError("目标不能是源目录或其父目录")
        for source in (self.tiles, self.grids, self.data_root / "configs", self.data_root / "browser_profile"):
            if target.is_relative_to(source):
                raise ValueError("目标不能位于素材、网格、凭证或登录态源目录内")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise ValueError("请选择新目录或空目录；不会覆盖已有仓库或非空目录")
        return target

    def start(self, target: str = "", *, export_format: str = "legacy") -> dict:
        with self._lock:
            if self._state["running"]:
                return {"ok": False, "error": "已有导出任务正在运行"}
            try:
                if export_format not in ("legacy", "tiled-v3"):
                    raise ValueError("不支持的数据包格式")
                output = self._target(target)
            except (ValueError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            self._state = {"running": True, "phase": "准备导出", "error": None, "output": str(output),
                           "format": export_format}
        threading.Thread(target=self._run, args=(output, export_format), daemon=True).start()
        return {"ok": True, "output": str(output)}

    def _run(self, target: Path, export_format: str = "legacy"):
        stage = None
        lock_path = None
        lock_fd = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            lock_path = target.parent / f".{target.name}.export.lock"
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.export-", dir=target.parent)).resolve()
            inputs = stage / ".inputs"
            inputs.mkdir()
            tiled = export_format == "tiled-v3"
            plan = _plan(self.tiles, self.grids, tiled=True) if tiled else _plan(self.tiles, self.grids)
            frozen, hashes = {}, {}
            for count, (name, source) in enumerate(plan.items(), 1):
                self._progress("冻结源文件", count, len(plan))
                destination = _relative(inputs, name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with _path_lock(source):
                    shutil.copyfile(source, destination)
                frozen[name], hashes[name] = destination, _digest(destination)
            if tiled:
                from .dataset_tiled_export import build_tiled_export, verify_tiled_release

                summary = build_tiled_export(frozen, inputs, stage, self._progress)
            else:
                summary = _build(frozen, stage, self._progress)
            self._progress("校验源文件一致性")
            current = _plan(self.tiles, self.grids, tiled=True) if tiled else _plan(self.tiles, self.grids)
            if current != plan or any(_digest(source) != hashes[name] for name, source in plan.items()):
                raise ValueError("源数据在导出期间发生变化，请保存完成后重新导出")
            if not inputs.is_relative_to(stage):
                raise ValueError("临时输入目录超出暂存目录")
            shutil.rmtree(inputs)
            if tiled:
                verify_tiled_release(stage)
            self._target(str(target))  # Recheck immediately before publication; never delete a nonempty destination.
            if target.exists():
                target.rmdir()
            os.rename(stage, target)
            stage = None
            with self._lock:
                self._state.update(summary=summary, phase="导出完成")
        except Exception as exc:
            with self._lock:
                self._state.update(error=str(exc), phase="导出失败")
        finally:
            try:
                if stage is not None and stage.parent == target.parent.resolve():
                    shutil.rmtree(stage, ignore_errors=True)
                if lock_fd is not None:
                    os.close(lock_fd)
                    lock_path.unlink(missing_ok=True)
            finally:
                with self._lock:
                    self._state["running"] = False

"""Pure three-way merges. The navigation file formats are deliberately unchanged."""
from __future__ import annotations

import copy
import hashlib
import json
import math

import numpy as np

from .settings import disk_tier, DENSE_CELL_LIMIT


MISSING = object()


def same(a, b):
    if a is MISSING or b is MISSING:
        return a is b
    return a == b


class Decisions:
    """Stable, content-bound conflict IDs; unresolved previews retain the target."""
    def __init__(self, choices=None):
        self.choices = choices or {}
        self.conflicts = []

    def conflict(self, key, label, base, current, incoming, **extra):
        values = {"base": base, "current": current, "incoming": incoming}
        encoded = json.dumps([key, values], sort_keys=True, ensure_ascii=False,
                             default=lambda x: {"missing": True})
        identity = hashlib.sha256(encoded.encode()).hexdigest()[:24]
        choice = self.choices.get(identity)
        if choice is not None and choice not in values:
            raise ValueError("无效的冲突选择")
        self.conflicts.append({"id": identity, "label": label, "choice": choice,
                               **{k: None if v is MISSING else v for k, v in values.items()},
                               "absent": [k for k,v in values.items() if v is MISSING],
                               **extra})
        return values[choice or "current"]

    def pick(self, key, label, base, current, incoming, **extra):
        if same(incoming, base) or same(current, incoming):
            return current
        if same(current, base):
            return incoming
        return self.conflict(key, label, base, current, incoming, **extra)


def grid_equal(a, b):
    if a is None or b is None:
        return a is b
    def metadata(meta):
        return {k: (disk_tier(v) if k == "tier" else v)
                for k, v in meta.items() if k != "created"}
    return metadata(a[1]) == metadata(b[1]) and np.array_equal(a[0], b[0])


def merge_grid(base, current, incoming, choices=None, namespace=""):
    """Return (grid or None, conflicts). All coordinates are world-aligned.

    Unknown (0) participates in the merge just like Free and Blocked. File
    deletion is represented by None and never confused with an empty grid.
    """
    decisions = Decisions(choices)
    versions = {"base": base, "current": current, "incoming": incoming}

    def whole(reason):
        summaries = {}
        for name, version in versions.items():
            summaries[name] = None if version is None else {
                "meta": version[1], "shape": list(version[0].shape),
                "sha256": hashlib.sha256(version[0].tobytes()).hexdigest()}
        selected = decisions.conflict(namespace + ":file", reason, **summaries, kind="file")
        name = next(k for k, v in summaries.items() if v == selected)
        return copy.deepcopy(versions[name]), decisions.conflicts

    if grid_equal(incoming, base) or grid_equal(current, incoming):
        return copy.deepcopy(current), []
    if grid_equal(current, base):
        return copy.deepcopy(incoming), []
    if current is None or incoming is None:
        return whole("删除与修改冲突：选择将决定整份网格是否保留")

    present = [v for v in versions.values() if v is not None]
    ref = current[1]
    size = float(ref["cell_size"])
    identity = ("magic", "schema_version", "map_name", "zoom", "cell_size", "axis_convention")
    if any(any(v[1].get(k) != ref.get(k) for k in identity)
           or disk_tier(v[1].get("tier")) != disk_tier(ref.get("tier"))
           or v[1]["origin"][1] != ref["origin"][1]
           or any(not math.isclose((v[1]["origin"][i] - ref["origin"][i]) / size,
                                   round((v[1]["origin"][i] - ref["origin"][i]) / size),
                                   rel_tol=0, abs_tol=1e-7) for i in (0, 2)) for v in present):
        return whole("坐标空间不兼容：不能逐格合并，请明确选择整份网格")

    def bounds(v):
        x = round((v[1]["origin"][0] - ref["origin"][0]) / size)
        z = round((v[1]["origin"][2] - ref["origin"][2]) / size)
        return (x, z, x + v[0].shape[1], z + v[0].shape[0])

    if base is not None:
        bb = bounds(base)
        if any(bounds(v)[0] > bb[0] or bounds(v)[1] > bb[1]
               or bounds(v)[2] < bb[2] or bounds(v)[3] < bb[3] for v in (current, incoming)):
            return whole("网格范围收缩：选择整份网格，避免静默裁掉其他修改")
    boxes = [bounds(v) for v in present]
    x0, z0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, z1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    if (x1 - x0) * (z1 - z0) > DENSE_CELL_LIMIT:
        raise ValueError("合并后的网格范围超过大小上限")
    aligned = []
    for v in versions.values():
        arr = np.zeros((z1-z0, x1-x0), dtype=np.uint8)
        if v is not None:
            x, z, right, bottom = bounds(v)
            arr[z-z0:bottom-z0, x-x0:right-x0] = v[0]
        aligned.append(arr)
    b, m, p = aligned
    result = np.where(m == b, p, m).astype(np.uint8)
    conflict_z, conflict_x = np.where((m != b) & (p != b) & (m != p))
    if len(conflict_x) > 1_000_000:
        raise ValueError("冲突格超过 100 万，请拆分 PR")
    remaining = set(zip(conflict_x.tolist(), conflict_z.tolist()))
    origin = [ref["origin"][0]+x0*size, ref["origin"][1], ref["origin"][2]+z0*size]
    for start in zip(conflict_x.tolist(), conflict_z.tolist()):
        if start not in remaining:
            continue
        remaining.remove(start)
        stack, points = [start], []
        while stack:
            x, z = stack.pop()
            points.append((x, z))
            for neighbor in ((x-1,z),(x+1,z),(x,z-1),(x,z+1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        points.sort(key=lambda pt: (pt[1], pt[0]))
        xs, zs = zip(*points)
        values = {name: [int(arr[z,x]) for x,z in points]
                  for name, arr in zip(versions, aligned)}
        choice_values = decisions.conflict(
            namespace + ":cells:" + json.dumps([origin, size, points]),
            f"{len(points)} 个冲突格", **values, kind="cells", count=len(points),
            bounds={"x0":origin[0]+min(xs)*size,"x1":origin[0]+(max(xs)+1)*size,
                    "z0":origin[2]+min(zs)*size,"z1":origin[2]+(max(zs)+1)*size},
            cells=[list(pt) for pt in points], origin=[origin[0],origin[2]], cellSize=size)
        result[list(zs), list(xs)] = choice_values
    meta = {}
    bm, mm, pm = (v[1] if v is not None else {} for v in versions.values())
    for key in sorted(bm.keys() | mm.keys() | pm.keys()):
        if key in ("origin", "created"):
            continue
        if key == "tier":
            meta[key] = disk_tier(ref.get(key))
            continue
        value = decisions.pick(namespace + ":meta:" + key, "元数据：" + key,
                               bm.get(key, MISSING), mm.get(key, MISSING), pm.get(key, MISSING),
                               kind="metadata")
        if value is not MISSING:
            meta[key] = copy.deepcopy(value)
    meta["origin"] = origin
    if "created" in mm:
        meta["created"] = mm["created"]
    return (result, meta), decisions.conflicts


def merge_records(base, current, incoming, key, choices=None, namespace=""):
    """Merge links/layer declarations by stable IDs, preserving every other field."""
    d = Decisions(choices)
    if base is None and current is not None and incoming is not None:
        base = {key: []}
    if base is None or current is None or incoming is None:
        value = d.pick(namespace, "文件新增 / 删除冲突", base, current, incoming, kind="json")
        return copy.deepcopy(value), d.conflicts
    def index(doc):
        result = {}
        for record in doc[key]:
            identity = record.get("id")
            if not isinstance(identity, str) or not identity or identity in result:
                raise ValueError(f"{namespace} 的 {key} 需要唯一非空 ID")
            result[identity] = record
        return result
    indexes = [index(doc) for doc in (base, current, incoming)]
    merged = {}
    for field in sorted(base.keys() | current.keys() | incoming.keys()):
        if field == key:
            continue
        if field == "generated_at":
            value = current.get(field, incoming.get(field, MISSING))
        else:
            value = d.pick(namespace + ":" + field, "字段：" + field,
                           *(doc.get(field, MISSING) for doc in (base, current, incoming)), kind="json")
        if value is not MISSING:
            merged[field] = copy.deepcopy(value)
    records = []
    for identity in sorted(set().union(*(i.keys() for i in indexes))):
        value = d.pick(namespace + ":" + identity, f"{key} · {identity}",
                       *(i.get(identity, MISSING) for i in indexes), kind="json")
        if value is not MISSING:
            records.append(copy.deepcopy(value))
    merged[key] = records
    return merged, d.conflicts


def merge_dataset(base, current, incoming, choices=None, namespace="dataset.json"):
    """Merge declarations; navigation hashes are derived from the final files."""
    if current is None or incoming is None:
        d = Decisions(choices)
        value = d.pick(namespace, "发布清单删除冲突", base, current, incoming, kind="json")
        return copy.deepcopy(value), d.conflicts
    docs = [copy.deepcopy(v or {}) for v in (base,current,incoming)]
    groups = {}
    conflicts = []
    for field in ("navigation_files", "coordinate_spaces", "registrations"):
        wrappers = []
        for doc in docs:
            records=[]
            for record in doc.pop(field, []):
                if field == "navigation_files":
                    identity=record["path"]
                    record.pop("size",None);record.pop("sha256",None)
                elif field == "registrations":
                    identity=json.dumps([record.get(k) for k in ("coordinate_space_id","map_name","tier","zoom")])
                else:
                    identity=record["id"]
                # Wrap, rather than inject an ID into the original record.
                records.append({"id":identity,"value":record})
            wrappers.append({"records":records})
        result, items=merge_records(*wrappers,"records",choices,namespace+":"+field)
        groups[field]=[r["value"] for r in result["records"]]
        conflicts.extend(items)
    d=Decisions(choices)
    result={}
    for key in sorted(set().union(*(doc.keys() for doc in docs))):
        if key in ("release_id","parent_release_id"):
            value=docs[1].get(key,docs[2].get(key,MISSING))
        else:
            value=d.pick(namespace+":"+key,"发布清单："+key,
                         *(doc.get(key,MISSING) for doc in docs),kind="json")
        if value is not MISSING:
            result[key]=value
    return {**result,**groups},conflicts+d.conflicts

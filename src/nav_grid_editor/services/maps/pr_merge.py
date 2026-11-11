"""Isolated PR merge sessions. Never check out, commit, or overwrite working data."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import numpy as np

from .grid import _load_grid_npz, _normalize_floor_regions
from .grid_merge import Decisions, merge_grid, merge_records, merge_dataset
from .pr_review import MAX_CELLS, compare_grid, parse_pr, sparse
from .settings import disk_tier


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def safe_path(path):
    parts = PurePosixPath(path).parts
    if (not parts or "\\" in path or ":" in path or path.startswith("/")
            or any(p.lower() in ("..", ".git") or p.endswith((".", " ")) for p in parts)
            or len(parts) > 8):
        raise ValueError("合并文件路径不合法")
    return path


class PRMergeSession:
    def __init__(self, review, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 10 or not all(isinstance(v, str) for v in values):
            raise ValueError("请选择 1–10 个 PR，按依赖顺序排列")
        self.review = review
        self.repository = review._repository_name()
        numbers = [parse_pr(v, self.repository) for v in values]
        if len(numbers) != len(set(numbers)):
            raise ValueError("合并队列不能包含重复 PR")
        self.token = uuid.uuid4().hex
        self.directory = review.cache.parent / "pr-merge" / self.token
        self.directory.mkdir(parents=True, exist_ok=True)
        self.prs, self.trees, self.blobs = [], {}, {}
        self.choices, self.result, self.response = {}, {}, None
        self.publication = None
        self.bytes_read = 0
        parents = []
        for number in numbers:
            pr = review._github(self.repository, number)
            target, head = pr["base"]["sha"], pr["head"]["sha"]
            if pr["state"] != "open":
                raise ValueError(f"PR #{number} 已关闭，不能加入合并队列")
            if not all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (target, head)):
                raise ValueError("GitHub 提交标识非法")
            if self.prs and (target != self.target or pr["base"]["ref"] != self.branch):
                raise ValueError("队列必须指向同一目标分支和版本，请重新预览")
            self.target, self.branch = target, pr["base"]["ref"]
            objects = review._git("cat-file", "--batch-check", data=f"{target}\n{head}\n".encode()).decode()
            if " missing" in objects:
                review._git("fetch", "--no-tags", "origin", f"pull/{number}/head", target)
            # Git treats the remaining heads as a hypothetical merge. This also
            # handles PRs stacked on an earlier PR in the selected queue.
            bases = review._git("merge-base", "--all", head, target, *parents).decode().split()
            if len(bases) != 1:
                raise ValueError("存在多个共同祖先，请先整理分支后再合并")
            base = bases[0]
            paths = [safe_path(p) for p in review._git(
                "diff", "--name-only", "--no-renames", "-z", base, head).decode().split("\0") if p]
            if len(paths) > 100:
                raise ValueError("单个 PR 超过 100 份文件，请拆分")
            if any(p.endswith(".grid.npz") and "/" in p for p in paths):
                raise ValueError("网格必须位于数据仓库根目录")
            self.prs.append(dict(number=number, title=pr["title"], head=head, base=base, paths=paths))
            parents.append(head)
        if len({p for pr in self.prs for p in pr["paths"] if p.endswith(".grid.npz")}) > 20:
            raise ValueError("一次合并最多处理 20 份变化网格")
        self.recompute()

    def tree(self, rev):
        if rev not in self.trees:
            listing = self.review._git("ls-tree", "-r", "-z", rev).decode().split("\0")
            self.trees[rev] = {}
            for item in listing:
                if item:
                    mode, kind, rest = item.split(" ", 2)
                    oid, path = rest.split("\t", 1)
                    self.trees[rev][path] = (mode, kind, oid)
        return self.trees[rev]

    def read(self, rev, path):
        safe_path(path)
        entry = self.tree(rev).get(path)
        if entry is None:
            return None
        if entry[0] not in ("100644", "100755") or entry[1] != "blob":
            raise ValueError(f"不支持符号链接或子模块：{path}")
        key = (rev, path)
        if key not in self.blobs:
            file = self.review._read(rev, path, self.directory / "sources" / rev)
            content = file.read_bytes()
            self.bytes_read += len(content)
            if self.bytes_read > 256 * 1024 * 1024:
                raise ValueError("合并快照合计超过 256 MiB")
            self.blobs[key] = content
        return self.blobs[key]

    def grid(self, content):
        if content is None:
            return None
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > 256 * 1024 * 1024:
                raise ValueError("网格解包尺寸过大")
        # The strict loader accepts file-like objects through numpy.load.
        arr, meta, _ = _load_grid_npz(io.BytesIO(content))
        _normalize_floor_regions(meta.get("floor_regions"))
        if np.count_nonzero(arr) > MAX_CELLS:
            raise ValueError("单份网格超过 100 万已涂格")
        return arr, meta

    def pack_grid(self, grid):
        if grid is None:
            return None
        arr, meta = grid
        meta = copy.deepcopy(meta)
        if "tier" in meta:
            meta["tier"] = disk_tier(meta["tier"])
        _normalize_floor_regions(meta.get("floor_regions"))
        out = io.BytesIO()
        np.savez_compressed(out, cells=arr, meta=np.array(json.dumps(meta, ensure_ascii=False, allow_nan=False)))
        content = out.getvalue()
        self.grid(content)
        return content

    def recompute(self):
        self.stages = []
        result, conflicts, files, completed, all_files = {}, [], [], [], {}
        total = 0
        for stage, pr in enumerate(self.prs):
            stage_conflicts = []
            files = []
            for path in pr["paths"]:
                base = self.read(pr["base"], path)
                current = result[path] if path in result else self.read(self.target, path)
                incoming = self.read(pr["head"], path)
                namespace = f"{stage}:{path}"
                if path.endswith(".grid.npz"):
                    b, m, p = (self.grid(v) for v in (base, current, incoming))
                    merged, items = merge_grid(b, m, p, self.choices, namespace)
                    content = self.pack_grid(merged)
                    comparison = compare_grid(m, merged) if m or merged else compare_grid(b, p)
                    comparison.update(path=path, pr=pr["number"], ancestor=sparse(*b) if b else None,
                                      current=sparse(*m) if m else None, incoming=sparse(*p) if p else None,
                                      result=sparse(*merged) if merged else None)
                    for item in items:
                        if "bounds" in item:
                            comparison["regions"].append({"count": item.get("count", 0), "bounds": item["bounds"], "conflict_id": item["id"]})
                    files.append(comparison)
                    all_files[path] = comparison
                    total += sum(int(np.count_nonzero(v[0])) for v in (b,m,p,merged) if v is not None)
                    if total > 4 * MAX_CELLS:
                        raise ValueError("合并预览超过 400 万已涂格，请拆分队列")
                elif path == "dataset.json":
                    docs = [json.loads(v) if v is not None else None for v in (base,current,incoming)]
                    merged, items = merge_dataset(*docs,self.choices,namespace)
                    content = json_bytes(merged) if merged is not None else None
                elif path in ("links.json", "levels.json"):
                    docs = [json.loads(v) if v is not None else None for v in (base,current,incoming)]
                    merged, items = merge_records(*docs, "links" if path == "links.json" else "layers", self.choices, namespace)
                    content = json_bytes(merged) if merged is not None else None
                else:
                    d = Decisions(self.choices)
                    digest = lambda v: hashlib.sha256(v).hexdigest() if v is not None else None
                    values = [digest(v) for v in (base,current,incoming)]
                    choice = d.pick(namespace, "整份文件：" + path, *values, kind="file")
                    content = (base,current,incoming)[values.index(choice)]
                    items = d.conflicts
                for item in items:
                    item.update(path=path, pr=pr["number"])
                stage_conflicts.extend(items)
                result[path] = content
            conflicts.extend(stage_conflicts)
            if len(conflicts) > 10000:
                raise ValueError("冲突区域超过 10000 个，请拆分 PR")
            if any(not c["choice"] for c in stage_conflicts):
                break
            completed.append(pr["number"])
            self.stages.append(dict(result))
        self.result = result
        # Preserve each file's actual merge ancestor/PR, including earlier stages.
        seen = {f["path"] for f in files}
        files.extend(f for path,f in all_files.items() if path not in seen)
        if len(completed)==len(self.prs):
            # Once every stage is resolved, review the WHOLE integration against
            # the real target branch, not just the last PR's incremental delta.
            for f in files:
                before=self.grid(self.read(self.target,f["path"]))
                after=self.grid(result[f["path"]])
                if before or after:
                    comparison=compare_grid(before,after)
                    f.update(comparison,current=comparison["before"],result=comparison["after"])
                f["contributors"]=[pr["number"] for pr in self.prs if f["path"] in pr["paths"]]
        declarations = result["levels.json"] if "levels.json" in result else self.read(self.target, "levels.json")
        layers = json.loads(declarations).get("layers", []) if declarations else []
        for f in files:
            # The current/result snapshots already carry these two arrays; avoid
            # sending and materializing duplicate million-cell documents.
            f.pop("before", None)
            f.pop("after", None)
            declaration = next((l for l in layers if l.get("id") == f["tier"] and l.get("map_name") == f["map"]), {})
            f["map_category"] = declaration.get("map_category", "layered")
        unresolved = sum(not c["choice"] for c in conflicts)
        self.response = dict(ok=True, token=self.token, repository=self.repository, target=self.target,
                             branch=self.branch, prs=self.prs, completed=completed,
                             active_pr=self.prs[min(len(completed),len(self.prs)-1)]["number"],
                             conflicts=conflicts, unresolved=unresolved, files=files,
                             ready=not unresolved and len(completed)==len(self.prs),
                             changed_paths=sorted(result), checks=[], json_changes={})
        return copy.deepcopy(self.response)

    def resolve(self, choices):
        if self.publication is not None:
            raise ValueError("这份结果已进入发布流程；修改选择请重新预览合并")
        if not isinstance(choices, dict) or not choices:
            raise ValueError("请提供冲突选择")
        known = {c["id"] for c in self.response["conflicts"]}
        if any(k not in known or v not in ("base", "current", "incoming") for k,v in choices.items()):
            raise ValueError("冲突已过期或选择无效，请重新预览")
        previous = dict(self.choices)
        self.choices.update(choices)
        try:
            return self.recompute()
        except Exception:
            self.choices = previous
            self.recompute()
            raise

    def check_fresh(self):
        if self.review._repository_name() != self.repository:
            raise ValueError("数据仓库已切换，请重新预览")
        for pr in self.prs:
            latest = self.review._github(self.repository, pr["number"])
            if (latest["state"] != "open" or latest["head"]["sha"] != pr["head"]
                    or latest["base"]["sha"] != self.target or latest["base"]["ref"] != self.branch):
                raise ValueError("PR 或目标分支已更新，旧合并结果已过期，请重新预览")

    def finalize_manifest(self, changes):
        result = dict(changes)
        manifest = result["dataset.json"] if "dataset.json" in result else self.read(self.target, "dataset.json")
        if manifest:
            doc = json.loads(manifest)
            records = {item["path"]: item for item in doc["navigation_files"]}
            for path, content in result.items():
                if path.endswith(".grid.npz") or path in ("links.json", "levels.json"):
                    if content is None:
                        records.pop(path, None)
                    else:
                        records[path] = {**records.get(path, {}), "path": path,
                                         "role": "grid" if path.endswith(".grid.npz") else ("links" if path == "links.json" else "levels"),
                                         "size":len(content), "sha256":hashlib.sha256(content).hexdigest()}
            for path, record in records.items():
                content = result[path] if path in result else self.read(self.target,path)
                if content is None:
                    raise ValueError("发布清单引用了已删除文件：" + path)
                record.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
            doc["navigation_files"] = sorted(records.values(), key=lambda r:r["path"])
            deleted = {p for p,v in result.items() if v is None}
            for registration in doc.get("registrations", []):
                if registration.get("grid") in deleted:
                    registration["grid"] = None
            # Adding a new grid without a coordinate-space binding requires a
            # complete dataset export, not an invented registration.
            registered = {r.get("grid") for r in doc.get("registrations", [])}
            if any(p.endswith(".grid.npz") and p not in registered for p in records):
                raise ValueError("新网格缺少 dataset.json 坐标绑定，请先补全发布清单")
            target_manifest = self.read(self.target,"dataset.json")
            if "release_id" in doc:
                doc["release_id"] = str(uuid.uuid4())
            if "parent_release_id" in doc:
                doc["parent_release_id"] = json.loads(target_manifest).get("release_id") if target_manifest else None
            result["dataset.json"] = json_bytes(doc)
        return result

    def export(self):
        if not self.response["ready"]:
            raise ValueError("仍有未解决冲突，不能导出")
        self.check_fresh()
        output = self.directory / ("export-" + uuid.uuid4().hex)
        output.mkdir()
        files_root = output / "files"
        files_root.mkdir()
        for path, content in self.result.items():
            if content is not None:
                destination = files_root / safe_path(path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        # Validate endpoints with the same implementation used by the editor.
        # Only referenced navigation grids are materialized; no local working data.
        links = self.result["links.json"] if "links.json" in self.result else self.read(self.target, "links.json")
        if links:
            (files_root / "links.json").write_bytes(links)
            from .service import MapService
            from .links import normalize_link
            for link in json.loads(links)["links"]:
                normalize_link(link, link.get("id", ""))
            svc = MapService(data_root=output, grid2d_dir=files_root)
            for path in self.tree(self.target):
                if "/" not in path and path.endswith(".grid.npz") and path not in self.result:
                    # Endpoint lookup needs file names to disambiguate legacy zooms.
                    (files_root / safe_path(path)).write_bytes(self.read(self.target,path))
            invalid = [l for l in svc.get_grid_links()["links"] if not l["checks"]["valid"]]
            if invalid:
                raise ValueError("合并后连接端点无效：" + "；".join(f"{l['id']} {l['checks']['issue']}" for l in invalid[:8]))
        self.result = self.finalize_manifest(self.result)
        if self.result.get("dataset.json") is not None:
            (files_root / "dataset.json").write_bytes(self.result["dataset.json"])
        plan = dict(repository=self.repository, target=self.target, branch=self.branch,
                    prs=self.prs, resolutions=self.choices,
                    deleted=[p for p,v in self.result.items() if v is None],
                    files=[dict(path=p, size=len(v), sha256=hashlib.sha256(v).hexdigest())
                           for p,v in self.result.items() if v is not None])
        archive = output / "grid-merge.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for item in plan["files"]:
                z.write(files_root/item["path"], "files/"+item["path"])
            z.writestr("merge-plan.json", json_bytes(plan))
            z.writestr("README.txt", "网格三方合并结果；不是完整数据仓库。\n"
                       "请在 merge-plan.json 指定的目标提交上建立独立集成分支。\n"
                       "按 prs 顺序保留原 PR head 的 merge 祖先关系；files/ 为最终合并文件，deleted 为删除列表。\n"
                       "入库前重新确认目标分支与所有 PR head 未变化，再运行数据检查。\n"
                       "本工具没有修改工作区、提交、推送或合并 GitHub PR。\n")
        return archive

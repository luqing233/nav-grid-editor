"""Read-only navigation PR snapshots. Never check out a PR or write navigation data."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import numpy as np

from .grid import _load_grid_npz, _normalize_floor_regions
from .settings import disk_tier

MAX_BYTES = 64 * 1024 * 1024
MAX_CELLS = 1_000_000


def parse_pr(value: str, repository: str) -> int:
    value = str(value).strip()
    match = re.fullmatch(r"#?([1-9]\d*)", value)
    if not match:
        match = re.fullmatch(r"https://github\.com/" + re.escape(repository) + r"/pull/([1-9]\d*)(?:/(?:files|commits|checks))?/?(?:[?#].*)?", value)
    if not match:
        raise ValueError(f"请输入 {repository} 的 PR 编号或完整链接")
    return int(match[1])


def sparse(arr, meta):
    if arr is None:
        return None
    free_z, free_x = np.where(arr == 1)
    block_z, block_x = np.where(arr == 2)
    return {"origin": [meta["origin"][0], meta["origin"][2]], "shape": list(arr.shape),
            "cellSize": meta["cell_size"], "sourceTier": disk_tier(meta.get("tier", "")),
            "cells": np.column_stack((free_x, free_z)).tolist(),
            "blocked": np.column_stack((block_x, block_z)).tolist()}


def grid_bounds(arr, meta):
    return {"x0": meta["origin"][0], "z0": meta["origin"][2],
            "x1": meta["origin"][0] + arr.shape[1] * meta["cell_size"],
            "z1": meta["origin"][2] + arr.shape[0] * meta["cell_size"]}


def change_regions(changes, origin, size):
    """Group spatially adjacent changes, regardless of cell state (not walkability)."""
    remaining = {(cell[0], cell[1]) for cell in changes}
    regions = []
    for cell in changes:
        start = (cell[0], cell[1])
        if start not in remaining:
            continue
        remaining.remove(start)
        stack, points = [start], []
        while stack:
            x, z = stack.pop()
            points.append((x, z))
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                neighbor = (x + dx, z + dz)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
        xs, zs = zip(*points)
        regions.append({"count": len(points), "bounds": {
            "x0": origin[0] + min(xs) * size, "x1": origin[0] + (max(xs) + 1) * size,
            "z0": origin[1] + min(zs) * size, "z1": origin[1] + (max(zs) + 1) * size}})
    regions.sort(key=lambda r: (-r["count"], r["bounds"]["z0"], r["bounds"]["x0"]))
    return regions


def compare_grid(before, after):
    a, am = before if before else (None, {})
    b, bm = after if after else (None, {})
    reference = bm or am
    result = {"map": reference["map_name"], "zoom": str(reference["zoom"]),
              "tier": disk_tier(reference.get("tier", "")),
              "before": sparse(a, am), "after": sparse(b, bm), "changes": [], "regions": [],
              "metadata_changes": sorted(k for k in am.keys() | bm.keys() if am.get(k) != bm.get(k)),
              "floor_regions_changed": am.get("floor_regions", []) != bm.get("floor_regions", []),
              "comparable": True, "transitions": {}}
    size = reference["cell_size"]
    origin = [min(m["origin"][i] for m in (am, bm) if m) for i in (0, 2)]
    result.update(origin=origin, cellSize=size)
    if a is not None and b is not None and a.shape != b.shape:
        result["metadata_changes"].append("shape")
    result["metadata_before"] = {key: ((list(a.shape) if a is not None else None) if key == "shape" else am.get(key)) for key in result["metadata_changes"]}
    result["metadata_after"] = {key: ((list(b.shape) if b is not None else None) if key == "shape" else bm.get(key)) for key in result["metadata_changes"]}
    if a is not None and b is not None:
        identity = ("map_name", "zoom", "tier", "cell_size", "axis_convention")
        aligned = all(math.isclose((bm["origin"][i] - am["origin"][i]) / size,
                                  round((bm["origin"][i] - am["origin"][i]) / size), rel_tol=0, abs_tol=1e-7)
                      for i in (0, 2))
        if any(am.get(k) != bm.get(k) for k in identity) or not aligned:
            result["comparable"] = False
            result["notice"] = "坐标空间、格子尺寸或原点对齐方式发生变化，请切换修改前/后核对；未生成逐格差异"
    if result["comparable"]:
        states = {}
        for side, arr, meta in ((0, a, am), (1, b, bm)):
            if arr is None:
                continue
            dz = round((meta["origin"][2] - origin[1]) / size)
            dx = round((meta["origin"][0] - origin[0]) / size)
            zs, xs = np.nonzero(arr)
            for x, z in zip(xs.tolist(), zs.tolist()):
                states.setdefault((x + dx, z + dz), [0, 0])[side] = int(arr[z, x])
        for (x, z), (old, new) in sorted(states.items(), key=lambda item: (item[0][1], item[0][0])):
            if old == new:
                continue
            result["changes"].append([x, z, old, new])
            transition = f"{old}>{new}"
            result["transitions"][transition] = result["transitions"].get(transition, 0) + 1
        result["regions"] = change_regions(result["changes"], origin, size)
    if not result["regions"]:
        result["regions"] = [{"count": 0, "metadata": True, "bounds": grid_bounds(b, bm) if b is not None else grid_bounds(a, am)}]
    return result


def object_changes(before, after, key):
    def index(data):
        return {str(item.get("id", i)): item for i, item in enumerate(data.get(key, []))}
    a, b = index(before), index(after)
    return {"added": sorted(b.keys() - a.keys()), "removed": sorted(a.keys() - b.keys()),
            "modified": sorted(k for k in a.keys() & b.keys() if a[k] != b[k])}


class DataPRReview:
    def __init__(self, repository: Path, cache: Path):
        self.repository, self.cache = Path(repository), Path(cache)
        self._lock = threading.Lock()
        self._results = {}
        self._merges = {}
        self._publications = {}

    def merge_prepare(self, values):
        from .pr_merge import PRMergeSession
        with self._lock:
            session = PRMergeSession(self, values)
            if len(self._merges) >= 2:
                self._merges.pop(next(iter(self._merges)))
            self._merges[session.token] = session
            return copy.deepcopy(session.response)

    def merge_action(self, token, action, choices=None):
        with self._lock:
            session = self._merges.get(token)
            if action in ("publish", "publication_status", "merge_to_base"):
                from .pr_publish import IntegrationPublisher
                if not isinstance(token,str) or not re.fullmatch(r"[0-9a-f]{32}",token):
                    raise ValueError("合并会话标识非法")
                record=self.cache.parent/"pr-merge"/token/"publication.json"
                if record.is_file():
                    identity=json.loads(record.read_text(encoding="utf-8"))["id"]
                    publisher=self._publications.get(identity)
                    if publisher is None:
                        publisher=IntegrationPublisher(self,session,identity=identity)
                elif session is not None and action=="publish":
                    if not session.response["ready"]:
                        raise ValueError("仍有未解决冲突，不能发布")
                    candidate=IntegrationPublisher(self,session)
                    publisher=self._publications.get(candidate.state["id"],candidate)
                    record.write_text(json.dumps({"id":publisher.state["id"]}),encoding="utf-8")
                else:
                    raise ValueError("未找到发布记录，请重新预览")
                self._publications[publisher.state["id"]]=publisher
                if session is not None:
                    session.publication=publisher
                    if publisher.session is None:publisher.session=session
                if action=="merge_to_base":return publisher.merge_to_base()
                return publisher.start() if action=="publish" else publisher.status()
            if session is None:
                raise ValueError("合并会话已过期，请重新预览")
            if action == "resolve":
                return session.resolve(choices)
            if action == "export":
                if session.publication is not None and session.publication.running:
                    raise ValueError("正在发布，请等待完成后再导出")
                return session.export()
            raise ValueError("未知合并操作")

    def _git(self, *args, data=None):
        try:
            process = subprocess.run(["git", "-C", str(self.repository), *getattr(self, "git_options", []), *args], input=data,
                                     env=self.command_environment() if hasattr(self, "command_environment") else None,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            raise ValueError("未找到 Git；PR 审阅需要 Git 和 Git LFS") from None
        if process.returncode:
            detail=process.stderr.decode("utf-8", "replace")[-1000:]
            if hasattr(self,"redact"):detail=self.redact(detail)
            raise ValueError("Git 读取 PR 失败：" + detail)
        return process.stdout

    def _github(self, repo, number):
        return self._github_request(f"repos/{repo}/pulls/{number}")

    def _github_request(self, path):
        # Reuse the user's existing gh login without copying credentials into the app.
        gh = shutil.which("gh")
        if gh:
            try:
                response = subprocess.run([gh, "api", path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if response.returncode == 0:
                    return json.loads(response.stdout)
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                pass
        request = urllib.request.Request(f"https://api.github.com/{path}",
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": "nav-grid-editor"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (403, 429) and error.headers.get("x-ratelimit-remaining") == "0":
                raise ValueError("GitHub 匿名 API 已限流；请先用 GitHub CLI 执行 gh auth login，再重新加载") from None
            raise ValueError(f"GitHub 返回 {error.code}：请检查 PR 是否公开可访问或稍后重试") from None

    def _repository_name(self):
        remote = self._git("remote", "get-url", "origin").decode().strip()
        match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
        if not match:
            raise ValueError("网格目录的 origin 必须指向 GitHub 数据仓库")
        return match[1]

    def list_open(self):
        repository = self._repository_name()
        pulls = {}
        page = 1
        while True:
            # Fetch every page, including drafts. Creation order stays stable when PRs are updated.
            batch = self._github_request(
                f"repos/{repository}/pulls?state=open&sort=created&direction=asc&per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("GitHub 返回了无效的 PR 列表")
            for pr in batch:
                if pr.get("state") != "open":
                    continue
                number = pr["number"]
                pulls[number] = {"number": number, "title": pr["title"],
                                 "draft": bool(pr.get("draft")), "url": pr["html_url"]}
            if len(batch) < 100:
                break
            page += 1
        return {"ok": True, "repository": repository,
                "pulls": sorted(pulls.values(), key=lambda pr: pr["number"], reverse=True)}

    def _read(self, revision, path, directory):
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) > 8:
            raise ValueError("PR 文件路径不合法")
        spec = revision + ":" + path
        length = int(self._git("cat-file", "-s", spec))
        if length > MAX_BYTES:
            raise ValueError(f"{path} 超过 64 MiB 读取上限")
        data = self._git("show", spec)
        if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
            pointer = data.decode("ascii")
            oid = re.search(r"^oid sha256:([0-9a-f]{64})$", pointer, re.M)
            size = re.search(r"^size (\d+)$", pointer, re.M)
            if not oid or not size or int(size[1]) > MAX_BYTES:
                raise ValueError(f"{path} 的 LFS 指针非法或文件过大")
            data = self._git("lfs", "smudge", data=data)
            if len(data) != int(size[1]) or hashlib.sha256(data).hexdigest() != oid[1]:
                raise ValueError(f"{path} 的 LFS 实体缺失或校验失败，请安装 Git LFS 后重试")
        destination = directory.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    def load(self, value):
        with self._lock:
            repository = self._repository_name()
            number = parse_pr(value, repository)
            pr = self._github(repository, number)
            base_tip, head = pr["base"]["sha"], pr["head"]["sha"]
            if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base_tip, head)):
                raise ValueError("GitHub 提交标识非法")
            objects = self._git("cat-file", "--batch-check", data=f"{base_tip}\n{head}\n".encode()).decode()
            if " missing" in objects:
                self._git("fetch", "--no-tags", "origin", f"pull/{number}/head", base_tip)
            base = self._git("merge-base", base_tip, head).decode().strip()
            identity = (repository, number, base, head)
            if identity not in self._results:
                if len(self._results) >= 2:
                    self._results.clear()
                self._results[identity] = self._build(repository, number, base, head)
            result = copy.deepcopy(self._results[identity])
            result.update(title=pr["title"], url=pr["html_url"], state=pr["state"], base_tip=base_tip)
            return {"ok": True, **result}

    def _build(self, repository, number, base, head):
        directory = self.cache / hashlib.sha256((repository + base + head).encode()).hexdigest()[:24]
        trees = {rev: set(self._git("ls-tree", "-r", "--name-only", "-z", rev).decode().split("\0")) for rev in (base, head)}
        paths = [p for p in self._git("diff", "--name-only", "--no-renames", "-z", base, head).decode().split("\0") if p]
        grids = [p for p in paths if "/" not in p and p.endswith(".grid.npz")]
        if len(grids) > 20:
            raise ValueError("本版最多同时审阅 20 份变化网格，请拆分 PR")
        checks, files, total_cells = [], [], 0

        def read_json(revision, path):
            if path not in trees[revision]:
                return {}
            return json.loads(self._read(revision, path, directory / revision).read_text(encoding="utf-8"))

        manifests = {rev: read_json(rev, "dataset.json") for rev in (base, head)}
        declarations = read_json(head, "levels.json") or read_json(base, "levels.json")
        for path in grids:
            versions = []
            for rev in (base, head):
                if path not in trees[rev]:
                    versions.append(None)
                    continue
                target = self._read(rev, path, directory / rev)
                with zipfile.ZipFile(target) as archive:
                    if sum(item.file_size for item in archive.infolist()) > 256 * 1024 * 1024:
                        raise ValueError(f"{path} 解包尺寸过大")
                arr, meta, warnings = _load_grid_npz(target)
                checks.append({"status": "pass", "label": path + (" · 修改前格式" if rev == base else " · 修改后格式"),
                               "detail": "NPZ 格式、坐标参数和格子值域有效"})
                try:
                    _normalize_floor_regions(meta.get("floor_regions"))
                except ValueError as error:
                    checks.append({"status": "error", "label": path + " · 高度区间", "detail": str(error)})
                count = int(np.count_nonzero(arr))
                total_cells += count
                if total_cells > MAX_CELLS:
                    raise ValueError("修改前后网格合计超过 100 万已涂格，本版无法同时载入")
                versions.append((arr, meta))
                entries = manifests[rev].get("navigation_files", [])
                entry = next((item for item in entries if item.get("path") == path), None)
                data = target.read_bytes()
                valid = entry and entry.get("size") == len(data) and entry.get("sha256") == hashlib.sha256(data).hexdigest()
                checks.append({"status": "pass" if valid else ("error" if entry else "warning"),
                               "label": path + (" · 修改前清单" if rev == base else " · 修改后清单"),
                               "detail": "大小和 SHA-256 一致" if valid else "清单缺失或大小/SHA-256 不一致"})
                for warning in warnings:
                    checks.append({"status": "warning", "label": path, "detail": warning})
            comparison = compare_grid(*versions)
            comparison["path"] = path
            declaration = next((item for item in declarations.get("layers", [])
                                if disk_tier(item.get("id")) == comparison["tier"] and item.get("map_name") == comparison["map"]), {})
            comparison["map_category"] = declaration.get("map_category", "layered")
            if comparison["map_category"] == "independent":
                comparison["notice"] = "独立场景坐标不能叠到大地图；本版仅显示检查摘要"
            if not comparison["comparable"]:
                checks.append({"status": "warning", "label": path, "detail": comparison["notice"]})
            files.append(comparison)
        json_changes = {}
        for path, key in (("links.json", "links"), ("levels.json", "layers")):
            if path in paths:
                json_changes[path] = object_changes(read_json(base, path), read_json(head, path), key)
        unsupported = [p for p in paths if p not in grids and p not in ("dataset.json", "links.json", "levels.json")]
        if unsupported:
            checks.append({"status": "warning", "label": "其它文件", "detail": "本版未比较：" + "、".join(unsupported)})
        return {"repository": repository, "number": number, "base": base, "head": head,
                "files": files, "changed_paths": paths, "checks": checks, "json_changes": json_changes}

"""Force the local navigation data worktree to match its remote repository."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path


class DataRepositorySync:
    """Synchronize one Git worktree, treating the remote default branch as truth."""

    def __init__(self, repository: Path):
        self.repository = Path(repository)
        self._lock = threading.Lock()

    def _git(self, *args: str, timeout: int = 300, check: bool = True,
             extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env.update({
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        })
        if extra_env:
            env.update(extra_env)
        try:
            process = subprocess.run(
                ["git", "-C", str(self.repository), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            raise ValueError("未找到 Git，无法同步数据仓库") from None
        except subprocess.TimeoutExpired:
            raise ValueError(f"Git 命令超时：git {' '.join(args[:2])}") from None
        if check and process.returncode:
            detail = process.stderr.decode("utf-8", "replace").strip()
            raise ValueError("Git 同步失败：" + (detail[-1200:] or f"退出码 {process.returncode}"))
        return process

    def _verified_root(self) -> Path:
        requested = self.repository.expanduser()
        if not requested.is_dir():
            raise ValueError(f"数据仓库目录不存在：{requested}")
        process = self._git("rev-parse", "--show-toplevel", check=False)
        if process.returncode:
            detail = process.stderr.decode("utf-8", "replace").strip()
            raise ValueError(f"数据目录不是 Git 工作区：{requested}" + (f"（{detail}）" if detail else ""))
        root = Path(process.stdout.decode("utf-8", "replace").strip())
        try:
            same_root = root.resolve() == requested.resolve()
        except OSError:
            same_root = False
        if not same_root:
            raise ValueError("同步目录必须正好是数据仓库根目录，不能是仓库内的子目录")
        bare = self._git("rev-parse", "--is-bare-repository").stdout.decode().strip()
        if bare == "true":
            raise ValueError("同步目录必须是带工作区的 Git 仓库，不能是裸仓库")
        return root

    def _remote_default_branch(self) -> str:
        process = self._git("ls-remote", "--symref", "origin", "HEAD", timeout=120)
        for line in process.stdout.decode("utf-8", "replace").splitlines():
            match = re.fullmatch(r"ref: refs/heads/(.+)\tHEAD", line.strip())
            if not match:
                continue
            branch = match.group(1)
            valid = self._git("check-ref-format", "--branch", branch, check=False)
            if valid.returncode:
                raise ValueError("远端默认分支名不合法")
            return branch
        raise ValueError("无法确定数据仓库 origin 的默认分支")

    def _revision(self, revision: str) -> str | None:
        process = self._git("rev-parse", "--verify", revision, check=False)
        if process.returncode:
            return None
        value = process.stdout.decode("ascii", "replace").strip()
        return value or None

    def _requires_lfs(self, revision: str) -> bool:
        attrs = self._git("show", f"{revision}:.gitattributes", check=False)
        return attrs.returncode == 0 and b"filter=lfs" in attrs.stdout

    def _paths(self, process: subprocess.CompletedProcess[bytes]) -> set[str]:
        return {
            path for path in process.stdout.decode("utf-8", "replace").split("\0")
            if path
        }

    def _changed_paths(self, previous: str | None, target: str) -> set[str]:
        if previous:
            process = self._git(
                "diff", "--name-only", "-z", previous, target
            )
        else:
            process = self._git(
                "ls-tree", "-r", "--name-only", "-z", target
            )
        return self._paths(process)

    def _dirty_tracked_paths(self) -> set[str]:
        paths: set[str] = set()
        for args in (
            ("diff", "--name-only", "-z", "HEAD"),
            ("diff", "--cached", "--name-only", "-z", "HEAD"),
        ):
            process = self._git(*args, check=False)
            if process.returncode == 0:
                paths.update(self._paths(process))
        return paths

    def _target_lfs_paths(self, target: str) -> set[str]:
        probe = self._git("lfs", "version", timeout=30, check=False)
        if probe.returncode:
            raise ValueError("数据仓库使用 Git LFS，请先安装 Git LFS 再同步")
        listed = self._git("lfs", "ls-files", "--name-only", target)
        return {
            path for path in listed.stdout.decode("utf-8", "replace").splitlines()
            if path
        }

    def _lfs_pointer(self, revision: str, path: str) -> tuple[str, int] | None:
        process = self._git("show", f"{revision}:{path}")
        text = process.stdout.decode("ascii", "replace")
        oid = re.search(r"^oid sha256:([0-9a-f]{64})$", text, re.MULTILINE)
        size = re.search(r"^size (\d+)$", text, re.MULTILINE)
        if not oid or not size:
            return None
        return oid.group(1), int(size.group(1))

    def _prepare_lfs(self, target: str, paths: set[str]) -> dict[str, tuple[str, int]]:
        pointers = {}
        for name in sorted(paths):
            pointer = self._lfs_pointer(target, name)
            if pointer is None:
                raise ValueError(f"无法读取 LFS 指针：{name}")
            pointers[name] = pointer

        ordered = sorted(paths)
        for start in range(0, len(ordered), 50):
            chunk = ordered[start:start + 50]
            include = ",".join(chunk)
            self._git(
                "-c", "lfs.fetchrecentalways=false",
                "-c", "lfs.fetchrecentrefsdays=0",
                "-c", "lfs.fetchrecentcommitsdays=0",
                "lfs", "fetch", "--include=" + include,
                "origin", target, timeout=900,
            )
        return pointers

    def _materialize_lfs(self, pointers: dict[str, tuple[str, int]]) -> int:
        ordered = sorted(pointers)
        for start in range(0, len(ordered), 50):
            chunk = ordered[start:start + 50]
            self._git("lfs", "checkout", *chunk, timeout=300)

        for name, (expected_oid, expected_size) in pointers.items():
            path = self.repository / name
            try:
                with path.open("rb") as handle:
                    actual_size = path.stat().st_size
                    actual_oid = hashlib.file_digest(handle, "sha256").hexdigest()
            except OSError:
                raise ValueError(f"Git LFS 文件不存在或无法读取：{name}") from None
            if actual_size != expected_size or actual_oid != expected_oid:
                raise ValueError(f"Git LFS 哈希校验失败：{name}")
        return len(pointers)

    def sync(self) -> dict:
        """Reset the worktree to origin's default branch and return a summary."""
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "busy": True, "error": "数据仓库同步已在运行中"}
        try:
            self.repository = self._verified_root()
            remote = self._git("remote", "get-url", "origin").stdout.decode(
                "utf-8", "replace"
            ).strip()
            branch = self._remote_default_branch()
            previous = self._revision("HEAD")
            dirty_paths = self._dirty_tracked_paths()
            dirty_before = len([
                line for line in self._git(
                    "status", "--porcelain=v1", "--untracked-files=all"
                ).stdout.decode("utf-8", "replace").splitlines()
                if line
            ])

            self._git(
                "fetch", "--prune", "--no-tags", "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                timeout=600,
            )
            target = self._revision(f"refs/remotes/origin/{branch}")
            if not target:
                raise ValueError(f"远端分支 origin/{branch} 不存在")

            changed_paths = self._changed_paths(previous, target)
            changed_files = len(changed_paths)

            uses_lfs = self._requires_lfs(target)
            lfs_paths: set[str] = set()
            if uses_lfs:
                target_lfs = self._target_lfs_paths(target)
                candidates = changed_paths | dirty_paths
                lfs_paths = target_lfs & candidates
                for name in target_lfs:
                    path = self.repository / name
                    try:
                        with path.open("rb") as handle:
                            if handle.read(64).startswith(
                                b"version https://git-lfs.github.com/spec/v1"
                            ):
                                lfs_paths.add(name)
                    except OSError:
                        continue

            lfs_pointers = (
                self._prepare_lfs(target, lfs_paths) if lfs_paths else {}
            )
            skip_smudge = {"GIT_LFS_SKIP_SMUDGE": "1"}
            self._git(
                "checkout", "--force", "-B", branch, target,
                extra_env=skip_smudge,
            )
            self._git("reset", "--hard", target, extra_env=skip_smudge)
            self._git("clean", "-ffdx")
            lfs_files_checked = (
                self._materialize_lfs(lfs_pointers) if lfs_pointers else 0
            )

            current = self._revision("HEAD")
            if current != target:
                raise ValueError("同步后 HEAD 与远端默认分支不一致")
            status = self._git(
                "status", "--porcelain=v1", "--untracked-files=all"
            ).stdout.decode("utf-8", "replace").strip()
            if status:
                raise ValueError("同步后工作区仍有本地改动")
            return {
                "ok": True,
                "repository": str(self.repository),
                "remote": remote,
                "branch": branch,
                "previous": previous,
                "current": current,
                "changed_files": changed_files,
                "dirty_files_replaced": dirty_before,
                "lfs": uses_lfs,
                "lfs_files_checked": lfs_files_checked,
            }
        except ValueError as error:
            return {"ok": False, "error": str(error)}
        finally:
            self._lock.release()

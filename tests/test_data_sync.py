import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nav_grid_editor.services.maps.data_sync import DataRepositorySync


def git(repository: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process.stdout.strip()


class DataRepositorySyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.local = self.root / "local"

        subprocess.run(
            ["git", "init", "--bare", "--initial-branch=master", str(self.remote)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "clone", str(self.remote), str(self.seed)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git(self.seed, "config", "user.name", "Data Sync Test")
        git(self.seed, "config", "user.email", "data-sync@example.invalid")
        (self.seed / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        (self.seed / "map_4.grid.npz").write_bytes(b"remote-v1")
        (self.seed / "levels.json").write_text('{"schema_version":1}', encoding="utf-8")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "initial")
        git(self.seed, "push", "-u", "origin", "master")

        subprocess.run(
            ["git", "clone", str(self.remote), str(self.local)],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        git(self.local, "config", "user.name", "Data Sync Test")
        git(self.local, "config", "user.email", "data-sync@example.invalid")

    def test_force_aligns_worktree_and_discards_every_local_state(self):
        (self.local / "map_4.grid.npz").write_bytes(b"local-uncommitted")
        (self.local / "local-only.tmp").write_text("ignored", encoding="utf-8")
        nested = self.local / "untracked" / "nested"
        nested.mkdir(parents=True)
        (nested / "file.bin").write_bytes(b"local")
        git(self.local, "add", "map_4.grid.npz")
        git(self.local, "commit", "-m", "local only")

        (self.seed / "map_4.grid.npz").write_bytes(b"remote-v2")
        (self.seed / "links.json").write_text('{"links":[]}', encoding="utf-8")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "remote update")
        git(self.seed, "push", "origin", "master")

        result = DataRepositorySync(self.local).sync()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["branch"], "master")
        self.assertEqual((self.local / "map_4.grid.npz").read_bytes(), b"remote-v2")
        self.assertEqual((self.local / "links.json").read_text(encoding="utf-8"), '{"links":[]}')
        self.assertFalse((self.local / "local-only.tmp").exists())
        self.assertFalse((self.local / "untracked").exists())
        self.assertEqual(git(self.local, "status", "--porcelain=v1"), "")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"),
                         git(self.remote, "rev-parse", "master"))

    def test_rejects_non_repository_and_repository_subdirectory(self):
        plain = self.root / "plain"
        plain.mkdir()
        result = DataRepositorySync(plain).sync()
        self.assertFalse(result["ok"])
        self.assertIn("不是 Git 工作区", result["error"])

        subdir = self.local / "untracked-dir"
        subdir.mkdir()
        result = DataRepositorySync(subdir).sync()
        self.assertFalse(result["ok"])
        self.assertIn("必须正好是数据仓库根目录", result["error"])

    def test_lfs_materialization_uses_selected_paths_and_hash_verification(self):
        name = "localization/changed.png"
        payload = b"actual-lfs-content"
        path = self.local / name
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
        oid = hashlib.sha256(payload).hexdigest()
        pointer = (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{oid}\n"
            f"size {len(payload)}\n"
        ).encode("ascii")
        calls = []

        def fake_git(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("show", f"HEAD:{name}"):
                return subprocess.CompletedProcess(["git", *args], 0, pointer, b"")
            return subprocess.CompletedProcess(["git", *args], 0, b"", b"")

        sync = DataRepositorySync(self.local)
        with mock.patch.object(sync, "_git", side_effect=fake_git):
            pointers = sync._prepare_lfs("HEAD", {name})
            checked = sync._materialize_lfs(pointers)

        self.assertEqual(checked, 1)
        self.assertTrue(any(
            "lfs" in args and "fetch" in args and f"--include={name}" in args
            for args in calls
        ))
        self.assertTrue(any(args == ("lfs", "checkout", name) for args in calls))
        self.assertFalse(any(args[:2] == ("lfs", "pull") for args in calls))


if __name__ == "__main__":
    unittest.main()

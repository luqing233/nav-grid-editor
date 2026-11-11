import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from nav_grid_editor.services.maps.pr_review import DataPRReview, compare_grid, object_changes, parse_pr
from nav_grid_editor.services.maps.settings import GRID_AXIS_CONVENTION
from nav_grid_editor.services.maps import MapService
from tests.test_server_routes import _ServerMixin


def grid(values, origin=(0, 0, 0), size=1):
    return np.array(values, dtype=np.uint8), dict(magic="okef-nav-grid", schema_version=2,
        map_name="map02", zoom="4", tier="", origin=list(origin), cell_size=size,
        axis_convention=GRID_AXIS_CONVENTION, floor_regions=[])


class PRComparisonTest(unittest.TestCase):
    def test_github_reuses_existing_cli_login(self):
        svc=DataPRReview(Path("unused"),Path("unused"))
        response=subprocess.CompletedProcess([],0,stdout=b'{"number":2,"title":"test"}',stderr=b"")
        with patch("nav_grid_editor.services.maps.pr_review.shutil.which",return_value="gh"),patch("nav_grid_editor.services.maps.pr_review.subprocess.run",return_value=response) as run,patch("nav_grid_editor.services.maps.pr_review.urllib.request.urlopen") as anonymous:
            self.assertEqual(svc._github("owner/nav-grid-data",2)["number"],2)
            self.assertEqual(run.call_args.args[0],["gh","api","repos/owner/nav-grid-data/pulls/2"])
            anonymous.assert_not_called()

    def test_pr_input_is_scoped_to_configured_repository(self):
        for value in ("2", "#2", "https://github.com/luqing233/nav-grid-data/pull/2", "https://github.com/luqing233/nav-grid-data/pull/2/files"):
            self.assertEqual(parse_pr(value, "luqing233/nav-grid-data"), 2)
        for value in ("0", "-2", "2;git reset", "http://localhost/pull/2", "https://github.com/other/repo/pull/2"):
            with self.assertRaises(ValueError): parse_pr(value, "luqing233/nav-grid-data")

    def test_origin_shift_aligns_cells_in_world_space(self):
        before = grid([[1, 2]])
        after = grid([[0, 1, 2, 1]], origin=(-1, 0, 0))
        result = compare_grid(before, after)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["transitions"], {"0>1": 1})
        self.assertEqual(result["regions"][0]["bounds"], dict(x0=2, x1=3, z0=0, z1=1))
        self.assertIn("shape", result["metadata_changes"])

    def test_all_transitions_and_disconnected_change_regions(self):
        result = compare_grid(grid([[0,1,2,0,0,0,1,2,0]]), grid([[1,0,1,0,0,0,2,0,2]]))
        self.assertEqual(result["transitions"], {"0>1":1,"1>0":1,"2>1":1,"1>2":1,"2>0":1,"0>2":1})
        self.assertEqual([r["count"] for r in result["regions"]], [3, 3])

    def test_incompatible_cell_geometry_is_not_resampled(self):
        for after in (grid([[1]], size=2), grid([[1]], origin=(.5, 0, 0))):
            result = compare_grid(grid([[1]]), after)
            self.assertFalse(result["comparable"])
            self.assertEqual(result["changes"], [])
            self.assertIsNotNone(result["before"])
            self.assertIsNotNone(result["after"])

    def test_added_removed_and_height_only_grids_remain_navigable(self):
        self.assertEqual(compare_grid(None,grid([[1,2]]))["transitions"], {"0>1":1,"0>2":1})
        self.assertEqual(compare_grid(grid([[1,2]]),None)["transitions"], {"1>0":1,"2>0":1})
        a,b=grid([[1]]),grid([[1]])
        b[1]["floor_regions"]=[dict(x0=0,x1=1,z0=0,z1=1,y_min=10,y_max=20)]
        result=compare_grid(a,b)
        self.assertTrue(result["floor_regions_changed"])
        self.assertTrue(result["regions"][0]["metadata"])
        self.assertEqual(result["changes"], [])

    def test_links_compare_by_identity_not_order(self):
        result=object_changes({"links":[{"id":"a","cost":1},{"id":"b"}]},
                              {"links":[{"id":"c"},{"id":"a","cost":2}]},"links")
        self.assertEqual(result,dict(added=["c"],removed=["b"],modified=["a"]))


@unittest.skipUnless(shutil.which("git"), "Git is required for snapshot tests")
class PRSnapshotTest(unittest.TestCase):
    def test_snapshot_reads_do_not_checkout_or_overwrite_working_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=root/"repo";repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git","-C",str(repo),"-c","user.name=Review Test","-c","user.email=review@example.invalid",*args],stderr=subprocess.PIPE).decode().strip()
            git("init");git("remote","add","origin","https://github.com/owner/nav-grid-data.git")
            path=repo/"map02_4.grid.npz"
            def save(values, bad_hash=False):
                arr,meta=grid(values)
                np.savez_compressed(path,cells=arr,meta=np.array(json.dumps(meta)))
                entry=dict(path=path.name,size=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                if bad_hash: entry["sha256"]="0"*64
                (repo/"dataset.json").write_text(json.dumps({"navigation_files":[entry]}),encoding="utf-8")
                git("add",".");git("commit","-m","fixture")
                return git("rev-parse","HEAD")
            base=save([[1,0,0]]);head=save([[1,1,0]],bad_hash=True)
            path.write_bytes(b"uncommitted local grid")
            original_status=git("status","--porcelain");original_index=git("ls-files","--stage")
            svc=DataPRReview(repo,root/"cache");original_git=svc._git
            def local_git(*args,**kwargs):
                return b"" if args[0]=="fetch" else original_git(*args,**kwargs)
            pr=dict(base={"sha":base},head={"sha":head},title="test",html_url="https://github.com/owner/nav-grid-data/pull/2",state="open")
            with patch.object(svc,"_github",return_value=pr),patch.object(svc,"_git",side_effect=local_git):
                result=svc.load("2")
                result["files"][0]["changes"].clear()
                cached=svc.load("2")
            self.assertEqual(len(cached["files"][0]["changes"]),1)
            self.assertTrue(any(check["status"]=="error" for check in result["checks"]))
            self.assertEqual(git("rev-parse","HEAD"),head)
            self.assertEqual(path.read_bytes(),b"uncommitted local grid")
            self.assertEqual(git("status","--porcelain"),original_status)
            self.assertEqual(git("ls-files","--stage"),original_index)

    def test_lfs_pointer_cannot_be_mistaken_for_grid_and_path_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);svc=DataPRReview(root,root/"cache")
            pointer=b"version https://git-lfs.github.com/spec/v1\noid sha256:"+b"0"*64+b"\nsize 10\n"
            with patch.object(svc,"_git",side_effect=[str(len(pointer)).encode(),pointer,pointer]):
                with self.assertRaisesRegex(ValueError,"LFS"):
                    svc._read("a"*40,"map02_4.grid.npz",root/"cache")
            with self.assertRaises(ValueError):svc._read("a"*40,"../outside",root/"cache")


class OpenPRListTest(unittest.TestCase):
    def service(self):
        return DataPRReview(Path("unused"), Path("unused"))

    def test_lists_every_page_including_drafts_and_deduplicates(self):
        svc = self.service()
        def pr(number, **kwargs):
            return dict(number=number, title=f"PR {number}", state="open",
                        html_url=f"https://github.com/owner/data/pull/{number}", **kwargs)
        pages = [[pr(n) for n in range(1, 101)],
                 [pr(100), pr(101, draft=True), {**pr(102), "state": "closed"}]]
        with patch.object(svc, "_git", return_value=b"git@github.com:owner/data.git\n") as git, \
             patch.object(svc, "_github_request", side_effect=pages) as github:
            result = svc.list_open()
        self.assertEqual(result["repository"], "owner/data")
        self.assertEqual([item["number"] for item in result["pulls"]], list(range(101, 0, -1)))
        self.assertTrue(result["pulls"][0]["draft"])
        self.assertEqual(set(result["pulls"][0]), {"number", "title", "url", "draft"})
        self.assertIn("state=open", github.call_args_list[0].args[0])
        self.assertTrue(github.call_args_list[1].args[0].endswith("per_page=100&page=2"))
        git.assert_called_once_with("remote", "get-url", "origin")

    def test_empty_list_and_failure_are_distinct(self):
        svc = self.service()
        with patch.object(svc, "_git", return_value=b"https://github.com/owner/data.git"), \
             patch.object(svc, "_github_request", return_value=[]):
            self.assertEqual(svc.list_open()["pulls"], [])
        with patch.object(svc, "_git", return_value=b"https://github.com/owner/data.git"), \
             patch.object(svc, "_github_request", side_effect=ValueError("limited")):
            with self.assertRaisesRegex(ValueError, "limited"):
                svc.list_open()

    def test_rejects_non_github_repository_without_network(self):
        svc = self.service()
        with patch.object(svc, "_git", return_value=b"https://example.com/data"), \
             patch.object(svc, "_github_request") as github:
            with self.assertRaisesRegex(ValueError, "GitHub"):
                svc.list_open()
            github.assert_not_called()

    def test_later_page_error_does_not_return_incomplete_list(self):
        svc = self.service()
        with patch.object(svc, "_repository_name", return_value="owner/data"), \
             patch.object(svc, "_github_request", side_effect=[[{"state": "closed"}] * 100, ValueError("page failed")]):
            with self.assertRaisesRegex(ValueError, "page failed"):
                svc.list_open()


class PRReviewApiTest(_ServerMixin, unittest.TestCase):
    def test_merge_api_validates_requests_and_exports_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            svc=MapService(data_root=Path(temp))
            self._start_server(svc)
            try:
                for body in ([],{},dict(action="export"),dict(action="bogus")):
                    code,data=self._post_json("/api/pr-review/merge",body)
                    self.assertEqual(code,400)
                expected=dict(ok=True,token="session",files=[],ready=False)
                with patch.object(svc.pr_review,"merge_prepare",return_value=expected) as prepare:
                    code,data=self._post_json("/api/pr-review/merge",dict(action="prepare",prs=["1","2"]))
                    self.assertEqual(code,200);self.assertEqual(data,expected)
                    prepare.assert_called_once_with(["1","2"])
                with patch.object(svc.pr_review,"merge_action",return_value=expected) as resolve:
                    code,data=self._post_json("/api/pr-review/merge",dict(action="resolve",token="session",choices={"a":"incoming"}))
                    self.assertEqual(code,200)
                    resolve.assert_called_once_with("session","resolve",{"a":"incoming"})
                for action in ("publish","publication_status","merge_to_base"):
                    with patch.object(svc.pr_review,"merge_action",return_value=dict(ok=True,phase="checking",running=True)) as publish:
                        code,data=self._post_json("/api/pr-review/merge",dict(action=action,token="session"))
                        self.assertEqual(code,200);self.assertTrue(data["running"])
                        publish.assert_called_once_with("session",action,None)
                archive=Path(temp)/"result.zip";archive.write_bytes(b"PK-test")
                with patch.object(svc.pr_review,"merge_action",return_value=archive):
                    code,body,headers=self._request("/api/pr-review/merge",data=json.dumps(dict(action="export",token="session")).encode(),headers={"Content-Type":"application/json"})
                    self.assertEqual(code,200);self.assertEqual(body,b"PK-test")
                with patch.object(svc.pr_review,"merge_action",side_effect=ValueError("过期")):
                    code,data=self._post_json("/api/pr-review/merge",dict(action="export",token="expired"))
                    self.assertEqual(code,400);self.assertIn("过期",data["error"])
            finally:self._stop_server()

    def test_open_pr_endpoint_returns_list_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            svc = MapService(data_root=Path(temp))
            self._start_server(svc)
            try:
                expected = dict(ok=True, repository="owner/data", pulls=[dict(number=3, title="Test", draft=False)])
                with patch.object(svc.pr_review, "list_open", return_value=expected) as listing:
                    code, body, _ = self._request("/api/pr-review/open")
                    self.assertEqual(code, 200)
                    self.assertEqual(json.loads(body), expected)
                    listing.assert_called_once_with()
                with patch.object(svc.pr_review, "list_open", side_effect=ValueError("GitHub unavailable")):
                    code, body, _ = self._request("/api/pr-review/open")
                    self.assertEqual(code, 502)
                    self.assertFalse(json.loads(body)["ok"])
                    self.assertIn("GitHub", json.loads(body)["error"])
            finally:
                self._stop_server()

    def test_load_endpoint_validates_input_and_returns_read_only_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            svc=MapService(data_root=Path(temp))
            self._start_server(svc)
            try:
                for body in ([],{},dict(pr=2)):
                    code,data=self._post_json("/api/pr-review/load",body)
                    self.assertEqual(code,400)
                    self.assertFalse(data["ok"])
                with patch.object(svc.pr_review,"load",return_value=dict(ok=True,files=[],base="a"*40,head="b"*40)) as load:
                    code,data=self._post_json("/api/pr-review/load",dict(pr="2"))
                    self.assertEqual(code,200)
                    self.assertTrue(data["ok"])
                    load.assert_called_once_with("2")
                with patch.object(svc.pr_review,"load",side_effect=ValueError("LFS 实体缺失")):
                    code,data=self._post_json("/api/pr-review/load",dict(pr="2"))
                    self.assertEqual(code,400)
                    self.assertIn("LFS",data["error"])
            finally:self._stop_server()

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from nav_grid_editor.integrations.github_accounts import GitHubAccount, list_accounts
from nav_grid_editor.services.maps.grid import _load_grid_npz
from nav_grid_editor.services.maps.local_pr import LocalSnapshot, LocalPublisher, LocalPRService
from tests import test_grid_merge as fixtures
from tests.test_server_routes import _ServerMixin


class AccountsTest(unittest.TestCase):
    def test_list_never_exposes_token_or_diagnostics(self):
        raw={"hosts":{"github.com":[{"login":"alice","active":True,"state":"success","token":"secret","tokenSource":"keyring"},
                                      {"login":"bob","state":"error","error":"private diagnostic"}]}}
        with patch("nav_grid_editor.integrations.github_accounts.gh_command",return_value=json.dumps(raw).encode()) as command:
            result=list_accounts()
        self.assertNotIn("secret",json.dumps(result))
        self.assertNotIn("diagnostic",json.dumps(result))
        self.assertEqual(result["accounts"][1],dict(login="bob",active=False,available=False))
        self.assertNotIn("--show-token",command.call_args.args)

    def test_selected_account_ignores_global_token_without_switching(self):
        calls=[]
        def command(*args,**kwargs):
            calls.append((args,kwargs))
            return b"selected-secret" if args[:2]==("auth","token") else b'{"login":"alice","id":42}'
        with patch.dict(os.environ,{"GH_TOKEN":"other-account","GITHUB_TOKEN":"another-account"}),patch("nav_grid_editor.integrations.github_accounts.gh_command",side_effect=command):
            account=GitHubAccount("alice")
            self.assertEqual(account.environment()["GH_TOKEN"],"selected-secret")
            self.assertNotIn("GITHUB_TOKEN",account.environment())
            self.assertEqual(os.environ["GH_TOKEN"],"other-account")
        self.assertEqual(calls[0][0],("auth","token","--hostname","github.com","--user","alice"))
        self.assertEqual(calls[1][1]["env"]["GH_TOKEN"],"selected-secret")
        self.assertEqual(account.redact("failure selected-secret"),"failure [已隐藏凭证]")

    def test_wrong_identity_rejected(self):
        with patch("nav_grid_editor.integrations.github_accounts.gh_command",side_effect=[b"secret",b'{"login":"bob"}']):
            with self.assertRaisesRegex(ValueError,"不一致"):GitHubAccount("alice")


class LocalPRTest(unittest.TestCase):
    git=fixtures.PRMergeSessionTest.git
    commit=fixtures.PRMergeSessionTest.commit
    pr=fixtures.PRMergeSessionTest.pr

    def setUp(self):
        fixtures.PRMergeSessionTest.setUp(self)
        self.account=SimpleNamespace(login="alice",user={"login":"alice","id":42},environment=lambda:dict(os.environ,GH_TOKEN="test-secret"),redact=lambda s:s.replace("test-secret","[redacted]"))
        self.account.api=lambda path: ({"permissions":{"push":True},"default_branch":"main"} if path=="repos/test/data" else {"sha":self.target})
        original=self.review._git
        def git(*args,**kwargs):
            if args[0]=="fetch":return b""
            return original(*args,**kwargs)
        self.fetch_patch=patch.object(self.review,"_git",side_effect=git)
        self.fetch_patch.start();self.addCleanup(self.fetch_patch.stop)
        self.write([[1,1,0]])

    def write(self,values):
        a,m=fixtures.grid(values)
        np.savez_compressed(self.repo/self.path,cells=a,meta=np.array(json.dumps(m)))

    def snapshot(self,paths=None):
        return LocalSnapshot(self.review,self.account,paths)

    def test_only_saved_navigation_files_and_stale_change(self):
        (self.repo/"credentials.txt").write_text("not navigation")
        before=(self.repo/self.path).read_bytes()
        snapshot=self.snapshot()
        self.assertEqual([f["path"] for f in snapshot.files],[self.path])
        self.assertEqual(snapshot.files[0]["changes"],1)
        self.assertEqual((self.repo/self.path).read_bytes(),before)
        self.assertEqual(self.git("ls-files","--stage"),self.index)
        self.write([[1,2,0]])
        with self.assertRaisesRegex(ValueError,"又有变化"):snapshot.check_fresh()

    def test_permissions_bad_paths_and_invalid_grid(self):
        for paths in (["../secret"],["credentials.txt"],[],"grid"):
            with self.assertRaises(ValueError):self.snapshot(paths)
        (self.repo/self.path).write_bytes(b"invalid npz")
        with self.assertRaises(Exception):self.snapshot()
        self.account.api=lambda path:{"permissions":{"push":False}}
        with self.assertRaisesRegex(ValueError,"推送权限"):self.snapshot()

    def test_already_merged_edits_are_not_resubmitted(self):
        self.git("checkout","-q","-f","--detach",self.target)
        self.target=self.commit([[1,1,2]],"upstream with my change and another change")
        self.git("checkout","-q","--detach",self.base)
        self.write([[0,1,0]])
        with self.assertRaisesRegex(ValueError,"没有可提交"):self.snapshot()

    def test_preserves_upstream_changes_by_using_common_ancestor(self):
        old=self.target
        self.target=self.commit([[1,0,2]],"upstream addition")
        self.git("checkout","-q","--detach",old)
        self.write([[1,1,0]])
        snapshot=self.snapshot()
        self.assertEqual(snapshot.target,old)
        self.assertEqual(snapshot.remote_target,self.target)
        self.assertEqual(snapshot.files[0]["changes"],1)

    def test_changed_manifest_is_rehashed_without_schema_change(self):
        original={"schema_version":1,"release_id":"old","parent_release_id":None,"navigation_files":[],
                  "coordinate_spaces":[],"registrations":[{"map_name":"map02","tier":"","zoom":"4","grid":self.path}]}
        (self.repo/"dataset.json").write_text(json.dumps(original),encoding="utf-8")
        snapshot=self.snapshot()
        snapshot.export()
        data=json.loads(snapshot.result["dataset.json"])
        self.assertEqual(set(data),set(original))
        self.assertEqual(data["navigation_files"][0]["size"],len(snapshot.result[self.path]))
        self.assertEqual((self.repo/"dataset.json").read_text(encoding="utf-8"),json.dumps(original))

    def test_manifest_derived_fields_alone_are_not_a_new_change(self):
        original={"schema_version":1,"release_id":"old","parent_release_id":None,"navigation_files":[{"path":self.path,"size":1,"sha256":"old"}],
                  "coordinate_spaces":[],"registrations":[{"map_name":"map02","tier":"","zoom":"4","grid":self.path}]}
        (self.repo/"dataset.json").write_text(json.dumps(original),encoding="utf-8")
        self.target=self.commit([[1,0,0]],"manifest")
        original["navigation_files"][0].update(size=123,sha256="new")
        original["release_id"]="new"
        (self.repo/"dataset.json").write_text(json.dumps(original),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"没有可提交"):self.snapshot()

    def test_account_mismatch_after_prepare_and_bad_title(self):
        service=LocalPRService(self.review);snapshot=self.snapshot();service.previews[snapshot.token]=snapshot
        with self.assertRaisesRegex(ValueError,"账号已改变"):service.publish(snapshot.token,"bob","feat(data): test")
        for title in ("",None,"a\nb","x"*161):
            with self.assertRaises(ValueError):service.publish(snapshot.token,"alice",title)

    def test_isolated_publish_author_parent_lfs_and_recovery(self):
        # Exercise real git and LFS, while substituting only GitHub/network endpoints.
        (self.repo/".gitattributes").write_text("*.grid.npz filter=lfs diff=lfs merge=lfs -text\n")
        self.target=self.commit([[1,0,0]],"lfs baseline")
        self.write([[1,1,0]])
        saved=(self.repo/self.path).read_bytes();index=self.git("ls-files","--stage")
        snapshot=self.snapshot()
        publisher=LocalPublisher(self.review,snapshot,account=self.account,title="feat(data): 本地网格")
        self.remote=self.root/"remote.git"
        subprocess.check_output(["git","init","--bare","-q",str(self.remote)])
        self.git("-c","core.hooksPath="+str(self.root/"empty-hooks"),"push",str(self.remote),self.target+":refs/heads/main")
        self.git("remote","add","local-target",str(self.remote))
        self.git("lfs","push","--all","local-target",self.target)
        created=[]
        def gh(*args):
            if args[:2]==("pr","list"):return json.dumps(created).encode()
            if args[:3]==("api","--method","POST"):
                payload=json.loads(Path(args[-1]).read_bytes())
                self.assertEqual(payload["base"],"main")
                created.append(dict(url="https://github.com/test/data/pull/10",number=10,state="OPEN",body=payload["body"],headRefOid=publisher.state["head"]))
                raise ValueError("lost response test-secret")
            raise AssertionError(args)
        original_build=publisher._build
        def build():
            original_build()
            publisher.git("remote","set-url","origin",str(self.remote))
            publisher.git("config","--unset","lfs.url")
        with patch("nav_grid_editor.services.maps.local_pr.GitHubAccount",return_value=self.account),patch.object(publisher,"gh",side_effect=gh),patch.object(publisher,"_build",side_effect=build):
            publisher._run()
            self.assertEqual(publisher.state["phase"],"failed",publisher.state)
            self.assertTrue(publisher.state.get("pushed"),publisher.state)
            self.assertNotIn("test-secret",publisher.state["error"])
            publisher._run()
        self.assertEqual(publisher.state["phase"],"complete",publisher.state)
        self.assertEqual(len(created),1)
        head=publisher.state["head"]
        self.assertEqual(publisher.git("show","-s","--format=%P",head).decode().strip(),snapshot.target)
        self.assertEqual(publisher.git("show","-s","--format=%an <%ae>",head).decode().strip(),"alice <42+alice@users.noreply.github.com>")
        self.assertTrue(publisher.git("show",head+":"+self.path).startswith(b"version https://git-lfs.github.com/spec/v1"))
        self.assertEqual((self.repo/self.path).read_bytes(),saved)
        self.assertEqual(self.git("ls-files","--stage"),index)
        self.assertEqual(self.git("rev-parse","HEAD"),self.target)
        for path in publisher.directory.rglob("*"):
            if path.is_file() and (path.suffix==".json" or path.name=="config"):
                self.assertNotIn(b"test-secret",path.read_bytes())

    def test_target_moves_blocks_publication(self):
        publisher=LocalPublisher(self.review,self.snapshot(),account=self.account,title="feat(data): test")
        self.target=self.base
        with self.assertRaisesRegex(ValueError,"目标分支已更新"):publisher.fresh()


class LocalPRRoutesTest(_ServerMixin, unittest.TestCase):
    def test_requests_are_validated_and_dispatched(self):
        from nav_grid_editor.services.maps.service import MapService
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            service=MapService(data_root=Path(temp));service.local_pr=Mock()
            service.local_pr.accounts.return_value={"ok":True,"accounts":[]}
            service.local_pr.prepare.return_value={"ok":True,"token":"preview"}
            service.local_pr.publish.return_value={"ok":True,"phase":"checking"}
            service.local_pr.action.return_value={"ok":True,"phase":"complete"}
            self._start_server(service)
            try:
                self.assertEqual(self._request("/api/local-pr/accounts")[0],200)
                for body in ([1],{}, {"action":"publish"},{"action":"status","id":[]}):
                    self.assertEqual(self._post_json("/api/local-pr",body)[0],400)
                self.assertEqual(self._post_json("/api/local-pr",{"action":"prepare","account":"alice"})[0],200)
                self.assertEqual(self._post_json("/api/local-pr",{"action":"publish","token":"t","account":"alice","title":"title"})[0],200)
                self.assertEqual(self._post_json("/api/local-pr",{"action":"retry","id":"job"})[0],200)
            finally:self._stop_server()
            service.local_pr.publish.assert_called_once_with("t","alice","title")
            service.local_pr.action.assert_called_once_with("job",True)

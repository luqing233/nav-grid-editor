import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from nav_grid_editor.services.maps.grid import _load_grid_npz
from nav_grid_editor.services.maps.pr_publish import IntegrationPublisher
from nav_grid_editor.services.maps.pr_review import DataPRReview
from tests import test_grid_merge as fixtures


class IntegrationPublicationTest(unittest.TestCase):
    git=fixtures.PRMergeSessionTest.git
    commit=fixtures.PRMergeSessionTest.commit
    pr=fixtures.PRMergeSessionTest.pr

    def setUp(self):
        fixtures.PRMergeSessionTest.setUp(self)
        self.remote=self.root/"remote.git"
        subprocess.check_output(["git","init","--bare","-q",str(self.remote)],stderr=subprocess.PIPE)
        self.git("-c","core.hooksPath="+str(self.root/"no-hooks"),"push",str(self.remote),self.target+":refs/heads/main")
        r=self.review.merge_prepare(["1","2"])
        r=self.review.merge_action(r["token"],"resolve",{r["conflicts"][0]["id"]:"incoming"})
        self.session=self.review._merges[r["token"]]
        self.publisher=IntegrationPublisher(self.review,self.session)
        self.publisher.gh_path="gh"
        self.created=[]

    def remote_git(self,*args):
        return subprocess.check_output(["git","--git-dir",str(self.remote),*args],stderr=subprocess.PIPE)

    def fake_gh(self,*args):
        if args[:2]==("auth","status"):return b""
        if args[:2]==("api","repos/test/data"):
            return b'{"permissions":{"push":true},"allow_merge_commit":true}'
        if args[:2]==("pr","list"):
            return json.dumps(self.created).encode()
        if args[:3]==("api","--method","POST"):
            payload=json.loads(Path(args[-1]).read_text(encoding="utf-8"))
            self.assertEqual(payload["base"],"main")
            self.assertIn("\n\n",payload["body"])
            self.created.append(dict(number=9,url="https://github.com/test/data/pull/9",state="OPEN",
                                     body=payload["body"],headRefOid=self.publisher.state["head"]))
            return json.dumps(dict(number=9,html_url="https://github.com/test/data/pull/9",state="open")).encode()
        raise AssertionError(args)

    def local_build(self):
        IntegrationPublisher._build(self.publisher)
        self.publisher.git("remote","set-url","origin",str(self.remote))
        self.publisher.git("config","--unset","lfs.url")

    def test_publication_keeps_each_parent_and_never_changes_local_checkout(self):
        with patch.object(self.publisher,"gh",side_effect=self.fake_gh),patch.object(self.publisher,"_build",side_effect=self.local_build):
            self.publisher._run()
            self.assertEqual(self.publisher.state["phase"],"complete",self.publisher.state)
            self.publisher._run()
        self.assertEqual(len(self.created),1)
        commits=self.publisher.state["commits"]
        self.assertEqual(self.remote_git("show","-s","--format=%P",commits[0]).decode().split(),[self.target,self.head1])
        self.assertEqual(self.remote_git("show","-s","--format=%P",commits[1]).decode().split(),[commits[0],self.head2])
        content=self.remote_git("show",commits[1]+":"+self.path)
        self.assertEqual(_load_grid_npz(io.BytesIO(content))[0].tolist(),[[2,1,2]])
        self.assertEqual(_load_grid_npz(io.BytesIO(self.remote_git("show",commits[0]+":"+self.path)))[0].tolist(),[[2,1,0]])
        self.assertEqual(self.remote_git("rev-parse","refs/heads/main").decode().strip(),self.target)
        self.assertEqual(self.git("status","--porcelain"),self.status)
        self.assertEqual(self.git("ls-files","--stage"),self.index)
        self.assertEqual((self.repo/self.path).read_bytes(),b"user unsaved grid")

    def test_lost_create_response_recovers_without_duplicate_or_second_push(self):
        def lost(*args):
            result=self.fake_gh(*args)
            if args[:3]==("api","--method","POST"):raise ValueError("network response lost")
            return result
        with patch.object(self.publisher,"gh",side_effect=lost),patch.object(self.publisher,"_build",side_effect=self.local_build):
            self.publisher._run()
        self.assertEqual(self.publisher.state["phase"],"failed")
        self.assertTrue(self.publisher.state["pushed"])
        identity=self.publisher.state["id"]
        self.publisher=IntegrationPublisher(self.review,identity=identity)
        with patch.object(self.publisher,"gh",side_effect=self.fake_gh),patch.object(self.publisher,"_push") as push:
            self.publisher._run()
        push.assert_not_called()
        self.assertEqual(self.publisher.state["phase"],"complete",self.publisher.state)
        self.assertEqual(len(self.created),1)

    def test_changed_remote_branch_is_never_overwritten(self):
        self.local_build()
        branch=self.publisher.state["branch"]
        self.git("-c","core.hooksPath="+str(self.root/"no-hooks"),"push",str(self.remote),self.head1+":refs/heads/"+branch)
        with self.assertRaisesRegex(ValueError,"拒绝"):
            self.publisher._push()
        self.assertEqual(self.remote_git("rev-parse","refs/heads/"+branch).decode().strip(),self.head1)

    def test_missing_write_permission_does_not_build_or_push(self):
        def gh(*args):
            if args[0]=="auth":return b""
            return b'{"permissions":{"push":false},"allow_merge_commit":true}'
        with patch.object(self.publisher,"gh",side_effect=gh),patch.object(self.publisher,"_build") as build:
            self.publisher._run()
        build.assert_not_called()
        self.assertEqual(self.publisher.state["phase"],"failed")
        self.assertIn("推送权限",self.publisher.state["error"])

    def merge_fixture(self):
        self.publisher.save(number=9,url="https://github.com/test/data/pull/9",head="a"*40,phase="complete")
        return dict(state="open",draft=False,merged=False,mergeable=True,
                    head={"sha":"a"*40},base={"sha":self.target,"ref":"main","repo":{"full_name":"test/data"}})

    def test_merge_to_base_uses_reviewed_sha_and_regular_merge(self):
        pr=self.merge_fixture()
        calls=[]
        def gh(*args):
            calls.append(args)
            if args[:3]==("api","--method","PUT"):
                payload=json.loads(Path(args[-1]).read_text(encoding="utf-8"))
                self.assertEqual(payload,{"sha":"a"*40,"merge_method":"merge"})
                return json.dumps(dict(merged=True,sha="b"*40)).encode()
            return json.dumps(pr).encode()
        with patch.object(self.publisher,"gh",side_effect=gh):
            state=self.publisher.merge_to_base()
            repeated=self.publisher.merge_to_base()
        self.assertTrue(state["merged"])
        self.assertEqual(state["merge_sha"],"b"*40)
        self.assertEqual(len(calls),2)
        self.assertTrue(repeated["merged"])

    def test_changed_head_target_and_branch_protection_block_merge(self):
        for change in ("head","base","protection"):
            pr=self.merge_fixture()
            if change=="head":pr["head"]["sha"]="c"*40
            if change=="base":pr["base"]["sha"]="c"*40
            calls=[]
            def gh(*args):
                calls.append(args)
                if args[:3]==("api","--method","PUT"):raise ValueError("405 required review not approved")
                return json.dumps(pr).encode()
            with patch.object(self.publisher,"gh",side_effect=gh):
                result=self.publisher.merge_to_base()
            self.assertFalse(result.get("merged",False))
            self.assertTrue(result["merge_error"])
            self.assertEqual(len(calls),2 if change=="protection" else 1)

    def test_lost_merge_response_recovers_on_explicit_retry(self):
        pr=self.merge_fixture()
        def gh(*args):
            if args[:3]==("api","--method","PUT"):
                pr.update(merged=True,state="closed",merge_commit_sha="b"*40)
                raise ValueError("response lost")
            return json.dumps(pr).encode()
        with patch.object(self.publisher,"gh",side_effect=gh):
            first=self.publisher.merge_to_base()
            second=self.publisher.merge_to_base()
        self.assertIn("lost",first["merge_error"])
        self.assertTrue(second["merged"])
        self.assertEqual(second["merge_sha"],"b"*40)

    def test_publication_session_api_freezes_decisions_and_status_survives_restart(self):
        with patch.object(IntegrationPublisher,"start",lambda p: p.status()):
            state=self.review.merge_action(self.session.token,"publish")
        with self.assertRaisesRegex(ValueError,"发布流程"):
            self.session.resolve({self.session.response["conflicts"][0]["id"]:"base"})
        publisher=self.review._publications[state["id"]]
        publisher.save(phase="failed",error="network")
        restarted=DataPRReview(self.repo,self.review.cache)
        status=restarted.merge_action(self.session.token,"publication_status")
        self.assertEqual(status["error"],"network")
        self.assertEqual(status["branch"],state["branch"])

    def test_lfs_new_objects_are_pushed_as_pointers(self):
        self.git("checkout","-q","-f","--detach",self.target)
        (self.repo/".gitattributes").write_text("*.grid.npz filter=lfs diff=lfs merge=lfs -text\n")
        self.git("config","filter.lfs.clean","git-lfs clean -- %f")
        self.git("config","filter.lfs.smudge","git-lfs smudge -- %f")
        self.git("add","--renormalize",".")
        self.base=self.commit([[0,0,0]],"lfs base")
        self.target=self.commit([[1,0,0]],"lfs target")
        self.git("checkout","-q","--detach",self.base)
        self.head1=self.commit([[0,1,0]],"lfs PR")
        self.prs[1]=self.pr(self.head1)
        self.git("remote","add","local-lfs",str(self.remote))
        self.git("lfs","push","--all","local-lfs",self.target,self.head1)
        self.git("-c","core.hooksPath="+str(self.root/"no-hooks"),"push",str(self.remote),self.target+":refs/heads/lfs-base")
        r=self.review.merge_prepare(["1"])
        self.session=self.review._merges[r["token"]]
        self.publisher=IntegrationPublisher(self.review,self.session);self.publisher.gh_path="gh"
        with patch.object(self.publisher,"gh",side_effect=self.fake_gh),patch.object(self.publisher,"_build",side_effect=self.local_build):
            self.publisher._run()
        self.assertEqual(self.publisher.state["phase"],"complete",self.publisher.state)
        pointer=self.remote_git("show",self.publisher.state["head"]+":"+self.path).decode()
        self.assertTrue(pointer.startswith("version https://git-lfs.github.com/spec/v1\n"))
        oid=pointer.split("oid sha256:")[1].split()[0]
        lfs=self.remote/"lfs"/"objects"/oid[:2]/oid[2:4]/oid
        self.assertEqual(_load_grid_npz(lfs)[0].tolist(),[[1,1,0]])

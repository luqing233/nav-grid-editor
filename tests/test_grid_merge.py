import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np

from nav_grid_editor.services.maps.grid import _load_grid_npz
from nav_grid_editor.services.maps.grid_merge import merge_grid, merge_records, merge_dataset
from nav_grid_editor.services.maps.pr_review import DataPRReview
from nav_grid_editor.services.maps.pr_merge import safe_path
from tests.test_pr_review import grid


class GridMergeTest(unittest.TestCase):
    def test_all_state_combinations_follow_three_way_rules(self):
        for base in range(3):
            for current in range(3):
                for incoming in range(3):
                    with self.subTest(base=base,current=current,incoming=incoming):
                        merged, conflicts = merge_grid(grid([[base]]),grid([[current]]),grid([[incoming]]))
                        expected = incoming if current == base else current
                        self.assertEqual(merged[0][0,0],expected)
                        collision = base != current and base != incoming and current != incoming
                        self.assertEqual(len(conflicts),int(collision))
                        if collision:
                            for choice, value in (("base",base),("current",current),("incoming",incoming)):
                                result, resolved = merge_grid(grid([[base]]),grid([[current]]),grid([[incoming]]),{conflicts[0]["id"]:choice})
                                self.assertEqual(result[0][0,0],value)
                                self.assertEqual(resolved[0]["choice"],choice)

    def test_expansion_origin_shift_erasure_and_unknown_border(self):
        b=grid([[1,0,2,0]])
        m=grid([[1,1,0,2,0]],origin=(-1,0,0))
        p=grid([[0,0,2,0,1]])
        merged, conflicts=merge_grid(b,m,p)
        self.assertFalse(conflicts)
        self.assertEqual(merged[1]["origin"],[-1,0,0])
        self.assertEqual(merged[0].tolist(),[[1,0,0,2,0,1]])
        self.assertEqual(b[0].tolist(),[[1,0,2,0]])

    def test_three_independent_prs_preserve_prior_changes(self):
        base=grid([[0,0,0]])
        current=copy.deepcopy(base)
        for incoming in (grid([[1,0,0]]),grid([[0,2,0]]),grid([[0,0,1]])):
            current, conflicts=merge_grid(base,current,incoming)
            self.assertFalse(conflicts)
        self.assertEqual(current[0].tolist(),[[1,2,1]])

    def test_main_tier_alias_is_not_a_geometry_or_metadata_conflict(self):
        b,m,p=grid([[0,0]]),grid([[1,0]]),grid([[0,2]])
        p[1]["tier"]="__main__"
        merged,conflicts=merge_grid(b,m,p)
        self.assertFalse(conflicts)
        self.assertEqual(merged[1]["tier"],"")
        self.assertEqual(merged[0].tolist(),[[1,2]])

    def test_connected_conflict_regions_and_content_bound_ids(self):
        b=grid([[0,0,0,0]])
        m=grid([[1,1,0,1]])
        p=grid([[2,2,0,2]])
        result, conflicts=merge_grid(b,m,p)
        self.assertEqual([c["count"] for c in conflicts],[2,1])
        result, resolved=merge_grid(b,m,p,{conflicts[0]["id"]:"incoming"})
        self.assertEqual(result[0].tolist(),[[2,2,0,1]])
        self.assertIsNone(resolved[1]["choice"])

    def test_delete_modify_add_add_and_geometry_conflicts(self):
        b,m,p=grid([[0,0]]),grid([[1,0]]),grid([[0,2]])
        result, conflicts=merge_grid(b,m,None)
        self.assertEqual(conflicts[0]["kind"],"file")
        result,_=merge_grid(b,m,None,{conflicts[0]["id"]:"incoming"})
        self.assertIsNone(result)
        self.assertIsNone(merge_grid(b,b,None)[0])
        self.assertEqual(merge_grid(None,m,p)[0][0].tolist(),[[1,2]])
        for incoming in (grid([[2,0]],size=2),grid([[2,0]],origin=(.5,0,0)),grid([[2]])):
            _, conflicts=merge_grid(b,m,incoming)
            self.assertEqual(conflicts[0]["kind"],"file")

    def test_floor_regions_and_missing_metadata_remain_distinct(self):
        b,m,p=grid([[0]]),grid([[1]]),grid([[2]])
        m[1]["floor_regions"]=[dict(x0=0,x1=1,z0=0,z1=1,y_min=1,y_max=2)]
        p[1]["floor_regions"]=[dict(x0=0,x1=1,z0=0,z1=1,y_min=3,y_max=4)]
        m[1]["extra"]=None
        merged, conflicts=merge_grid(b,m,p)
        self.assertEqual([c["kind"] for c in conflicts],["cells","metadata"])
        self.assertIn("extra",merged[1])
        choices={c["id"]:"incoming" for c in conflicts}
        merged,_=merge_grid(b,m,p,choices)
        self.assertEqual(merged[1]["floor_regions"],p[1]["floor_regions"])

    def test_links_merge_by_id_and_delete_modify_is_conflict(self):
        b={"schema_version":3,"links":[{"id":"a","cost":1}]}
        m={"schema_version":3,"links":[{"id":"a","cost":2},{"id":"b","cost":1}]}
        p={"schema_version":3,"links":[{"id":"c","cost":1}]}
        result, conflicts=merge_records(b,m,p,"links")
        self.assertEqual(len(conflicts),1)
        self.assertEqual([i["id"] for i in result["links"]],["a","b","c"])
        result,_=merge_records(b,m,p,"links",{conflicts[0]["id"]:"incoming"})
        self.assertEqual([i["id"] for i in result["links"]],["b","c"])

    def test_new_record_files_merge_and_duplicate_ids_reject(self):
        a={"schema_version":1,"layers":[{"id":"a"}]}
        b={"schema_version":1,"layers":[{"id":"b"}]}
        result, conflicts=merge_records(None,a,b,"layers")
        self.assertFalse(conflicts)
        self.assertEqual(result["layers"],[{"id":"a"},{"id":"b"}])
        with self.assertRaisesRegex(ValueError,"唯一"):
            merge_records(a,a,{**b,"layers":[{"id":"b"},{"id":"b"}]},"layers")

    def test_manifest_hashes_are_derived_not_conflicts(self):
        b=dict(schema_version=1,release_id="a",parent_release_id=None,
               navigation_files=[dict(path="a.grid.npz",role="grid",size=1,sha256="base")],
               coordinate_spaces=[dict(id="world")],registrations=[])
        m,p=copy.deepcopy(b),copy.deepcopy(b)
        m["navigation_files"][0]["sha256"]="main"
        p["navigation_files"][0]["sha256"]="pr"
        p["release_id"]="new"
        result, conflicts=merge_dataset(b,m,p)
        self.assertFalse(conflicts)
        self.assertEqual(result["navigation_files"],[dict(path="a.grid.npz",role="grid")])
        self.assertEqual(result["coordinate_spaces"],[dict(id="world")])


@unittest.skipUnless(shutil.which("git"),"Git is required")
class PRMergeSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.repo=self.root/"repo";self.repo.mkdir()
        self.git("init","-q")
        self.git("config","user.email","test@example.com")
        self.git("config","user.name","Test")
        self.git("remote","add","origin","https://github.com/test/data.git")
        self.path="map02_4.grid.npz"
        self.base=self.commit([[0,0,0]],"base")
        self.target=self.commit([[1,0,0]],"target")
        self.git("checkout","-q","--detach",self.base)
        self.head1=self.commit([[2,1,0]],"pr1")
        self.git("checkout","-q","--detach",self.base)
        self.head2=self.commit([[0,0,2]],"pr2")
        self.git("checkout","-q","--detach",self.target)
        (self.repo/self.path).write_bytes(b"user unsaved grid")
        self.status=self.git("status","--porcelain")
        self.index=self.git("ls-files","--stage")
        self.review=DataPRReview(self.repo,self.root/"output"/"pr-review")
        self.prs={1:self.pr(self.head1),2:self.pr(self.head2)}
        self.patcher=patch.object(self.review,"_github",side_effect=lambda repo,n:self.prs[n])
        self.patcher.start();self.addCleanup(self.patcher.stop)

    def git(self,*args):
        return subprocess.check_output(["git","-C",str(self.repo),*args],stderr=subprocess.PIPE).decode().strip()

    def commit(self,values,message):
        a,m=grid(values)
        np.savez_compressed(self.repo/self.path,cells=a,meta=np.array(json.dumps(m)))
        self.git("add",".");self.git("commit","-qm",message)
        return self.git("rev-parse","HEAD")

    def pr(self,head):
        return {"state":"open","title":"test","base":{"sha":self.target,"ref":"main"},"head":{"sha":head}}

    def test_queue_conflict_resolution_export_and_working_tree_unchanged(self):
        result=self.review.merge_prepare(["1","2"])
        self.assertEqual(result["completed"],[])
        self.assertEqual(result["unresolved"],1)
        token=result["token"]
        with self.assertRaisesRegex(ValueError,"未解决"):
            self.review.merge_action(token,"export")
        with self.assertRaisesRegex(ValueError,"过期"):
            self.review.merge_action(token,"resolve",{"not-a-conflict":"incoming"})
        result=self.review.merge_action(token,"resolve",{result["conflicts"][0]["id"]:"incoming"})
        self.assertTrue(result["ready"])
        self.assertEqual(result["completed"],[1,2])
        self.assertEqual(len(result["files"][0]["changes"]),3)
        self.assertEqual(result["files"][0]["current"]["cells"],[[0,0]])
        self.assertEqual(result["files"][0]["contributors"],[1,2])
        archive=self.review.merge_action(token,"export")
        with zipfile.ZipFile(archive) as z:
            arr,meta,_=_load_grid_npz(io.BytesIO(z.read("files/"+self.path)))
            self.assertEqual(arr.tolist(),[[2,1,2]])
            self.assertEqual(meta["schema_version"],2)
            plan=json.loads(z.read("merge-plan.json"))
            self.assertEqual(plan["target"],self.target)
            self.assertEqual(len(plan["prs"]),2)
        self.assertEqual((self.repo/self.path).read_bytes(),b"user unsaved grid")
        self.assertEqual(self.git("rev-parse","HEAD"),self.target)
        self.assertEqual(self.git("status","--porcelain"),self.status)
        self.assertEqual(self.git("ls-files","--stage"),self.index)
        self.prs[2]["head"]["sha"]=self.head1
        with self.assertRaisesRegex(ValueError,"过期"):
            self.review.merge_action(token,"export")

    def test_stacked_pr_uses_previous_pr_as_ancestor(self):
        self.git("checkout","-q","-f","--detach",self.head1)
        stacked=self.commit([[2,1,1]],"stacked")
        self.prs[2]=self.pr(stacked)
        result=self.review.merge_prepare(["1","2"])
        self.assertEqual(result["prs"][1]["base"],self.head1)
        result=self.review.merge_action(result["token"],"resolve",{result["conflicts"][0]["id"]:"incoming"})
        self.assertEqual(result["files"][0]["result"]["cells"],[[1,0],[2,0]])

    def test_export_rebuilds_manifest_without_mutating_schema(self):
        self.git("checkout","-q","-f","--detach",self.target)
        original={"schema_version":1,"release_id":"old","parent_release_id":None,
                  "navigation_files":[{"path":self.path,"role":"grid","size":0,"sha256":"stale"}],
                  "coordinate_spaces":[{"id":"map02"}],
                  "registrations":[{"coordinate_space_id":"map02","map_name":"map02","tier":"","zoom":"4","grid":self.path,"visual":None}]}
        (self.repo/"dataset.json").write_text(json.dumps(original),encoding="utf-8")
        self.target=self.commit([[1,0,0]],"manifest target")
        self.prs[2]=self.pr(self.head2)
        result=self.review.merge_prepare(["2"])
        archive=self.review.merge_action(result["token"],"export")
        import hashlib
        with zipfile.ZipFile(archive) as z:
            manifest=json.loads(z.read("files/dataset.json"))
            content=z.read("files/"+self.path)
        self.assertEqual(set(manifest),set(original))
        self.assertEqual(manifest["parent_release_id"],"old")
        self.assertEqual(manifest["navigation_files"][0]["sha256"],hashlib.sha256(content).hexdigest())
        self.assertEqual(manifest["navigation_files"][0]["size"],len(content))

    def test_export_checks_link_endpoints_and_does_not_publish_bad_data(self):
        self.git("checkout","-q","-f","--detach",self.target)
        endpoint={"map":"map02","zoom":"4","tier":"","rect":{"x0":100,"x1":101,"z0":100,"z1":101}}
        other=copy.deepcopy(endpoint);other["rect"]["x0"]=102;other["rect"]["x1"]=103
        links={"schema_version":3,"links":[{"id":"bad","from":endpoint,"to":other,"kind":"stairs","cost":1,"bidirectional":True}]}
        (self.repo/"links.json").write_text(json.dumps(links),encoding="utf-8")
        self.target=self.commit([[1,0,0]],"invalid links")
        self.prs[2]=self.pr(self.head2)
        result=self.review.merge_prepare(["2"])
        with self.assertRaisesRegex(ValueError,"端点无效"):
            self.review.merge_action(result["token"],"export")

    def test_closed_pr_and_moved_target_invalidate_export(self):
        result=self.review.merge_prepare(["2"])
        self.prs[2]["base"]["sha"]=self.base
        with self.assertRaisesRegex(ValueError,"过期"):
            self.review.merge_action(result["token"],"export")
        self.prs[2]=self.pr(self.head2);self.prs[2]["state"]="closed"
        with self.assertRaisesRegex(ValueError,"已关闭"):
            self.review.merge_prepare(["2"])

    def test_bad_inputs_and_paths_are_rejected(self):
        for values in ([],["1","1"],[1],"1"):
            with self.assertRaises(ValueError):self.review.merge_prepare(values)
        for path in ("../escape","/absolute","C:/escape",".git/config","foo\\bar"):
            with self.assertRaises(ValueError):safe_path(path)

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
JS=ROOT/"src/nav_grid_editor/web/static/js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for PR review tests")
class PRReviewJsTest(unittest.TestCase):
    def test_stale_mapping_request_cannot_replace_restored_calibration(self):
        script=f"""
import assert from 'node:assert/strict';import fs from 'node:fs';import vm from 'node:vm';
const source=fs.readFileSync({json.dumps((JS/'map_composer.js').as_posix())},'utf8').replace(/\\r\\n/g,'\\n');
const start=source.indexOf('async function loadMappingForCurrent()');
const pending=[],noop=()=>{{}};
const c={{state:{{activeKey:'map02@4'}},calib:{{}},calibHistory:{{undo:[],redo:[]}},lastPos:null,
  $:()=>({{textContent:'',innerHTML:''}}),closeCalibDialog:noop,renderCalibList:noop,renderCalibMarks:noop,updatePlayerMarker:noop,requestRender:noop,updateMarksHint:noop,syncTierControls:noop,addLog:noop,
  fetch:()=>new Promise(resolve=>pending.push(data=>resolve({{json:async()=>data}})))}};
vm.createContext(c);vm.runInContext(source.slice(start,source.indexOf('\\n}}\\n',start)+3),c);
const a=c.loadMappingForCurrent(),b=c.loadMappingForCurrent();
pending[1]({{ok:true,matrix:[[2]]}});await b;pending[0]({{ok:true,matrix:[[1]]}});await a;
assert.equal(c.calib.mapping.matrix[0][0],2);
const old=c.loadMappingForCurrent();c.calib.loadRequestId++;c.calib.mapping={{draft:true}};
pending[2]({{ok:true,matrix:[[0]]}});await old;assert.equal(c.calib.mapping.draft,true);
"""
        result=subprocess.run(["node","--input-type=module","-e",script],cwd=ROOT,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr or result.stdout)

    def test_change_navigation_lookup_and_worker_use_real_walk_policy(self):
        script=f"""
import assert from 'node:assert/strict';
import {{regionsOf,snapshotDocument,transitionAt,TRANSITIONS}} from {json.dumps((JS/'review/model.js').as_uri())};
const file={{origin:[-10,-20],cellSize:2,before:{{cells:[[0,0]],shape:[3,3]}},after:{{cells:[[0,0],[2,2]],shape:[3,3]}},map:'map02',tier:'',regions:[{{count:1,bounds:{{x0:0,x1:1,z0:0,z1:1}}}}]}};
const regions=regionsOf({{files:[file,{{...file,tier:'A',regions:[...file.regions,...file.regions]}}]}});
assert.deepEqual(regions.map(r=>[r.fileIndex,r.regionIndex]),[[0,0],[1,0],[1,1]]);
const doc=snapshotDocument(file,'before');doc.setCell(0,0,2);
assert.deepEqual(file.before.cells,[[0,0]],'snapshot documents must not mutate transport payload');
assert.deepEqual(transitionAt(file,-9,-19,new Map([['0,0',[2,1]]])),[2,1]);
assert.equal(transitionAt(file,0,0,new Map()),null);
assert.equal(Object.keys(TRANSITIONS).length,6);
globalThis.self={{postMessage:data=>globalThis.result=data}};
await import({json.dumps((JS/'review/checks-worker.js').as_uri())});
self.onmessage({{data:[file]}});
assert.equal(result.files[0].before.components.length,1);
assert.equal(result.files[0].after.components.length,2,'diagonal free cells cannot cross unknown corner guards');
"""
        result=subprocess.run(["node","--input-type=module","-e",script],cwd=ROOT,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr or result.stdout)

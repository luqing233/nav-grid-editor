import json
import shutil
import subprocess
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "src/nav_grid_editor/web/static/js/three/perspective-view.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class PerspectiveLifecycleJsTest(unittest.TestCase):
    def run_js(self, script):
        bootstrap = f"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync({json.dumps(MODULE.as_posix())}, 'utf8');
const context = {{qs: () => '', MAIN_TIER: '__main__'}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('export class PerspectiveView')).replace('export class', 'class') + '\\nglobalThis.View = PerspectiveView;', context);
const view = Object.create(context.View.prototype);
const loops = [];
Object.assign(view, {{loadGeneration: 0, plateGeneration: 0, open: false,
  root: {{hidden: true}}, renderer: {{setAnimationLoop: value => loops.push(value)}},
  resize: () => {{}}, setStatus: () => {{}}, clearPlates: () => {{}},
  spacingInput: {{}}, autoSpacing: () => 80, frameScene: () => {{}},
  controls: {{update: () => {{}}}},
}});
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", bootstrap + script],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_close_during_load_cannot_restart_hidden_render_loop(self):
        self.run_js(r"""
let resolve, builds = 0;
view.loadData = () => new Promise(r => { resolve = r; });
view.rebuildPlates = async () => { builds++; };
const loading = view.show({map: 'map02', zoom: '4', tier: ''});
view.close(); resolve(); await loading;
assert.equal(builds, 0);
assert.deepEqual(loops, [null]);
assert.equal(view.root.hidden, true);
""")

    def test_reopening_ignores_previous_load_completion(self):
        self.run_js(r"""
const pending = []; let builds = 0;
view.loadData = () => new Promise(r => pending.push(r));
view.rebuildPlates = async () => { builds++; };
const old = view.show({map: 'base01', zoom: '4', tier: ''});
view.close();
const current = view.show({map: 'map02', zoom: '4', tier: ''});
pending[1](); await current;
pending[0](); await old;
assert.equal(builds, 1);
assert.equal(loops.filter(value => typeof value === 'function').length, 1);
assert.equal(view.map, 'map02');
""")

    def test_old_grid_response_does_not_repopulate_disposed_scene(self):
        self.run_js(r"""
let resolve;
context.fetch = () => new Promise(r => { resolve = r; });
view.open = true; view.plateGeneration = 3;
const pending = view.buildGridFor('__main__', [[0, 0], [10, 10]], 0, 3);
view.plateGeneration = 4;
resolve({json: async () => ({data: {cells: [[0, 0]]}})});
await pending;
// No geometry builders exist in this context: a stale response must stop before them.
""")

    def test_stale_bitmap_is_released_after_close(self):
        self.run_js(r"""
let resolve, released = 0;
context.decodeBitmap = () => new Promise(r => { resolve = r; });
context.affine = (_, x, z) => [x, z];
context.makeTexture = bitmap => bitmap;
context.releaseTexture = () => { released++; };
view.open = true; view.plateGeneration = 1;
view.calib = {image_size: {width: 10, height: 10}, matrix: []};
view.plateYs = new Map();
const pending = view.buildBasePlate(1);
view.close(); resolve({}); await pending;
assert.equal(released, 1);
""")

"""Exercise the real viewport orchestration with a deterministic animation clock."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src/nav_grid_editor/web/static/js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class ViewRenderJsTest(unittest.TestCase):
    def run_js(self, script):
        bootstrap = r'''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { RenderScheduler, RENDER_LAYERS } from 'SCHEDULER_URI';
const source = fs.readFileSync('COMPOSER_PATH', 'utf8');
const callbacks = new Map();
let nextId = 0;
globalThis.requestAnimationFrame = callback => { callbacks.set(++nextId, callback); return nextId; };
globalThis.cancelAnimationFrame = id => callbacks.delete(id);
const draws = [], chrome = [];
const c = {
  requestAnimationFrame, cancelAnimationFrame, RENDER_LAYERS,
  mapView: {scale: 1, panX: 10, panY: 20, focus: null},
  isPanning: false, viewFrame: 0, viewFrameNeedsRender: false,
  deferredRenderLayers: 0, renderOverscan: 0, DRAG_OVERSCAN: 0.36,
  applyViewChrome: () => chrome.push({...c.mapView}),
  render: layers => draws.push(layers), syncTierResolution: () => {},
  clampPan: () => {}, showMarkTip: () => {},
};
c.renderScheduler = new RenderScheduler(c.render);
function part(start, end) {
  const a=source.indexOf(start), b=source.indexOf(end,a);
  assert(a>=0 && b>a, 'viewport function boundaries');
  return source.slice(a,b);
}
vm.createContext(c);
vm.runInContext(
  part('function requestRender(', '// 可见区域') +
  part('function applyView(renderNow', 'function currentViewportRect()') +
  part('function activatePanBuffer()', '// 地图小于窗口') +
  part('function zoomAt(', 'function clearView('), c);
function tick() {
  const batch=[...callbacks];
  for(const [id,callback] of batch) {
    if(!callbacks.has(id))continue;
    callbacks.delete(id);callback();
  }
}
'''
        bootstrap = bootstrap.replace("SCHEDULER_URI", (JS / "render/render-scheduler.js").as_uri())
        bootstrap = bootstrap.replace("COMPOSER_PATH", (JS / "map_composer.js").as_posix())
        result = subprocess.run(["node", "--input-type=module", "-e", bootstrap + script],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_zoom_burst_replaces_pending_partial_draw_and_keeps_anchor(self):
        self.run_js(r'''
c.requestRender(RENDER_LAYERS.GRID);
const anchor = {x:(300-c.mapView.panX)/c.mapView.scale,y:(200-c.mapView.panY)/c.mapView.scale};
for(let i=0;i<40;i++)c.zoomAt(300,200,i%2 ? 1/1.15 : 1.15);
assert.equal(draws.length,0,'input handlers must not draw synchronously');
assert.equal(callbacks.size,1,'partial frame is superseded by full viewport draw');
tick();
assert.deepEqual(draws,[RENDER_LAYERS.ALL]);
assert.equal(chrome.length,1);
assert.ok(Math.abs((300-c.mapView.panX)/c.mapView.scale-anchor.x)<1e-9);
assert.ok(Math.abs((200-c.mapView.panY)/c.mapView.scale-anchor.y)<1e-9);
tick();assert.equal(draws.length,1,'no redundant frame remains');
''')

    def test_pan_burst_and_finish_do_not_leave_a_stale_frame(self):
        self.run_js(r'''
c.activatePanBuffer();
for(let i=0;i<40;i++) {c.mapView.panX=i;c.scheduleViewFrame(i%5===0);}
c.requestRender(RENDER_LAYERS.BASE);
assert.equal(draws.length,0);
tick();
assert.deepEqual(draws,[RENDER_LAYERS.ALL]);
assert.equal(chrome[0].panX,39);
c.scheduleViewFrame(true);
c.finishPanGesture();
assert.equal(c.isPanning,false);
assert.equal(c.renderOverscan,0);
assert.equal(draws.length,2,'pointerup draws final viewport once');
tick();assert.equal(draws.length,2);
''')

    def test_buffered_pan_updates_transform_without_rasterizing(self):
        self.run_js(r'''
c.isPanning=true;
for(let i=0;i<40;i++) {c.mapView.panX=i;c.scheduleViewFrame(false);}
tick();
assert.equal(chrome.length,1);assert.equal(chrome[0].panX,39);
assert.equal(draws.length,0,'moving inside cached buffer needs no raster draw');
''')

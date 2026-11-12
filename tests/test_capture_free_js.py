import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/nav_grid_editor/web/static/js/grid/capture-free.js"
GRID_DOCUMENT = ROOT / "src/nav_grid_editor/web/static/js/grid/grid-document.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class CaptureFreeJsTest(unittest.TestCase):
    def run_js(self, script):
        bootstrap = (
            f'import {{ paintCaptureFreeSegment, CAPTURE_FREE_MAX_SPEED, '
            f'CAPTURE_FREE_MAX_GAP_METERS }} from {MODULE.as_uri()!r};\n'
            f'import {{ GridDocument, CELL_FREE, CELL_BLOCKED, CELL_UNKNOWN }} '
            f'from {GRID_DOCUMENT.as_uri()!r};\n'
            r'''
import assert from "node:assert/strict";
const document = (cells = []) => {
  const doc = new GridDocument({origin: [0, 0, 0], shape: [24, 24], cellSize: 1});
  for (const [x, z, state] of cells) doc.setCell(x, z, state);
  return doc;
};
'''
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", bootstrap + script],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def run_composer_js(self, script):
        bootstrap = r'''
import fs from "node:fs";
import vm from "node:vm";
import { sameTier } from "TIER_URI";
const source = fs.readFileSync("COMPOSER_PATH", "utf8");
const part = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a);
  return source.slice(a, b);
};
const noop = () => {};
const elements = new Map([
  ["captureFreeEnabled", {checked: true}], ["captureFreeMode", {value: "auto"}],
  ["captureFreeRadius", {value: "0"}], ["captureFreeTarget", {}], ["captureFreeIndicator", {}],
]);
const doc = document(); doc.sourceTier = "new-floor"; doc.loaded = true;
const view = {edit: doc};
const c = {
  state: {activeKey: "map02@4", views: new Map([["map02@4", view]]),
    positionView: {requestId: 0, appliedKey: "old"}},
  edit: {mode: true, v: view}, tier: "new-floor",
  $: id => elements.get(id), ed: () => c.edit.v.edit, currentTierId: () => c.tier,
  currentTierCatalog: () => [{id: "new-floor", level_name: "区域", name: "新分层"}],
  mapDisplayName: () => "武陵", sameTier, paintCaptureFreeSegment, CAPTURE_FREE_MAX_SPEED,
  beginEditTransaction: e => e.beginTransaction(), commitEditTransaction: e => e.commitTransaction(),
  setGridDirty: noop, updateEditStats: noop, requestRender: noop,
  scheduleFloorOverlapLiveRefresh: noop, RENDER_LAYERS: {GRID: 1},
};
vm.createContext(c);
vm.runInContext(
  part("function manualCaptureFreeActive()", "// ---------------- 2D 网格编辑") +
  part("async function syncPositionLocation(data)", "async function loadPositionStatus()") +
  part("function resetCaptureFreeCollection()", '$("captureFreeEnabled").addEventListener'), c);
const sample = (x = 0.5, extra = {}) => ({mapId: "map02", gridTierId: "", speed: 1,
  pos: {x, y: 100, z: 0.5}, ...extra});
'''
        html = (ROOT / "src/nav_grid_editor/web/map_composer.html").read_text(encoding="utf-8")
        self.assertIn('id="captureFreeMode"', html)
        self.assertIn('id="captureFreeTarget"', html)
        js = ROOT / "src/nav_grid_editor/web/static/js"
        bootstrap = bootstrap.replace("TIER_URI", (js / "grid/tier-id.js").as_uri())
        bootstrap = bootstrap.replace("COMPOSER_PATH", (js / "map_composer.js").as_posix())
        self.run_js(bootstrap + script)

    def test_manual_collects_unannotated_floor_and_auto_still_requires_match(self):
        self.run_composer_js(r'''
c.markCapturePositionFree(sample());
assert.equal(doc.getCell(0, 0), CELL_UNKNOWN);
elements.get("captureFreeMode").value = "manual";
c.resetCaptureFreeCollection();
c.markCapturePositionFree(sample());
assert.equal(doc.getCell(0, 0), CELL_FREE);
assert.equal(doc.floorRegions.length, 0);
assert.match(elements.get("captureFreeTarget").textContent, /当前写入：武陵 \/ 区域 · 新分层（手动选层）/);
assert.match(elements.get("captureFreeIndicator").textContent, /新分层/);
elements.get("captureFreeMode").value = "auto";
c.resetCaptureFreeCollection();
c.markCapturePositionFree(sample(1.5, {gridTierId: "new-floor"}));
assert.equal(doc.getCell(1, 0), CELL_FREE);
''')

    def test_manual_rejects_other_map_high_speed_and_stale_document(self):
        self.run_composer_js(r'''
elements.get("captureFreeMode").value = "manual";
for (const extra of [{mapId: "map01"}, {speed: 8}, {speed: null}]) {
  c.markCapturePositionFree(sample(0.5, extra));
  assert.equal(doc.freeCount, 0);
}
c.tier = "other-floor";
c.markCapturePositionFree(sample());
assert.equal(doc.freeCount, 0);
assert.match(elements.get("captureFreeTarget").textContent, /暂停/);
c.tier = "new-floor";
c.state.tierLoading = true;
c.markCapturePositionFree(sample());
assert.equal(doc.freeCount, 0);
c.state.tierLoading = false;
c.state.prReviewActive = true;
c.markCapturePositionFree(sample());
assert.equal(doc.freeCount, 0);
''')

    def test_switching_floor_or_mode_never_bridges_trajectories(self):
        self.run_composer_js(r'''
elements.get("captureFreeMode").value = "manual";
c.markCapturePositionFree(sample());
const next = document(); next.sourceTier = "other-floor"; next.loaded = true;
c.tier = "other-floor"; view.edit = next;
c.markCapturePositionFree(sample(5.5));
assert.equal(next.getCell(0, 0), CELL_UNKNOWN);
assert.equal(next.getCell(5, 0), CELL_FREE);
assert.equal(doc.getCell(5, 0), CELL_UNKNOWN);
c.resetCaptureFreeCollection();
c.markCapturePositionFree(sample(7.5));
assert.equal(next.getCell(6, 0), CELL_UNKNOWN);
''')

    def test_manual_pauses_auto_switching_only_while_collecting(self):
        self.run_composer_js(r'''
elements.get("captureFreeMode").value = "manual";
await c.syncPositionLocation(sample()); // 自动切图路径未运行，无需地图加载替身
assert.equal(c.state.positionView.appliedKey, "old");
c.resetCaptureFreeCollection();
assert.equal(c.state.positionView.appliedKey, "");
assert.equal(c.state.positionView.requestId, 1);
assert.equal(c.manualCaptureFreeActive(), true);
elements.get("captureFreeEnabled").checked = false;
assert.equal(c.manualCaptureFreeActive(), false);
elements.get("captureFreeEnabled").checked = true;
c.edit.mode = false;
assert.equal(c.manualCaptureFreeActive(), false);
''')

    def test_enabling_manual_discards_pending_auto_tier_image(self):
        self.run_composer_js(r'''
let respond, closed = false;
Object.assign(c, {
  mapView: {}, calib: {mapping: true, key: "map02@4"},
  state: {...c.state, tierRequestId: 0},
  syncTierControls: () => c.updateCaptureFreeTarget(), setStatus: noop,
  populateTierSelect: () => c.updateCaptureFreeTarget(),
  fetch: () => new Promise(resolve => {respond = resolve;}),
  createImageBitmap: async () => ({close: () => {closed = true;}}),
});
vm.runInContext(part("async function setActiveTier(", "async function loadTileMaps()"), c);
c.tier = "";
const request = c.state.positionView.requestId;
const pending = c.setActiveTier("new-floor", {canApply: () => request === c.state.positionView.requestId});
elements.get("captureFreeMode").value = "manual";
c.resetCaptureFreeCollection();
respond({ok: true, blob: async () => ({})});
await pending;
assert.equal(closed, true);
assert.equal(c.state.activeTier, undefined);
assert.equal(c.state.tierLoading, false);
''')

    def test_seven_metres_is_inclusive_and_fills_only_unknown_cells(self):
        self.run_js(r'''
assert.equal(CAPTURE_FREE_MAX_SPEED, 7.5);
assert.equal(CAPTURE_FREE_MAX_GAP_METERS, 7);
const doc = document([[4, 0, CELL_BLOCKED]]);
const result = paintCaptureFreeSegment(doc, {x: 0.5, z: 0.5}, {x: 7.5, z: 0.5}, 0);
assert.equal(result.connected, true);
assert.equal(result.changed, true);
assert.equal(result.distance, 7);
for (let x = 0; x <= 7; x++) {
  assert.equal(doc.getCell(x, 0), x === 4 ? CELL_BLOCKED : CELL_FREE, `cell ${x}`);
}
''')

    def test_more_than_seven_metres_does_not_bridge(self):
        self.run_js(r'''
const doc = document();
const result = paintCaptureFreeSegment(doc, {x: 0.5, z: 0.5}, {x: 7.5001, z: 0.5}, 0);
assert.equal(result.connected, false);
assert.equal(result.changed, false);
assert.equal(doc.getCell(0, 0), CELL_UNKNOWN);
assert.equal(doc.getCell(7, 0), CELL_UNKNOWN);
''')

    def test_radius_and_diagonal_path_leave_no_orthogonal_gap(self):
        self.run_js(r'''
const doc = document();
const result = paintCaptureFreeSegment(doc, {x: 0.5, z: 0.5}, {x: 2.5, z: 2.5}, 0);
assert.equal(result.connected, true);
assert.equal(doc.getCell(0, 0), CELL_FREE);
assert.equal(doc.getCell(1, 0), CELL_FREE);
assert.equal(doc.getCell(1, 1), CELL_FREE);
assert.equal(doc.getCell(2, 1), CELL_FREE);
assert.equal(doc.getCell(2, 2), CELL_FREE);

const wide = document();
const area = paintCaptureFreeSegment(wide, {x: 5.5, z: 5.5}, {x: 5.5, z: 5.5}, 1);
assert.equal(area.cells, 9);
for (let z = 4; z <= 6; z++) {
  for (let x = 4; x <= 6; x++) assert.equal(wide.getCell(x, z), CELL_FREE);
}
''')


if __name__ == "__main__":
    unittest.main()

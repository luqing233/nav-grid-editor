# -*- coding: utf-8 -*-
"""页面网格编排的乱序响应回归：真实模型/传输 ESM，最小 DOM 与网络替身。"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "src/nav_grid_editor/web/static/js"

PRELUDE = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { GridDocument, CELL_FREE } from "DOCUMENT_URI";
import { loadGridDocument, saveGridDocument } from "TRANSPORT_URI";
import { sameTier } from "TIER_URI";
const source = fs.readFileSync("COMPOSER_PATH", "utf8");
function part(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a);
  assert(a >= 0 && b > a, "页面函数边界必须存在");
  return source.slice(a, b);
}
function harness() {
  const pending = [];
  globalThis.fetch = (url, options) => new Promise(resolve => {
    pending.push({ url, body: options?.body && JSON.parse(options.body),
      respond: result => resolve({ json: async () => result }) });
  });
  const noop = () => {};
  const elements = new Map();
  const doc = GridDocument.fromSparse({origin: [0, 0], shape: [2, 2], cells: [[0, 0]]});
  doc.loaded = true;
  const view = { edit: doc };
  const c = {
    GridDocument, loadGridDocument, saveGridDocument, sameTier,
    state: { activeKey: "map02@4", views: new Map([["map02@4", view]]) },
    tier: "", edit: { v: view, mode: false, dirty: false }, link: {mode: false},
    currentTierId: () => c.tier, ed: () => c.edit.v?.edit,
    $: id => {
      if (!elements.has(id)) elements.set(id, {dataset: {}, innerHTML: "", textContent: ""});
      return elements.get(id);
    },
    gridRasterCache: { clear: noop }, invalidateGridLayer: noop,
    updateEditStats: noop, updateSelBar: noop, syncEditCanvas: noop,
    scheduleFloorOverlapLiveRefresh: noop, loadFloorRegionMap: async () => {},
    loadFloorOverlaps: async () => {}, unmarkedYellowComponents: () => [],
    queueDraftSave: noop,
    setStatus: noop, syncHistoryButtons: noop, diagnoseGrid: async () => "",
    connectivityWarning: () => "", addLog: noop, requestRender: noop,
    loadNavReadiness: noop, RENDER_LAYERS: {GRID: 1, SELECTION: 2},
  };
  vm.createContext(c);
  vm.runInContext(
    part("function setGridDirty(value)", "function syncHistoryButtons()") +
    part("function adoptViewEdit(v)", "function setEditMode(on)") +
    part("async function saveGrid2D()", "// ---------------- 路径标记"), c);
  return { c, pending, doc, view, elements };
}
const loaded = tier => ({data: {origin: [0, 0, 0], shape: [2, 2], cell_size: 1,
  tier, cells: [[0, 0]], blocked: []}});
const saved = {ok: true, path: "grid.npz", origin: [-1, 0, -1], shape: [3, 3], shift: [-1, -1]};
async function posted(pending) {
  for (let i = 0; i < 20 && !pending.length; i++) await Promise.resolve();
  assert.equal(pending.length, 1, "请求必须已发出");
}
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class GridAsyncJsTest(unittest.TestCase):
    def test_dirty_tier_document_survives_round_trip(self):
        self.run_js(r"""
const {c,pending,view}=harness();
c.tier="A";const a=c.loadGrid2D(true);pending[0].respond(loaded("A"));await a;
const original=view.edit;
original.beginTransaction();original.setCell(1,1,CELL_FREE);original.commitTransaction();c.setGridDirty(true);
c.tier="B";const b=c.loadGrid2D(true);pending[1].respond(loaded("B"));await b;
c.tier="A";await c.loadGrid2D(true);
assert.equal(pending.length,2);
assert.equal(view.edit,original);
assert.equal(c.edit.dirty,true);
assert.equal(original.history.undo.length,1);
""")

    def run_js(self, script):
        prelude = PRELUDE.replace("DOCUMENT_URI", (JS / "grid/grid-document.js").as_uri())
        prelude = prelude.replace("TRANSPORT_URI", (JS / "grid/grid-transport.js").as_uri())
        prelude = prelude.replace("TIER_URI", (JS / "grid/tier-id.js").as_uri())
        prelude = prelude.replace("COMPOSER_PATH", (JS / "map_composer.js").as_posix())
        result = subprocess.run(
            ["node", "--input-type=module", "-e", prelude + script],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_old_tier_response_cannot_replace_new_tier(self):
        self.run_js(r"""
const {c, pending, view} = harness();
c.tier = "A"; const a = c.loadGrid2D(true);
c.tier = "B"; const b = c.loadGrid2D(true);
pending[1].respond(loaded("B")); await b;
const bDoc = view.edit;
pending[0].respond(loaded("A")); await a;
assert.equal(view.edit, bDoc);
assert.equal(view.edit.sourceTier, "B");
""")

    def test_same_tier_reload_uses_latest_request(self):
        self.run_js(r"""
const {c, pending, view} = harness();
const a = c.loadGrid2D(true), b = c.loadGrid2D(true);
pending[1].respond(loaded("")); await b;
const latest = view.edit;
pending[0].respond(loaded("")); await a;
assert.equal(view.edit, latest);
""")

    def test_response_after_map_switch_is_ignored(self):
        self.run_js(r"""
const {c, pending, doc, view} = harness();
const a = c.loadGrid2D(true);
c.state.activeKey = "map01@4";
pending[0].respond(loaded("")); await a;
assert.equal(view.edit, doc);
""")

    def test_loading_does_not_discard_new_edits_or_failed_reload_history(self):
        self.run_js(r"""
for (const failure of [false, true]) {
  const {c, pending, doc, view} = harness();
  const a = c.loadGrid2D(true);
  doc.beginTransaction(); doc.setCell(1, 1, CELL_FREE); doc.commitTransaction();
  c.setGridDirty(true);
  pending[0].respond(failure ? {error: "读取失败"} : loaded("")); await a;
  assert.equal(view.edit, doc);
  assert.equal(c.edit.dirty, true);
  assert.equal(doc.history.undo.length, 1);
}
const {c, pending, doc} = harness();
c.setGridDirty(true);
doc.beginTransaction(); doc.setCell(1, 1, CELL_FREE); doc.commitTransaction();
const a = c.loadGrid2D(true);
assert.equal(pending.length, 0, "未保存文档禁止强制重载"); await a;
assert.equal(c.edit.dirty, true);
assert.equal(doc.history.undo.length, 1);
""")

    def test_save_keeps_edits_history_and_coordinate_frame_until_next_save(self):
        self.run_js(r"""
const {c, pending, doc} = harness();
c.setGridDirty(true);
const saving = c.saveGrid2D(); await posted(pending);
assert.equal(pending[0].body.data.cells.length, 1);
doc.beginTransaction(); doc.setCell(1, 1, CELL_FREE); doc.commitTransaction();
c.setGridDirty(true);
pending[0].respond(saved); await saving;
assert.equal(c.edit.dirty, true);
assert.equal(doc.unsaved, true);
assert.equal(doc.history.undo.length, 1);
assert.deepEqual(doc.origin, [0, 0]);
assert.equal(doc.getCell(1, 1), CELL_FREE);
assert(doc.undo()); assert.equal(doc.getCell(1, 1), 0);
assert(doc.redo()); assert.equal(doc.getCell(1, 1), CELL_FREE);
pending.length = 0;
const again = c.saveGrid2D(); await posted(pending);
assert.equal(pending[0].body.data.cells.length, 2);
pending[0].respond(saved); await again;
assert.equal(c.edit.dirty, false);
assert.equal(doc.history.undo.length, 1);
assert(doc.undo()); assert.equal(doc.getCell(2, 2), 0);
assert(doc.redo());
assert.deepEqual(Array.from(doc.origin), [-1, -1]);
assert.equal(doc.getCell(2, 2), CELL_FREE);
""")

    def test_save_keeps_metadata_edits_and_serializes_duplicate_saves(self):
        self.run_js(r"""
const {c, pending, doc} = harness();
const a = c.saveGrid2D(); await posted(pending);
await c.saveGrid2D(); assert.equal(pending.length, 1);
doc.origin[0] = 50; c.setGridDirty(true);
pending[0].respond(saved); await a;
assert.equal(c.edit.dirty, true);
assert.equal(doc.origin[0], 50);
assert.equal(doc.saving, false);
""")

    def test_old_save_response_does_not_clear_new_views_dirty_state(self):
        self.run_js(r"""
const {c, pending, doc} = harness();
const a = c.saveGrid2D(); await posted(pending);
const other = new GridDocument();
c.state.activeKey = "map01@4"; c.edit.v = {edit: other}; c.setGridDirty(true);
pending[0].respond(saved); await a;
assert.equal(doc.unsaved, false);
assert.equal(other.unsaved, true);
assert.equal(c.edit.dirty, true);
""")

    def test_switch_during_preflight_does_not_send_wrong_target(self):
        self.run_js(r"""
const {c, pending} = harness();
let release;
c.loadFloorRegionMap = () => new Promise(r => release = r);
const a = c.saveGrid2D();
c.tier = "other"; release(); await a;
assert.equal(pending.length, 0);
assert.equal(c.ed().saving, false);
""")

    def test_switch_to_another_tier_while_unchanged_save_is_pending(self):
        self.run_js(r"""
const {c, pending, view} = harness();
const saving = c.saveGrid2D(); await posted(pending);
c.tier = "B";
const loading = c.loadGrid2D(true);
assert.equal(pending.length, 2);
pending[1].respond(loaded("B")); await loading;
const bDoc = view.edit;
c.setGridDirty(true);
pending[0].respond(saved); await saving;
assert.equal(view.edit, bDoc);
assert.equal(view.edit.sourceTier, "B");
assert.equal(c.edit.dirty, true);
""")

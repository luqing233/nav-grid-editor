"""The export UI must reflect a server-owned job and preserve unsaved edits."""

import shutil
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "src/nav_grid_editor/web/static/js/dataset-export.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class DatasetExportJsTest(unittest.TestCase):
    def run_js(self, script):
        bootstrap = f'import {{ initDatasetExport }} from {MODULE.as_uri()!r};\n' + r'''
import assert from "node:assert/strict";
const elements = new Map();
globalThis.document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {value: "", textContent: "", disabled: false, focus() {}});
  return elements.get(id);
}};
globalThis.localStorage = {getItem: () => "", setItem() {}};
const timers = [];
globalThis.setTimeout = callback => {timers.push(callback); return timers.length;};
globalThis.clearTimeout = () => {};
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const result = data => ({ok: true, json: async () => data});
'''
        completed = subprocess.run(["node", "--input-type=module", "-e", bootstrap + script],
                                   capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_unsaved_edits_block_export_and_running_job_disables_button(self):
        self.run_js(r'''
let calls = [], dirty = true;
const replies = [result({running: false}), result({ok: true}),
  result({running: true, phase: "彩色 WebP Q90", done: 2, total: 72}),
  result({running: false, phase: "导出完成", output: "E:/数据", summary: {images: 72, grids: 6}})];
globalThis.fetch = async (url, options) => {calls.push({url, options}); return replies.shift();};
const ui = initDatasetExport({hasUnsaved: () => dirty});
await tick();
const button = elements.get("btnDatasetExport"), target = elements.get("datasetTarget");
await button.onclick();
assert.equal(calls.length, 1);
assert.match(elements.get("datasetExportStatus").textContent, /先保存/);
dirty = false; target.value = " E:/数据 ";
await button.onclick();
assert.equal(JSON.parse(calls[1].options.body).target, "E:/数据");
assert.equal(button.disabled, true);
await button.onclick(); assert.equal(calls.length, 3);
await ui.poll();
assert.equal(button.disabled, false);
assert.match(elements.get("datasetExportStatus").textContent, /72 张彩色 WebP.*6 份网格/);
ui.stop();
''')

    def test_reloaded_page_recovers_job_and_failure_explains_reason(self):
        self.run_js(r'''
const replies = [result({running: true, phase: "冻结源文件", done: 1, total: 10}),
 result({running: false, phase: "导出失败", error: "源数据变化"})];
globalThis.fetch = async () => replies.shift();
const ui = initDatasetExport(); await tick();
assert.equal(elements.get("btnDatasetExport").disabled, true);
await ui.poll();
assert.equal(elements.get("btnDatasetExport").disabled, false);
assert.match(elements.get("datasetExportStatus").textContent, /源数据变化/);
ui.stop();
''')

    def test_v3_selection_reaches_server_and_uses_tile_summary(self):
        self.run_js(r'''
const replies = [result({running: false}), result({ok: true}),
  result({running: false, phase: "导出完成", summary: {format: "tiled-v3", tiles: 10383, grids: 6}})];
let request;
globalThis.fetch = async (url, options) => { if (options) request = JSON.parse(options.body); return replies.shift(); };
const ui = initDatasetExport(); await tick();
elements.get("datasetFormat").value = "tiled-v3";
await elements.get("btnDatasetExport").onclick();
assert.equal(request.format, "tiled-v3");
assert.match(elements.get("datasetExportStatus").textContent, /定位瓦片 v3.*10383 格.*6 份网格/);
ui.stop();
''')

    def test_explicit_bad_target_error_is_not_overwritten_by_poll(self):
        self.run_js(r'''
let calls = 0;
globalThis.fetch = async () => ++calls === 1 ? result({running: false, phase: "未导出"}) :
  {ok: false, status: 400, json: async () => ({ok: false, error: "请选择新目录或空目录"})};
initDatasetExport(); await tick();
await elements.get("btnDatasetExport").onclick();
assert.equal(calls, 2);
assert.equal(timers.length, 0);
assert.match(elements.get("datasetExportStatus").textContent, /新目录或空目录/);
assert.equal(elements.get("btnDatasetExport").disabled, false);
''')

import shutil
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "src/nav_grid_editor/web/static/js/data-sync.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class DataSyncJsTest(unittest.TestCase):
    def run_js(self, script):
        bootstrap = f'import {{ initDataSync }} from {MODULE.as_uri()!r};\n' + r'''
import assert from "node:assert/strict";
const elements = new Map();
globalThis.document = {getElementById(id) {
  if (!elements.has(id)) elements.set(id, {disabled: false, textContent: "同步仓库"});
  return elements.get(id);
}};
const result = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
'''
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", bootstrap + script],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_sync_requires_confirmation_and_posts_destructive_consent(self):
        self.run_js(r'''
let confirmed = false, calls = [], synced = null, status = null;
globalThis.fetch = async (url, options) => {
  calls.push({url, options});
  return result({ok: true, current: "0123456789abcdef"});
};
const ui = initDataSync({
  hasUnsaved: () => true,
  unsavedNames: () => ["map02@4/主图"],
  confirmSync: message => { assert.match(message, /未保存网格/); return confirmed; },
  setStatus: (text, state) => { status = {text, state}; },
  onSynced: data => { synced = data; },
});
assert.equal(await ui.sync(), false);
assert.equal(calls.length, 0);
confirmed = true;
assert.equal(await ui.sync(), true);
assert.equal(calls[0].url, "/api/data-repository/sync");
assert.equal(JSON.parse(calls[0].options.body).replace_local, true);
assert.equal(synced.current, "0123456789abcdef");
assert.match(status.text, /01234567.*重新载入/);
assert.equal(status.state, "ready");
assert.equal(elements.get("btnRepoSync").disabled, false);
assert.equal(elements.get("btnRepoSync").textContent, "同步仓库");
''')

    def test_failure_is_visible_and_button_recovers(self):
        self.run_js(r'''
let status = null;
globalThis.fetch = async () => result({ok: false, error: "Git LFS 实体未完整下载"}, 409);
const ui = initDataSync({
  confirmSync: () => true,
  setStatus: (text, state) => { status = {text, state}; },
});
assert.equal(await ui.sync(), false);
assert.match(status.text, /Git LFS.*未完整下载/);
assert.equal(status.state, "warning");
assert.equal(elements.get("btnRepoSync").disabled, false);
assert.equal(elements.get("btnRepoSync").textContent, "同步仓库");
''')

    def test_async_confirmation_blocks_requests_and_duplicate_prompts(self):
        self.run_js(r'''
let decide, prompts = 0, requests = 0;
globalThis.fetch = async () => { requests++; return result({ok: true}); };
const ui = initDataSync({confirmSync: () => {
  prompts++;
  return new Promise(resolve => { decide = resolve; });
}});
const cancelled = ui.sync();
assert.equal(await ui.sync(), false);
assert.equal(prompts, 1);
assert.equal(requests, 0);
decide(false);
assert.equal(await cancelled, false);
assert.equal(requests, 0);
const accepted = ui.sync();
assert.equal(requests, 0);
decide(true);
assert.equal(await accepted, true);
assert.equal(requests, 1);
''')

    def test_rejected_confirmation_recovers_without_synchronizing(self):
        self.run_js(r'''
let prompts = 0, status = "";
globalThis.fetch = async () => { throw new Error("must not fetch"); };
const ui = initDataSync({
  confirmSync: async () => { prompts++; throw new Error("dialog unavailable"); },
  setStatus: text => { status = text; },
});
assert.equal(await ui.sync(), false);
assert.match(status, /dialog unavailable/);
assert.equal(await ui.sync(), false);
assert.equal(prompts, 2);
assert.equal(elements.get("btnRepoSync").disabled, false);
''')


if __name__ == "__main__":
    unittest.main()

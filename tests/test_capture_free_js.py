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

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/nav_grid_editor/web/static/js/player-follow.js"
COMPOSER = ROOT / "src/nav_grid_editor/web/static/js/map_composer.js"


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class PlayerFollowJsTest(unittest.TestCase):
    def run_js(self, script):
        bootstrap = (
            f'import {{ playerMapPixel, centeredPlayerPan, playerPositionMoved }} '
            f'from {MODULE.as_uri()!r};\n'
            r'''
import assert from "node:assert/strict";
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);
'''
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", bootstrap + script],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_world_position_maps_to_pixel_and_pan_centers_it(self):
        self.run_js(r'''
const mapping = {inverse_matrix: [[2, 0, 10], [0, 3, 20]]};
assert.deepEqual(playerMapPixel({x: 4, z: 5}, mapping), [18, 35]);
const pan = centeredPlayerPan([18, 35], 800, 600, 2);
assert.deepEqual(pan, {x: 364, y: 230});
close(18 * 2 + pan.x, 400);
close(35 * 2 + pan.y, 300);
assert.equal(playerMapPixel({x: NaN, z: 1}, mapping), null);
assert.equal(centeredPlayerPan([1, 2], 0, 600, 1), null);
''')

    def test_only_x_or_z_movement_recenters(self):
        self.run_js(r'''
const base = {x: 10, z: 20, y: 30};
assert.equal(playerPositionMoved(base, {x: 10, z: 20, y: 99}), false);
assert.equal(playerPositionMoved(base, {x: 10.0001, z: 20, y: 30}), true);
assert.equal(playerPositionMoved(base, {x: 10, z: 20.0001, y: 30}), true);
assert.equal(playerPositionMoved(null, base), true);
''')


class PlayerFollowSourceTest(unittest.TestCase):
    def test_coordinate_events_request_follow_centering(self):
        source = COMPOSER.read_text(encoding="utf-8")
        self.assertIn("if (center && state.follow && (moved || forceCenter))", source)
        self.assertIn(
            "updatePlayerMarker({ x: pos.x, y: pos.y, z: pos.z }, { center: true })",
            source,
        )
        self.assertIn(
            "updatePlayerMarker(data.pos || data, { center: true, forceCenter: true })",
            source,
        )
        self.assertIn("clampPan(true)", source)


if __name__ == "__main__":
    unittest.main()

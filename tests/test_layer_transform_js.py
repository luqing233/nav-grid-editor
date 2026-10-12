# -*- coding: utf-8 -*-
"""Tier-image to base-map transform regression tests."""

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = (
    ROOT / "src" / "nav_grid_editor" / "web" / "static" / "js" /
    "layers" / "tier-transform.js"
)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class TierTransformJsTest(unittest.TestCase):
    def test_bound_transform_flips_vertical_axis(self):
        script = f"""
import {{ tierToBaseMatrix }} from {MODULE.as_uri()!r};
const m = tierToBaseMatrix({{
  width: 100, height: 200, bounds: {{ west: 0, east: 100, south: 0, north: 200 }},
}}, [[1, 0, 0], [0, 1, 0]]);
const expected = [1, 0, 0, -1, 0, 200];
if (m.some((v, i) => Math.abs(v - expected[i]) > 1e-9)) {{
  throw new Error("unexpected matrix: " + JSON.stringify(m));
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_map02_official_bounds_land_on_base_map_pixels(self):
        script = f"""
import {{ tierToBaseMatrix }} from {MODULE.as_uri()!r};
const inverse = [
  [4.00026845872982, 0.0007771718169132002, 8192.873040474118],
  [0.0004214943445035852, -3.9998219337473473, 7169.102736429521],
];
const m = tierToBaseMatrix({{
  width: 5400, height: 6600,
  bounds: {{ west: -1664, east: -512, south: -1024, north: 384 }},
}}, inverse);
function point(u, v) {{
  return [m[0] * u + m[2] * v + m[4], m[1] * u + m[3] * v + m[5]];
}}
const topLeft = point(0, 0);
const bottomRight = point(5400, 6600);
const near = (a, b) => Math.abs(a - b) < 0.01;
if (!near(topLeft[0], 1536.7247591253936) ||
    !near(topLeft[1], 5632.469747281286) ||
    !near(bottomRight[0], 6143.939765663932) ||
    !near(bottomRight[1], 11264.70459148242)) {{
  throw new Error("unexpected corners: " + JSON.stringify([topLeft, bottomRight]));
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_layer_entrance_anchor_selects_its_group(self):
        script = f"""
import {{ pickLayerGroup }} from {MODULE.as_uri()!r};
const layers = [
  {{ id: "a", group: "储备站", entrance: {{ x: -800, z: -742, radius: 300 }} }},
  {{ id: "b", group: "储备站", entrance: {{ x: -800, z: -742, radius: 300 }} }},
  {{ id: "c", group: "天井院", entrance: {{ x: -819, z: -527, radius: 300 }} }},
];
const picked = pickLayerGroup(layers, -800, -536);
if (!picked || picked.group !== "天井院" || picked.layers.length !== 1) {{
  throw new Error("wrong group: " + JSON.stringify(picked));
}}
if (pickLayerGroup(layers, -1473, -1461) !== null) {{
  throw new Error("far marker should not select a group");
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_exact_entrance_list_selects_its_group(self):
        script = f"""
import {{ pickLayerGroup }} from {MODULE.as_uri()!r};
const layers = [
  {{ id: "a", group: "A", group_key: "lv:1",
     entrances: [{{ x: -10, z: 20, radius: 28 }}, {{ x: -30, z: 40, radius: 28 }}] }},
  {{ id: "b", group: "B", group_key: "lv:2",
     entrances: [{{ x: 100, z: 100, radius: 28 }}] }},
];
const picked = pickLayerGroup(layers, -30, 40);
if (!picked || picked.groupKey !== "lv:1" || picked.layers.length !== 1) {{
  throw new Error("wrong exact group: " + JSON.stringify(picked));
}}
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

if __name__ == "__main__":
    unittest.main()

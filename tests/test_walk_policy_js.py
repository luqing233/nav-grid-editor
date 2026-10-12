# -*- coding: utf-8 -*-
"""可走策略与连通性诊断的回归测试（Node 执行真模块）。

钉住的是这次修掉的一个具体分叉：编辑器的分块告警过去用「节点认 Free、守卫只查
Blocked」这组**互不匹配**的判据，于是报出来的块数在产品默认策略下偏少 —— 实测
map02 报 11 块、规划器看 12 块，base01 报 5、看 8。

同层还要和规划器的邻域规则一致：八连通，斜向时两条正交邻格都要过守卫。
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "src" / "nav_grid_editor" / "web" / "static" / "js" /
          "grid" / "walk-policy.js")
DOC_MODULE = (ROOT / "src" / "nav_grid_editor" / "web" / "static" / "js" /
              "grid" / "grid-document.js")


def _run(script: str) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node 失败:\nstdout={result.stdout}\nstderr={result.stderr}")
    return result.stdout


# 用 GridDocument 本身当被测文档，保证判据是在真实数据结构上验的。
_PRELUDE = f"""
import {{ GridDocument, CELL_FREE, CELL_BLOCKED }} from {DOC_MODULE.as_uri()!r};
import {{ WALK_POLICIES, walkableComponents, walkableAnomalies }} from {MODULE.as_uri()!r};

function doc(width, height, free, blocked) {{
  return GridDocument.fromSparse({{
    origin: [0, 0],
    shape: [height, width],
    cellSize: 1,
    cells: free,
    blocked: blocked,
  }});
}}
const B = (w, h) => ({{ x0: 0, x1: w - 1, z0: 0, z1: h - 1 }});
const strict = WALK_POLICIES.noUnknown;
"""


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class WalkPolicyTest(unittest.TestCase):
    def test_corner_guard_matches_the_node_predicate(self):
        """斜角相接、正交格是未知：不穿未知格时必须是两块。

        旧实现里守卫只查 Blocked（未知格不算挡），于是把这两块并成一块 —— 这就是
        告警少算的那一格。
        """
        out = _run(_PRELUDE + """
const d = doc(2, 2, [[0, 0], [1, 1]], []);
const comps = walkableComponents(d, B(2, 2), strict);
console.log(JSON.stringify(comps));
""")
        self.assertEqual(json.loads(out), [1, 1])

    def test_orthogonally_connected_cells_stay_one_block(self):
        out = _run(_PRELUDE + """
const d = doc(2, 2, [[0, 0], [1, 0]], []);
console.log(JSON.stringify(walkableComponents(d, B(2, 2), strict)));
""")
        self.assertEqual(json.loads(out), [2])

    def test_corner_cut_through_blocked_is_still_refused(self):
        """放宽到允许穿未知格，也不许切阻挡格的角 —— 那是墙。"""
        out = _run(_PRELUDE + """
const d = doc(2, 2, [[0, 0], [1, 1]], [[1, 0], [0, 1]]);
console.log(JSON.stringify(walkableComponents(d, B(2, 2), strict)));
""")
        self.assertEqual(json.loads(out), [1, 1])

    def test_bounds_must_cover_the_document(self):
        """bounds 只作用于守卫，**不**过滤起点。

        对角步的两个端点都在界内时，两个正交邻格必然也在界内（矩形是凸的），所以
        守卫的越界分支实际不可达。调用方必须传能盖住所有已涂格的范围 ——
        `savedBounds()` 正是「[0, shape-1] ∪ 已涂包围盒」，保证覆盖。这条把该前提
        写下来，免得以后有人换个范围进来、界外的格子被当成孤点。
        """
        out = _run(_PRELUDE + """
const d = doc(3, 1, [[0, 0], [2, 0]], []);
console.log(JSON.stringify(
  walkableComponents(d, {x0:0,x1:0,z0:0,z1:0}, strict)));
""")
        # 缩到第一格：(2,0) 在界外，但起点不按 bounds 过滤，所以是两个块而不是一个
        self.assertEqual(json.loads(out), [1, 1])

    def test_permissive_policy_is_refused_not_silently_wrong(self):
        """宽松档不能算：会**少报**块数，也就是显得更连通。宁可报错。

        漏的只是「整块都由未知格组成」的连通区 —— 种子枚举不到（`forEachCell` 只
        产出被设置过的格子），但未知格本身是能被 `getCell` 走访的。所以误差方向是
        偏小，对一条数据质量告警来说恰好是最乐观的那一侧。这条防的是"再出现一个
        数字看着合理、其实不对的诊断"—— 正是这次修的东西。
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", _PRELUDE + """
const d = doc(2, 2, [[0, 0], [1, 1]], []);
walkableComponents(d, B(2, 2), WALK_POLICIES.allowUnknown);
"""],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("只支持 noUnknown", result.stderr)
        self.assertIn("少报", result.stderr)

    def test_real_map02_fragmentation_matches_the_strict_predicate(self):
        """真实数据上钉住那次分叉的具体数字：主图 12 块、最大 31918。

        npz 由 Python 这边解好写成 JSON（别在 node 里嵌 python 字符串，
        引号和换行会把它弄坏），node 只负责跑被测模块。
        缺文件时跳过（子模块未检出）。
        """
        grid = ROOT / "assets" / "nav-grid-data" / "map02_4.grid.npz"
        if not grid.is_file():
            self.skipTest("nav-grid-data 子模块未检出")

        with np.load(grid, allow_pickle=False) as z:
            cells = np.asarray(z["cells"])
        free = [[int(x), int(y)] for y, x in zip(*np.nonzero(cells == 1))]
        blocked = [[int(x), int(y)] for y, x in zip(*np.nonzero(cells == 2))]

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "map02.json"
            payload.write_text(json.dumps({
                "free": free, "blocked": blocked, "shape": list(cells.shape),
            }), encoding="utf-8")
            out = _run(_PRELUDE + f"""
import {{ readFileSync }} from "node:fs";
const data = JSON.parse(readFileSync({str(payload)!r}, "utf8"));
const d = GridDocument.fromSparse({{
  origin: [0, 0], shape: data.shape, cellSize: 1,
  cells: data.free, blocked: data.blocked,
}});
const bounds = {{ x0: 0, x1: data.shape[1] - 1, z0: 0, z1: data.shape[0] - 1 }};
const comps = walkableComponents(d, bounds, strict);
console.log(JSON.stringify([comps.length, comps[0]]));
""")
        self.assertEqual(json.loads(out), [12, 31918])

    def test_anomaly_buckets_are_mutually_exclusive(self):
        """八邻全阻挡的格子必然也没有可走邻居：不能被两条提示各报一次。"""
        out = _run(_PRELUDE + """
const d = doc(3, 3, [[1, 1]], [[0,0],[1,0],[2,0],[0,1],[2,1],[0,2],[1,2],[2,2]]);
const r = walkableAnomalies(d, B(3, 3));
console.log(JSON.stringify({lone: r.lone.length, trapped: r.trapped.length}));
""")
        self.assertEqual(json.loads(out), {"lone": 0, "trapped": 1})

    def test_unknown_neighbor_clears_trapped_but_not_lone(self):
        """邻里有未知格就不算"被困"（还能冒险连出去），但仍算"孤立"。"""
        out = _run(_PRELUDE + """
const d = doc(3, 3, [[1, 1]], [[0,0],[1,0],[2,1],[0,2],[1,2],[2,2]]);
const r = walkableAnomalies(d, B(3, 3));
console.log(JSON.stringify({lone: r.lone.length, trapped: r.trapped.length}));
""")
        self.assertEqual(json.loads(out), {"lone": 1, "trapped": 0})


    def test_block_count_overestimates_isolation(self):
        """块数**高估**孤立：斜角相接、两个正交格都是阻挡时，图上是 2 块，但两个格
        子各自都有可行走邻居（对角那个），既不算孤立也不算被困。

        这是禁斜穿规则**我们自己造的孤立**，游戏里人从对角走得过去。所以块数不能当
        "有多少格不连通"的另一种说法读 —— 方向是偏多。真实数据上 base01 就是
        8 块 / 2 个单格块 / 0 个真被困。
        """
        out = _run(_PRELUDE + """
const d = doc(2, 2, [[0, 0], [1, 1]], [[1, 0], [0, 1]]);
const b = B(2, 2);
const r = walkableAnomalies(d, b);
console.log(JSON.stringify({
  blocks: walkableComponents(d, b, strict).length,
  lone: r.lone.length,
  trapped: r.trapped.length,
}));
""")
        self.assertEqual(json.loads(out),
                         {"blocks": 2, "lone": 0, "trapped": 0})

    def test_singleton_blocks_decompose_exactly(self):
        """单格块数 = 真被困 + 孤立 + **假孤立**，精确成立（不是倾向）。

        推导：单格块里的那格必然没有可行走邻居（否则块会更大），而邻居只可能是
        Free/Blocked/Unknown。没有 Free 邻居时分两种：八邻全阻挡 → 被困；还有未知
        格 → 孤立（`escapePossible` 为真但 `hasWalkableNb` 为假）。有 Free 邻居却
        仍是单格块，只可能那些 Free 都在对角、而两个正交格被挡 —— 就是假孤立。

        所以 `单格块 == 被困 + 孤立` ⟺ 假孤立数 == 0，是定理而不是数据性质。
        """
        out = _run(_PRELUDE + """
function d(free, blocked, shape) {
  const doc0 = doc(shape[1], shape[0], free, blocked);
  const b = { x0: 0, x1: shape[1] - 1, z0: 0, z1: shape[0] - 1 };
  const comps = walkableComponents(doc0, b, strict);
  const { lone, trapped } = walkableAnomalies(doc0, b);
  const singles = comps.filter(c => c === 1).length;
  return [singles, lone.length, trapped.length,
          singles - lone.length - trapped.length];
}
console.log(JSON.stringify({
  // 对角成对、两正交格是阻挡 → 2 个假孤立
  blockedDiag: d([[0,0],[1,1]], [[1,0],[0,1]], [2,2]),
  // 对角成对、两正交格是未知 → 同样是假孤立（邻居是 Free，只是走不过去）
  unknownDiag: d([[0,0],[1,1]], [], [2,2]),
  // 四周全阻挡 → 真被困
  walled: d([[1,1]], [[0,0],[1,0],[2,0],[0,1],[2,1],[0,2],[1,2],[2,2]], [3,3]),
  // 唯一邻居是未知 → 孤立
  unknownOnlyNeighbour: d([[0,0]], [[1,0],[0,1]], [2,2]),
}));
""")
        got = json.loads(out)
        for name, row in got.items():
            # [单格块, 孤立, 被困, 假孤立]
            self.assertEqual(row[0], row[1] + row[2] + row[3],
                             f"{name} 的分解不成立: {row}")
        self.assertEqual(got["blockedDiag"], [2, 0, 0, 2])
        self.assertEqual(got["unknownDiag"], [2, 0, 0, 2])
        self.assertEqual(got["walled"], [1, 0, 1, 0])
        self.assertEqual(got["unknownOnlyNeighbour"], [1, 1, 0, 0])

    def test_real_grids_are_never_trapped(self):
        """真实数据上钉住方向：块数 > 1 但一个真被困都没有。

        也就是多出来的块**全是**我们自己的禁斜穿规则造成的假孤立。缺文件时跳过。
        """
        grid = ROOT / "assets" / "nav-grid-data" / "base01_4.grid.npz"
        if not grid.is_file():
            self.skipTest("nav-grid-data 子模块未检出")
        with np.load(grid, allow_pickle=False) as z:
            cells = np.asarray(z["cells"])
        free = [[int(x), int(y)] for y, x in zip(*np.nonzero(cells == 1))]
        blocked = [[int(x), int(y)] for y, x in zip(*np.nonzero(cells == 2))]
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "base01.json"
            payload.write_text(json.dumps({
                "free": free, "blocked": blocked, "shape": list(cells.shape),
            }), encoding="utf-8")
            out = _run(_PRELUDE + f"""
import {{ readFileSync }} from "node:fs";
const data = JSON.parse(readFileSync({str(payload)!r}, "utf8"));
const d = GridDocument.fromSparse({{
  origin: [0, 0], shape: data.shape, cellSize: 1,
  cells: data.free, blocked: data.blocked,
}});
const b = {{ x0: 0, x1: data.shape[1] - 1, z0: 0, z1: data.shape[0] - 1 }};
const comps = walkableComponents(d, b, strict);
const r = walkableAnomalies(d, b);
console.log(JSON.stringify([comps.length,
                            comps.filter(c => c === 1).length,
                            r.lone.length, r.trapped.length]));
""")
        self.assertEqual(json.loads(out), [8, 2, 2, 0])

    def test_reported_anomalies_are_singleton_blocks_on_real_data(self):
        """真实数据上：两组互斥，且每个报出的异常格确实都在单格块里。

        反过来不成立 —— 单格块比并集多的部分就是假孤立。base01 上 8 块里有 2 个
        单格块、孤立 2、被困 0，三者相等只是**这批数据没有假孤立**，不是恒等式。
        """
        grid = ROOT / "assets" / "nav-grid-data" / "base01_4.grid.npz"
        if not grid.is_file():
            self.skipTest("nav-grid-data 子模块未检出")
        with np.load(grid, allow_pickle=False) as z:
            cells = np.asarray(z["cells"])
        free = [[int(x), int(y)] for y, x in zip(*np.nonzero(cells == 1))]
        blocked = [[int(x), int(y)] for y, x in zip(*np.nonzero(cells == 2))]
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "base01.json"
            payload.write_text(json.dumps({
                "free": free, "blocked": blocked, "shape": list(cells.shape),
            }), encoding="utf-8")
            out = _run(_PRELUDE + f"""
import {{ readFileSync }} from "node:fs";
const data = JSON.parse(readFileSync({str(payload)!r}, "utf8"));
const d = GridDocument.fromSparse({{
  origin: [0, 0], shape: data.shape, cellSize: 1,
  cells: data.free, blocked: data.blocked,
}});
const b = {{ x0: 0, x1: data.shape[1] - 1, z0: 0, z1: data.shape[0] - 1 }};
const r = walkableAnomalies(d, b);
// 用规划器规则反查每个异常格所在块的大小
const size = new Map();
const seen = new Set();
d.forEachCell((ix, iz, v) => {{
  if (v !== 1) return;
  const start = ix + "," + iz;
  if (seen.has(start)) return;
  seen.add(start);
  const stack = [start], cellsIn = [];
  while (stack.length) {{
    const k = stack.pop(); cellsIn.push(k);
    const c = k.indexOf(",");
    const cx = parseInt(k, 10), cz = +k.slice(c + 1);
    for (let dx = -1; dx <= 1; dx++) for (let dz = -1; dz <= 1; dz++) {{
      if (!dx && !dz) continue;
      const nx = cx + dx, nz = cz + dz, nk = nx + "," + nz;
      if (seen.has(nk) || d.getCell(nx, nz) !== 1) continue;
      if (dx && dz && (d.getCell(cx + dx, cz) !== 1 || d.getCell(cx, cz + dz) !== 1)) continue;
      seen.add(nk); stack.push(nk);
    }}
  }}
  for (const k of cellsIn) size.set(k, cellsIn.length);
}});
const lone = new Set(r.lone.map(p => p[0] + "," + p[1]));
const trapped = new Set(r.trapped.map(p => p[0] + "," + p[1]));
const overlap = [...lone].filter(k => trapped.has(k));
const notSingleton = [...lone, ...trapped].filter(k => size.get(k) !== 1);
console.log(JSON.stringify([overlap.length, notSingleton.length]));
""")
        self.assertEqual(json.loads(out), [0, 0])


    def test_anomaly_sets_are_disjoint_and_always_singleton_blocks(self):
        """报出的异常格必须**都是单格块**，且两组互斥。

        这两条是构造上就该成立的（选点用邻居事实、分档用 else-if），所以一旦破了
        就是实现漂移，不是数据问题：
        - 单格块 ⊇ (孤立 ∪ 被困)：格子级判据比图连通性**弱**，报出的必然在图上也孤立；
        - 反过来不成立 —— 单格块比它们并集多出来的，是禁斜穿规则造的**假孤立**。
        """
        out = _run(_PRELUDE + """
function probe(free, blocked) {
  const d = doc(2, 2, free, blocked);
  const b = B(2, 2);
  const r = walkableAnomalies(d, b);
  return {
    blocks: walkableComponents(d, b, strict).length,
    lone: r.lone.length,
    trapped: r.trapped.length,
  };
}
console.log(JSON.stringify({
  // 唯一可行走格的对角是未知格：图上 1 块 = 1 孤立，真被困 0
  unknownDiagonal: probe([[0, 0]], [[1, 0], [0, 1]]),
  // 对角是**可行走**格：图上 2 块，但既不孤立也不被困 —— 假孤立
  freeDiagonal: probe([[0, 0], [1, 1]], [[1, 0], [0, 1]]),
}));
""")
        self.assertEqual(json.loads(out), {
            "unknownDiagonal": {"blocks": 1, "lone": 1, "trapped": 0},
            "freeDiagonal": {"blocks": 2, "lone": 0, "trapped": 0},
        })


if __name__ == "__main__":
    unittest.main()

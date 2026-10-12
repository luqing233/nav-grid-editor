// 可走/阻挡的**唯一策略来源**，以及由它派生的连通性诊断。
//
// 为什么要抽出来：编辑器里这些派生量过去各自内联了「什么叫可走」，于是必然漂移。
// 具体出过一次：分块告警用「节点认 Free、守卫只查 Blocked」这组**互不匹配**的
// 判据，报出来的块数在产品的默认策略下是错的 —— 实测 map02 报 11 块、而规划器
// （allow_unknown=False）看到 12 块，base01 报 5、看到 8。用户保存时看到的数字
// 和实际能不能走过去对不上。
//
// 关键不变量：**守卫判据必须与节点判据同源**。守卫比节点松时，斜向移动能借一个
// 未知格把两片可走区并起来 —— 这正是那次少算一块的原因，而这种错误在图上几乎
// 看不出来（map02 上是 3 格 + 2 格两个碎片）。
//
// 注意：只有「节点判据」和「守卫判据」**都**取同一档策略，这个策略才对应规划器
// 的某一档 allow_unknown。混着取就不对应任何一档，是个没有意义的诊断。
import { CELL_BLOCKED, CELL_FREE } from "./grid-document.js";

export const WALK_POLICIES = Object.freeze({
  // 与 ok-end-field 的 allow_unknown=False（产品默认）一致：
  // 只走已确认可走的格子，斜向不许借未知格切角。
  noUnknown: Object.freeze({
    id: "noUnknown",
    label: "不穿未知格",
    node: value => value === CELL_FREE,
    guard: value => value !== CELL_FREE,
  }),
  // 与 allow_unknown=True 一致：未知格当作可以冒险通过，但仍不许切阻挡格的角。
  allowUnknown: Object.freeze({
    id: "allowUnknown",
    label: "允许穿越未知格",
    node: value => value !== CELL_BLOCKED,
    guard: value => value !== CELL_BLOCKED,
  }),
});

const D8 = [
  [1, 0], [-1, 0], [0, 1], [0, -1],
  [1, 1], [1, -1], [-1, 1], [-1, -1],
];

/** 某格按该策略是否可走；越界与 grid_io 一致，一律算不可走。 */
function walkableAt(doc, bounds, policy, ix, iz) {
  if (ix < bounds.x0 || ix > bounds.x1 || iz < bounds.z0 || iz > bounds.z1) {
    return false;
  }
  return policy.node(doc.getCell(ix, iz));
}

/**
 * 可行走格的连通块大小（降序）。
 *
 * 邻域与规划器一致：八连通，斜向时两条正交邻格**都**要过守卫（否则算穿墙角）。
 *
 * 只接受 WALK_POLICIES.noUnknown —— 也就是规划器在 allow_unknown=False（产品
 * 默认）下看到的块数，告警要显示的就是这个。
 *
 * bounds 只作用于守卫，不用于过滤起点；调用方必须传能盖住所有已涂格的范围
 * （`GridDocument.savedBounds()` 就是「[0, shape-1] ∪ 已涂包围盒」，保证覆盖）。
 * 顺带：两个对角端点都在界内时，它们的两个正交邻格必然也在界内（矩形是凸的），
 * 所以守卫里的越界分支实际不可达 —— 留着只是为了不依赖这个推理。
 *
 * 为什么不让传宽松档：受限的是**种子枚举**，不是遍历。BFS 走访邻居用的是
 * `getCell`，它对未设置格返回 `CELL_UNKNOWN`，所以未知格**是能走过去的**；真正
 * 缺的只是 `forEachCell` 不会把未知格交出来当起点。
 *
 * 于是宽松档漏掉的只有「整块都由未知格组成」的连通区，算出来的块数是个**下界**。
 * 方向是"少报块数" = 显得更连通 = 对一条数据质量告警来说是**最乐观、最不该错**的
 * 方向。宁可报错，也不要再出现"数字看着合理、其实不对"的诊断。
 */
export function walkableComponents(doc, bounds, policy) {
  if (policy !== WALK_POLICIES.noUnknown) {
    throw new Error(
      "walkableComponents 只支持 noUnknown 策略：宽松档下未知格组成的连通区枚举" +
      "不到（forEachCell 只产出被设置过的格子），而未知格又能被 getCell 走访，" +
      "于是块数会**少报** —— 对数据质量告警来说这是最乐观的方向");
  }
  if (!bounds) return [];
  const seen = new Set();
  const comps = [];
  doc.forEachCell((ix, iz, value) => {
    if (!policy.node(value)) return;
    const start = ix + "," + iz;
    if (seen.has(start)) return;
    seen.add(start);
    const stack = [start];
    let n = 0;
    while (stack.length) {
      const k = stack.pop();
      n++;
      const c = k.indexOf(",");
      const cx = parseInt(k, 10);
      const cz = +k.slice(c + 1);
      for (const [dx, dz] of D8) {
        const nx = cx + dx;
        const nz = cz + dz;
        const nk = nx + "," + nz;
        if (seen.has(nk) || !policy.node(doc.getCell(nx, nz))) continue;
        if (dx && dz &&
            (!walkableAt(doc, bounds, policy, cx + dx, cz) ||
             !walkableAt(doc, bounds, policy, cx, cz + dz))) {
          continue;
        }
        seen.add(nk);
        stack.push(nk);
      }
    }
    comps.push(n);
  });
  comps.sort((a, b) => b - a);
  return comps;
}

/**
 * 可行走格里值得提示的两类"异常"，**两档互斥**。
 *
 * 用**邻居事实**而不是图连通性：禁斜穿墙角是我们自己的保守策略，不是物理 ——
 * 沿图算会把"四正交被挡、只能斜着连过去"的格子误判成孤立（base01_3 实测 6 个
 * 单格块里有 5 个是这么来的），让用户去修其实没问题的格子。
 *
 * 两档用的是**不同**策略，这是刻意的，不是笔误：
 * - trapped 问的是"有没有任何逃出去的可能"，所以取宽松档（未知格也算出路）；
 * - lone 问的是"有没有不用冒险就挨着的邻居"，所以取默认档。
 * 互斥是必须的：八邻全阻挡的格子**必然**也没有可行走邻居，不排除就会让同一个
 * 格子被两条提示各报一次。所以一次算完、用 else-if 分档。
 */
export function walkableAnomalies(doc, bounds) {
  if (!bounds) return { lone: [], trapped: [] };
  const strict = WALK_POLICIES.noUnknown;
  const permissive = WALK_POLICIES.allowUnknown;
  const lone = [];
  const trapped = [];
  doc.forEachCell((ix, iz, value) => {
    if (!strict.node(value)) return;
    let hasWalkableNb = false;
    let escapePossible = false;
    for (const [dx, dz] of D8) {
      const nx = ix + dx;
      const nz = iz + dz;
      if (nx < bounds.x0 || nx > bounds.x1 ||
          nz < bounds.z0 || nz > bounds.z1) {
        continue;  // 越界当阻挡，不给"还能连出去"的希望
      }
      const neighbor = doc.getCell(nx, nz);
      if (strict.node(neighbor)) hasWalkableNb = true;
      if (permissive.node(neighbor)) escapePossible = true;
    }
    if (!escapePossible) trapped.push([ix, iz]);
    else if (!hasWalkableNb) lone.push([ix, iz]);
  });
  return { lone, trapped };
}

/** 告警文案里用的策略说明，例如「按『不穿未知格』口径」。 */
export function policyLabel(policy) {
  return "按「" + policy.label + "」口径";
}

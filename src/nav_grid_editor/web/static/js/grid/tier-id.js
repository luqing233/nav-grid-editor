// 主图的 tier 有两种拼写，进出各走一个函数，别再内联判断。
//
//   API 口径     "__main__"    —— nav-graph / floor-map / SSE 的 gridTierId
//   落盘口径     ""            —— 文件名 `map02_4.grid.npz`（不带 tier 段）
//
// 两种都必须存在：文件名里不能出现 `__main__`，而 API 里空串又表达不出「就是主图」
// 而不是「没选分层」。以前这段转换散在六处内联三元里，必然漂移 —— 服务端那边
// 同样的问题已经真的造出过 `map02____main___4.grid.npz` 这种伪文件名。
export const MAIN_TIER_ID = "__main__";

/** API 口径 → 落盘口径；同时给 setActiveTier / 请求参数用（主图 = 关掉分层）。 */
export function toDiskTier(tierId) {
  const text = String(tierId || "");
  return text === MAIN_TIER_ID ? "" : text;
}

/** 落盘口径（或空）→ API 口径。 */
export function toApiTier(tierId) {
  const text = String(tierId || "");
  return text ? text : MAIN_TIER_ID;
}

/** 两种口径混着比时用这个，别自己写 `===`。 */
export function sameTier(a, b) {
  return toApiTier(a) === toApiTier(b);
}

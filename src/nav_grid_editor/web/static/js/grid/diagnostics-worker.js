import { GridDocument } from "./grid-document.js";
import { WALK_POLICIES, walkableComponents, walkableAnomalies, policyLabel } from "./walk-policy.js";
self.onmessage = ({data}) => {
  try {
    const doc = GridDocument.fromSparse(data.document);
    const components = walkableComponents(doc, doc.savedBounds(), WALK_POLICIES.noUnknown);
    const {lone, trapped} = walkableAnomalies(doc, doc.savedBounds());
    const parts = [];
    if (components.length > 1) parts.push(`可行走分成 ${components.length} 块（${policyLabel(WALK_POLICIES.noUnknown)}），最大块 ${(100 * components[0] / doc.freeCount).toFixed(1)}%`);
    if (lone.length) parts.push(`${lone.length} 个可行走格没有可行走邻居`);
    if (trapped.length) parts.push(`${trapped.length} 个可行走格八邻全是阻挡`);
    self.postMessage({id:data.id,text:parts.join("；")});
  } catch(error) { self.postMessage({id:data.id,error:error.message}); }
};

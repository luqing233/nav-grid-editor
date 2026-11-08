import { GridDocument } from "../grid/grid-document.js";
import { WALK_POLICIES, walkableComponents, walkableAnomalies } from "../grid/walk-policy.js";

function check(snapshot) {
  if (!snapshot) return {components:[], free:0, blocked:0, lone:0, trapped:0};
  const doc = GridDocument.fromSparse(snapshot);
  const components = walkableComponents(doc, doc.savedBounds(), WALK_POLICIES.noUnknown);
  const {lone, trapped} = walkableAnomalies(doc, doc.savedBounds());
  return {components, free:doc.freeCount, blocked:doc.blockedCount, lone:lone.length, trapped:trapped.length};
}
self.onmessage = ({data}) => {
  try {
    const files = data.map(file => ({before:check(file.before), after:check(file.after)}));
    self.postMessage({files});
  } catch (error) { self.postMessage({error:error.message}); }
};

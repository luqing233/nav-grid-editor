let sequence = 0;
// A short-lived worker releases its snapshot and traversal memory after each job.
export function diagnoseGrid(document) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL("./diagnostics-worker.js", import.meta.url), {type:"module"});
    const timer = setTimeout(() => finish(new Error("连通性诊断超时")), 30000);
    function finish(error, text) { clearTimeout(timer); worker.terminate(); error ? reject(error) : resolve(text); }
    worker.onmessage = ({data}) => { if(data.id===id) finish(data.error ? new Error(data.error) : null,data.text); };
    worker.onerror = event => finish(new Error(event.message));
    worker.postMessage({id,document:{...document.exportSparse(),origin:document.origin,shape:document.shape,cellSize:document.cellSize}});
  });
}

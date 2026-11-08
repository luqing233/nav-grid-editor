// Disk-snapshot export: the server owns the job across page reloads.
export function initDatasetExport({ hasUnsaved = () => false, onOpen = () => {}, unsavedNames = () => [], saveAll = null } = {}) {
  const target = document.getElementById("datasetTarget");
  const button = document.getElementById("btnDatasetExport");
  const status = document.getElementById("datasetExportStatus");
  let timer = null;
  let starting = false;
  let running = false;
  let requestId = 0;
  const show = data => {
    running = !!data.running;
    button.disabled = starting || running;
    status.textContent = data.error ? "导出失败：" + data.error :
      (data.phase || "未导出") +
      (data.running && data.total ? ` · ${data.done}/${data.total}` : "") +
      (data.output ? "\n目标：" + data.output : "") +
      (data.summary ? `\n${data.summary.images} 张灰度图 · ${data.summary.grids} 份网格` : "");
    if (data.default_parent) target.placeholder = data.default_parent + " 下自动生成新目录（可留空）";
  };
  async function poll() {
    clearTimeout(timer);
    const id = ++requestId;
    try {
      const response = await fetch("/api/dataset/export/status");
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json();
      if (id !== requestId) return;
      show(data);
      if (data.running) timer = setTimeout(poll, 1000);
    } catch (err) {
      if (id !== requestId) return;
      status.textContent = "读取导出状态失败：" + err.message;
      timer = setTimeout(poll, 3000);
    }
  }
  button.onclick = async () => {
    if (starting || button.disabled) return;
    if (hasUnsaved()) {
      status.textContent = "请先保存网格修改，再导出数据包。" + unsavedNames().join("、");
      return;
    }
    ++requestId;
    starting = true;
    button.disabled = true;
    clearTimeout(timer);
    try {
      const response = await fetch("/api/dataset/export", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target: target.value.trim() }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        const message = data.error || "HTTP " + response.status;
        if (data.ok === false || (response.status >= 400 && response.status < 500)) {
          status.textContent = "导出失败：" + message;
          return; // A definitive rejection must remain visible until the next user action.
        }
        throw new Error(message);
      }
      try { localStorage.setItem("nav-grid-export-target", target.value); } catch { /* Optional preference. */ }
      starting = false;
      await poll();
    } catch (err) {
      status.textContent = "导出失败：" + err.message;
      // The request may have reached the server even if its reply was lost.
      timer = setTimeout(poll, 1000);
    } finally {
      starting = false;
      button.disabled = running;
    }
  };
  document.getElementById("btnDatasetPanel").onclick = () => { onOpen(); target.focus(); void poll(); };
  try { target.value = localStorage.getItem("nav-grid-export-target") || ""; } catch { /* Optional preference. */ }
  if (saveAll) {
    const saveButton = document.createElement("button"); saveButton.textContent = "保存全部并导出";
    button.after(saveButton);
    saveButton.onclick = async () => {
      saveButton.disabled = true;
      try { await saveAll(); if (hasUnsaved()) throw new Error("仍有未保存修改"); await button.onclick(); }
      catch (error) { status.textContent = error.message; }
      finally { saveButton.disabled = false; }
    };
  }
  void poll();
  return { poll, stop: () => clearTimeout(timer) };
}

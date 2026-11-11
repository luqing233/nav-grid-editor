// Manual repository synchronization. The server performs the destructive Git reset.
export function initDataSync({
  hasUnsaved = () => false,
  unsavedNames = () => [],
  setStatus = () => {},
  onSynced = null,
  confirmSync = message => globalThis.confirm(message),
} = {}) {
  const button = document.getElementById("btnRepoSync");
  if (!button) return { sync: async () => false };
  let running = false;

  async function sync() {
    if (running) return false;
    const names = unsavedNames();
    const warning = "同步会以远端数据仓库为准，强制覆盖本地地图网格、连接、层声明和草稿，" +
      "本地提交及未提交修改都会丢失。\n\n" +
      (hasUnsaved() ? `当前还有 ${names.length} 份未保存网格：${names.join("、")}\n\n` : "") +
      "确定继续？";
    running = true;
    try {
      if (!await confirmSync(warning)) { running = false; return false; }
    } catch (error) {
      running = false;
      setStatus("无法确认同步：" + (error.message || String(error)), "warning");
      return false;
    }

    button.disabled = true;
    const label = button.textContent;
    button.textContent = "同步中…";
    setStatus("正在从远端数据仓库同步地图网格与资源…", "active");
    try {
      const response = await fetch("/api/data-repository/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ replace_local: true }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "HTTP " + response.status);
      const revision = data.current ? String(data.current).slice(0, 8) : "最新版本";
      setStatus(`数据仓库已同步到 ${revision}，正在重新载入…`, "ready");
      if (onSynced) await onSynced(data);
      return true;
    } catch (error) {
      setStatus("数据仓库同步失败：" + (error.message || String(error)), "warning");
      return false;
    } finally {
      running = false;
      button.disabled = false;
      button.textContent = label;
    }
  }

  button.onclick = sync;
  return { sync };
}

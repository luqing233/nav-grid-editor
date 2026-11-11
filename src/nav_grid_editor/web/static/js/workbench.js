// Presentation only. Map, grid and serialization remain owned by the editor.
export function initWorkbenchUI(host) {
  const $ = (id) => document.getElementById(id);
  const resource = $("resourceDialog");
  const titles = {
    capture: "地图采集",
    markers: "地图标记",
    export: "发布数据",
    log: "运行日志",
    maintenance: "数据管理",
  };
  $("btnInspectorClose").onclick = () => host.setSidePanel(false);

  function openResource(name) {
    if (document.body.classList.contains("pr-review-open")) return;
    for (const panel of resource.querySelectorAll("[data-resource-panel]"))
      panel.hidden = panel.dataset.resourcePanel !== name;
    for (const button of resource.querySelectorAll("[data-resource-tab]")) {
      button.classList.toggle("is-active", button.dataset.resourceTab === name);
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.resourceTab === name),
      );
    }
    $("resourceTitle").textContent = titles[name];
    if (!resource.open) resource.showModal();
    resource.querySelector(".resource-content").scrollTop = 0;
  }
  for (const button of resource.querySelectorAll("[data-resource-tab]"))
    button.onclick = () => openResource(button.dataset.resourceTab);
  for (const [id, panel] of Object.entries({
    btnCapturePanel: "capture",
    btnMarkersPanel: "markers",
    btnLogPanel: "log",
    btnMaintenancePanel: "maintenance",
  }))
    $(id).onclick = () => openResource(panel);
  for (const button of document.querySelectorAll("[data-close-dialog]"))
    button.onclick = () => button.closest("dialog").close();

  $("quickFit").onclick = host.fit;
  $("quickBack").onclick = host.back;
  $("quickLocation").onclick = () => $("locationDialog").showModal();
  $("jumpGo").onclick = () => {
    if (!$("jumpX").value.trim() || !$("jumpZ").value.trim()) {
      $("jumpX").focus();
      return;
    }
    const x = Number($("jumpX").value),
      z = Number($("jumpZ").value);
    if (!Number.isFinite(x) || !Number.isFinite(z)) return;
    host.jump(x, z);
    $("locationDialog").close();
  };
  for (const id of ["jumpX", "jumpZ"])
    $(id).onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        $("jumpGo").click();
      }
    };
  $("btnZoomIn").onclick = () => host.zoom(1.25);
  $("btnZoomOut").onclick = () => host.zoom(0.8);

  const help = document.createElement("dialog");
  help.id = "shortcutDialog";
  help.setAttribute("aria-labelledby", "shortcutTitle");
  help.innerHTML = `<header class="dialog-heading"><h2 id="shortcutTitle">操作与快捷键</h2><button class="icon-button" aria-label="关闭操作说明">×</button></header>
    <dl class="shortcut-list"><dt>右键拖动 / 滚轮</dt><dd>平移地图 / 缩放</dd><dt>1 / 2 / 3</dt><dd>可走 / 阻挡 / 擦除；有选区时填充</dd><dt>按住数字 + 滚轮</dt><dd>调整画笔宽度</dd><dt>Shift + 左键拖动</dt><dd>框选网格</dd><dt>Ctrl S</dt><dd>保存当前网格、标定或连接</dd><dt>Ctrl Z / Y</dt><dd>撤销 / 重做</dd><dt>Ctrl K</dt><dd>搜索命令</dd><dt>Esc</dt><dd>关闭窗口或取消选择</dd></dl>
    <p class="panel-note">PR 审阅：J / K 切换变化区，1 / 2 / 3 切换对比视图，F 定位，H 收起面板。</p>`;
  document.body.append(help);
  help.querySelector("button").onclick = () => help.close();
  $("quickHelp").onclick = () => help.showModal();

  const commands = [
    ["切换主图", () => $("selMap").focus(), "地图"],
    ["切换分层", () => $("selTier").focus(), "地图"],
    ["浏览地图", "btnBrowse", "模式"],
    ["编辑网格", "btnEdit", "模式"],
    ["编辑分层连接", "btnLink", "模式"],
    ["标定地图", "btnCalib", "模式"],
    ["读取坐标", "btnPick", "模式"],
    [
      "保存当前内容",
      () =>
        document
          .querySelector(".save-actions .tb-act:not([hidden]) button")
          ?.click(),
      "Ctrl S",
    ],
    ["适应窗口", "quickFit", "视图"],
    ["定位世界坐标", "quickLocation", "视图"],
    ["打开 3D 透视", "btnPerspective", "视图"],
    ["显示当前工具属性", () => host.setSidePanel(true), "视图"],
    ["地图采集与合成", () => openResource("capture"), "工作空间"],
    ["筛选地图标记", () => openResource("markers"), "工作空间"],
    ["导出数据包", "btnDatasetPanel", "工作空间"],
    ["查看运行日志", () => openResource("log"), "工作空间"],
    ["数据仓库管理", () => openResource("maintenance"), "工作空间"],
    ["审阅数据 PR", "btnPRReview", "审阅"],
    ["新建数据 PR", "btnLocalPR", "发布"],
    ["操作与快捷键", "quickHelp", "帮助"],
  ];
  let commandIndex = 0,
    matches = [];
  const commandDialog = $("commandDialog");
  function selectCommand(index) {
    commandIndex = Math.max(0, Math.min(matches.length - 1, index));
    [...$("commandResults").children].forEach((node, i) => {
      node.setAttribute("aria-selected", String(i === commandIndex));
    });
    const selected = $("commandResults").children[commandIndex];
    if (selected?.id) {
      $("commandSearch").setAttribute("aria-activedescendant", selected.id);
      selected.scrollIntoView({ block: "nearest" });
    } else $("commandSearch").removeAttribute("aria-activedescendant");
  }
  function executeCommand(index) {
    const command = matches[index];
    if (!command) return;
    commandDialog.close();
    if (typeof command[1] === "function") command[1]();
    else $(command[1]).click();
  }
  function renderCommands() {
    const query = $("commandSearch").value.trim().toLocaleLowerCase();
    matches = commands.filter((c) =>
      (c[0] + c[2]).toLocaleLowerCase().includes(query),
    );
    $("commandResults").replaceChildren();
    matches.forEach((command, index) => {
      const row = document.createElement("button");
      row.id = "command-" + index;
      row.type = "button";
      row.setAttribute("role", "option");
      row.tabIndex = -1;
      const name = document.createElement("span");
      name.textContent = command[0];
      const hint = document.createElement("small");
      hint.textContent = command[2];
      row.append(name, hint);
      row.onclick = () => executeCommand(index);
      $("commandResults").append(row);
    });
    if (!matches.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "没有找到命令，试试“网格”或“采集”";
      $("commandResults").append(empty);
    }
    selectCommand(0);
  }
  $("commandSearch").setAttribute("role", "combobox");
  $("commandSearch").setAttribute("aria-controls", "commandResults");
  $("commandSearch").setAttribute("aria-expanded", "true");
  $("commandSearch").oninput = renderCommands;
  $("commandSearch").onkeydown = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      selectCommand(commandIndex + (e.key === "ArrowDown" ? 1 : -1));
    }
    if (e.key === "Enter") {
      e.preventDefault();
      executeCommand(commandIndex);
    }
  };
  $("btnCommand").onclick = () => {
    $("commandSearch").value = "";
    commandDialog.showModal();
    renderCommands();
    $("commandSearch").focus();
  };
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (commandDialog.open) commandDialog.close();
      else if (!document.querySelector("dialog[open]") && $("pvView").hidden)
        $("btnCommand").click();
    } else if (
      e.key === "?" &&
      !document.querySelector("dialog[open]") &&
      !e.target.closest("input, textarea, select, [contenteditable]")
    )
      $("quickHelp").click();
  });

  function confirm(message) {
    const dialog = $("confirmDialog");
    $("confirmMessage").textContent = message;
    return new Promise((resolve) => {
      const finish = (value) => {
        dialog.close();
        dialog.oncancel = null;
        resolve(value);
      };
      $("confirmCancel").onclick = () => finish(false);
      $("confirmContinue").onclick = () => finish(true);
      dialog.oncancel = (e) => {
        e.preventDefault();
        finish(false);
      };
      dialog.showModal();
      $("confirmCancel").focus();
    });
  }
  function syncMode(mode) {
    $("interactionHint").textContent = {
      browse: "右键拖动平移 · 滚轮缩放",
      edit: "左键绘制 · Shift 框选 · 1 / 2 / 3 切换画笔",
      link: "依次点选起点和终点 · 可跨分层连接",
      calib: "点击特征点，添加游戏坐标",
      pick: "点击地图读取坐标 · 右键平移",
    }[mode];
  }
  return { openResource, syncMode, confirm };
}

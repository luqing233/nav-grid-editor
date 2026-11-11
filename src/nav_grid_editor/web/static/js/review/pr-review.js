import { GridRasterCache } from "../grid/grid-raster-cache.js";
import { TRANSITIONS, STATE_NAMES, regionsOf, snapshotDocument, transitionAt } from "./model.js";
import { initMergeReview } from "./merge-review.js";

const element = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};

export function initPRReview(host) {
  const panel = element("section", undefined, "pr-review-panel");
  panel.id = "prReviewPanel"; panel.hidden = true;
  panel.setAttribute("aria-label", "数据 PR 审阅");
  panel.innerHTML = `<div class="pr-review-heading">
      <div><span class="pr-review-kicker">DATA REVIEW</span><strong id="prReviewHeading">数据 PR 审阅</strong></div>
      <span class="pr-review-readonly">只读</span>
      <button id="prReviewCollapse" class="pr-review-icon" type="button" aria-label="收起审阅面板" title="收起面板（H）" aria-expanded="true">‹</button>
      <button id="prReviewClose" class="pr-review-icon" type="button" aria-label="关闭审阅" title="关闭审阅（Esc）">×</button>
    </div>
    <div class="pr-review-loader">
      <label for="prReviewOpenSelect">打开的 PR</label>
      <div class="pr-review-input-row"><select id="prReviewOpenSelect" aria-describedby="prReviewListStatus" disabled><option value="">正在获取 PR 列表…</option></select><button id="prReviewRefresh" type="button" title="刷新 open PR 列表" aria-label="刷新 open PR 列表">刷新</button></div>
      <div id="prReviewListStatus" role="status"></div>
      <label for="prReviewInput">PR 链接或编号</label>
      <div class="pr-review-input-row"><input id="prReviewInput" placeholder="编号或 GitHub PR 链接" value="2" autocomplete="off" spellcheck="false"><button id="prReviewLoad" type="button" aria-label="加载 data PR">加载 <span aria-hidden="true">↵</span></button></div>
      <div id="prReviewStatus" role="status">输入 PR，开始地图审阅。</div>
    </div>
    <div id="prReviewContent" hidden>
      <div class="pr-review-context">
        <a id="prReviewLink" target="_blank" rel="noreferrer"></a>
        <details class="pr-review-revision"><summary id="prReviewCounts"></summary><div id="prReviewRevision"></div></details>
        <label for="prReviewRegions" class="sr-only">变化区</label><select id="prReviewRegions" aria-label="选择变化区"></select>
      </div>
      <div class="pr-review-scroll">
        <div class="pr-review-section-heading"><h3>检查摘要</h3><span>当前网格</span></div>
        <div id="prReviewSummary"></div><div id="prReviewChecks"></div>
        <details class="pr-review-help"><summary>审阅说明与快捷键</summary>
          <p>J / K：下一个 / 上一个变化区<br>1 / 2 / 3：差异 / 修改前 / 修改后<br>F：定位变化区 · H：收起面板 · Esc：退出</p>
          <p>连通性按“不穿未知格”检查。独立路线需确认用途；地图叠加使用本地底图，实际地形仍需实走核验。</p>
        </details>
      </div>
    </div>`;
  const dock = element("section", undefined, "pr-review-dock");
  dock.id="prReviewDock";dock.hidden=true;dock.setAttribute("aria-label","地图对比工具");
  dock.innerHTML=`<div class="pr-review-dock-main">
      <div class="pr-review-views" role="group" aria-label="对比视图"><button data-pr-view="diff" title="地图差异（1）">地图差异</button><button data-pr-view="before" title="修改前（2）">修改前</button><button data-pr-view="after" title="修改后（3）">修改后</button><button data-pr-view="ancestor" hidden>共同祖先</button><button data-pr-view="current" hidden>当前主线</button><button data-pr-view="incoming" hidden>此 PR</button><button data-pr-view="result" hidden>合并结果</button></div>
      <div class="pr-review-navigation"><button id="prReviewPrev" class="pr-review-icon" aria-label="上一个变化区" title="上一个变化区（K）">←</button><span id="prReviewRegionCount"></span><button id="prReviewNext" class="pr-review-icon" aria-label="下一个变化区" title="下一个变化区（J）">→</button><button id="prReviewFocus" class="pr-review-icon" aria-label="定位变化区" title="定位变化区（F）">⌖</button></div>
    </div><div class="pr-review-dock-detail">
      <label class="pr-review-opacity">透明度<input id="prReviewOpacity" aria-label="叠加透明度" type="range" min="0" max="1" step="0.05" value="0.8"><output id="prReviewOpacityValue">80%</output></label>
      <div id="prReviewHover">移动鼠标查看格子变化</div>
    </div>`;
  host.container.append(panel,dock);
  const $ = id => panel.querySelector("#"+id) || dock.querySelector("#"+id);
  const preferenceKey="nav-grid-editor.pr-review.v1";
  let preferences={};
  try { preferences=JSON.parse(localStorage.getItem(preferenceKey)) || {}; } catch { /* Local storage may be unavailable. */ }
  if(typeof preferences!=="object" || Array.isArray(preferences))preferences={};
  if(typeof preferences.pr==="string")$("prReviewInput").value=preferences.pr;
  if(typeof preferences.opacity==="number" && Number.isFinite(preferences.opacity))$("prReviewOpacity").value=String(Math.max(0,Math.min(1,preferences.opacity)));
  const remember=()=>{try{localStorage.setItem(preferenceKey,JSON.stringify(preferences));}catch{/* Preferences are optional. */}};
  const raster = new GridRasterCache();
  let report = null, regions = [], index = 0, file = null, view = ["diff","before","after"].includes(preferences.view)?preferences.view:"diff", generation = 0;
  let lookup = new Map(), documents = {}, diffToken = {}, checks = null, checksError = "";
  let diagnosticTimer = null, diffChunks = new Map();
  let worker = null, controller = null, selection = 0, closed = true, active = false;
  let listController = null, listGeneration = 0;
  const status = (text, tone="neutral") => { $("prReviewStatus").textContent=text;$("prReviewStatus").dataset.tone=tone; };
  const number=value=>value.toLocaleString("zh-CN");
  function mergeViews(enabled) {
    for(const button of dock.querySelectorAll("[data-pr-view]")) {
      const key=button.dataset.prView;
      button.hidden=key!=="diff" && (enabled ? ["before","after"].includes(key) : !["before","after"].includes(key));
    }
  }
  const mergeUI=initMergeReview(panel,{
    exit:()=>void load(),
    begin(){generation++;selection++;controller?.abort();worker?.terminate();clearTimeout(diagnosticTimer);
      $("prReviewLoad").disabled=false;$("prReviewLoad").textContent="加载 ↵";panel.removeAttribute("aria-busy");},
    render:()=>host.render(),
    focus(conflict){
      const found=regions.findIndex(r=>r.conflict_id===conflict.id);
      const fallback=regions.findIndex(r=>report.files[r.fileIndex].path===conflict.path);
      if(found>=0 || fallback>=0)void selectRegion(found>=0?found:fallback);
    },
    async result(data){
      const request=++generation;
      if(!host.canOpen())throw new Error("请先结束正在进行的编辑操作");
      if(!active){await host.activate();active=true;}
      if(closed || request!==generation)return;
      report=data;regions=regionsOf(report,host.mapLabel);file=null;checks=null;checksError="";index=0;
      document.body.classList.add("pr-review-active");
      $("prReviewHeading").textContent="网格三方合并";$("prReviewContent").hidden=false;dock.hidden=!regions.length;
      $("prReviewLink").textContent=data.prs.map(p=>"#"+p.number).join(" → ");$("prReviewLink").removeAttribute("href");
      $("prReviewCounts").textContent=`${data.changed_paths.length} 个文件 · ${data.unresolved} 个未解决冲突`;
      $("prReviewRevision").textContent=`目标 ${data.branch} @ ${data.target}\n`+data.prs.map(p=>`#${p.number} 祖先 ${p.base.slice(0,8)} · PR ${p.head.slice(0,8)}`).join("\n");
      $("prReviewRegions").disabled=!regions.length;
      $("prReviewRegions").replaceChildren(...regions.map((r,i)=>new Option(`${i+1}. ${r.conflict_id?"冲突 · ":""}${r.label}`,String(i))));
      mergeViews(true);setView("result");renderChecks();
      worker?.terminate();clearTimeout(diagnosticTimer);
      if(regions.length){
        const job=new Worker(new URL("./checks-worker.js",import.meta.url),{type:"module"});worker=job;
        const finish=(result,error)=>{job.terminate();if(closed||request!==generation)return;
          clearTimeout(diagnosticTimer);worker=null;checks=result?.files || null;checksError=error || result?.error || "";renderSummary();};
        job.onmessage=({data})=>finish(data);job.onerror=e=>finish(null,e.message);
        diagnosticTimer=setTimeout(()=>finish(null,"检查超时，请重新预览"),60000);
        job.postMessage(data.files.map(f=>({before:f.current,after:f.result})));await selectRegion(0);
      }else{$("prReviewSummary").textContent="本次仅包含非网格文件变化；请检查冲突列表。";host.render();}
    },
  });

  function syncOpenSelection(value = $("prReviewInput").value) {
    const match = String(value).trim().match(/^(?:#)?(\d+)$|\/pull\/(\d+)(?:\/|$)/);
    const selected = match ? match[1] || match[2] : "";
    $("prReviewOpenSelect").value = [...$("prReviewOpenSelect").options].some(option => option.value === selected) ? selected : "";
    $("prReviewOpenSelect").title = $("prReviewOpenSelect").selectedOptions[0]?.textContent || "选择 open PR";
  }

  async function refreshOpenPRs() {
    const request = ++listGeneration;
    listController?.abort();
    listController = new AbortController();
    const select = $("prReviewOpenSelect"), message = $("prReviewListStatus");
    select.disabled = true;
    $("prReviewRefresh").disabled = true;
    select.replaceChildren(new Option("正在获取 PR 列表…", ""));
    message.textContent = "";
    try {
      const response = await fetch("/api/pr-review/open", { signal: listController.signal, cache: "no-store" });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "HTTP " + response.status);
      if (closed || request !== listGeneration) return;
      if (!Array.isArray(data.pulls)) throw new Error("PR 列表格式无效");
      select.replaceChildren(new Option(data.pulls.length ? "选择待审阅 PR…" : "当前没有 open 的 PR", ""));
      for (const pr of data.pulls) {
        const option = new Option(`#${pr.number} ${pr.title}${pr.draft ? "（草稿）" : ""}`, String(pr.number));
        option.title = option.textContent;
        select.append(option);
      }
      select.disabled = !data.pulls.length;
      message.textContent = `${data.repository} · ${data.pulls.length} 个 open PR`;
      mergeUI.offer(data.pulls);
      syncOpenSelection();
    } catch (error) {
      if (closed || request !== listGeneration || error.name === "AbortError") return;
      select.replaceChildren(new Option("列表获取失败", ""));
      message.textContent = "获取失败：" + error.message + "；可刷新或手动输入 PR。";
    } finally {
      if (request === listGeneration) $("prReviewRefresh").disabled = false;
    }
  }

  function setCollapsed(collapsed) {
    panel.classList.toggle("is-collapsed",collapsed);
    document.body.classList.toggle("pr-review-collapsed",collapsed);
    $("prReviewCollapse").setAttribute("aria-expanded",String(!collapsed));
    $("prReviewCollapse").setAttribute("aria-label",collapsed?"展开审阅面板":"收起审阅面板");
    $("prReviewCollapse").title=(collapsed?"展开":"收起")+"面板（H）";
    $("prReviewCollapse").textContent=collapsed?"›":"‹";
    host.render();
  }

  function setView(next) {
    view=next;preferences.view=view;remember();
    for(const button of dock.querySelectorAll("[data-pr-view]"))button.setAttribute("aria-pressed",String(button.dataset.prView===view));
    host.render();
  }

  function renderChecks() {
    const root=$("prReviewChecks");root.replaceChildren();
    for(const check of report.checks.filter(check=>check.status!=="pass")) {
      const item=element("div",undefined,"pr-review-callout pr-review-"+check.status);
      item.append(element("strong",check.label),element("p",check.detail));root.append(item);
    }
    const passed=report.checks.filter(check=>check.status==="pass");
    if(passed.length) {
      const details=element("details",undefined,"pr-review-passed");
      details.append(element("summary",`${passed.length} 项格式与清单检查通过`));
      for(const check of passed) {const row=element("p");row.append(element("strong",check.label),element("span",check.detail));details.append(row);}
      root.append(details);
    }
    for(const [name,changes] of Object.entries(report.json_changes))root.append(element("p",`${name}：新增 ${changes.added.length}，删除 ${changes.removed.length}，修改 ${changes.modified.length}`,"pr-review-note"));
  }

  function buildRasters() {
    raster.clear(); documents = {};
    for (const side of (report?.token ? ["ancestor", "current", "incoming", "result"] : ["before", "after"])) {
      documents[side] = snapshotDocument(file, side);
    }
    diffToken = {}; lookup = new Map();
    const chunks = new Map();
    for (const [x,z,old,value] of file.changes) {
      lookup.set(x + "," + z, [old,value]);
      const cx=Math.floor(x/256), cz=Math.floor(z/256), key=cx+","+cz;
      if (!chunks.has(key)) {
        chunks.set(key,{cx,cz,baseX:cx*256,baseZ:cz*256,pixels:[]});
      }
      const chunk=chunks.get(key), offset=((z-cz*256)*256+x-cx*256)*4;
      chunk.pixels.push([offset,TRANSITIONS[old+">"+value].color]);
    }
    diffChunks=chunks;
  }

  function syncVisible(document, transform, visible, difference) {
    const cache=raster.cacheFor(difference?diffToken:document);
    const source=difference?diffChunks:document.chunks;
    for(const [key,chunk] of source) {
      const [cx,cz]=key.split(",").map(Number),x=cx*256,z=cz*256;
      const points=[[x,z],[x+256,z],[x,z+256],[x+256,z+256]].map(([px,pz])=>[
        transform.a00*px+transform.a01*pz+transform.a02,transform.a10*px+transform.a11*pz+transform.a12]);
      const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
      if(Math.max(...xs)<visible.x||Math.min(...xs)>visible.x+visible.w||Math.max(...ys)<visible.y||Math.min(...ys)>visible.y+visible.h)continue;
      if(cache.has(key)) {const hit=cache.get(key);cache.delete(key);cache.set(key,hit);continue;}
      if(difference) {
        const canvas=element("canvas");canvas.width=canvas.height=256;
        const context=canvas.getContext("2d"),image=context.createImageData(256,256);
        for(const [offset,color] of chunk.pixels)image.data.set(color,offset);
        context.putImageData(image,0,0);cache.set(key,{...chunk,canvas});
      } else cache.set(key,raster.buildChunk(cx,cz,chunk));
    }
    // Keep at most 32 MiB of decoded review rasters, shared across the three views.
    let count=[...raster.caches.values()].reduce((n,chunks)=>n+chunks.size,0);
    for(const chunks of raster.caches.values())while(count>128 && chunks.size) {
      const key=chunks.keys().next().value;chunks.get(key).canvas.width=1;chunks.delete(key);count--;
    }
  }

  function renderSummary() {
    const summary=$("prReviewSummary");summary.replaceChildren();
    if (!file) return;
    summary.append(element("div",file.path+(file.contributors?" · PR "+file.contributors.map(n=>"#"+n).join(" + "):(file.pr?` · PR #${file.pr}`:"")),"pr-review-file"));
    const diagnostic=checks?.[regions[index]?.fileIndex];
    const metrics=element("div",undefined,"pr-review-metrics");
    const metric=(label,value)=>{const item=element("div");item.append(element("strong",value),element("span",label));metrics.append(item);};
    metric("变化格",number(file.changes.length));
    const delta=diagnostic?diagnostic.after.free-diagnostic.before.free:null;
    metric("可走格增减",delta===null?"…":(delta>0?"+":"")+number(delta));
    metric("连通块",diagnostic?`${diagnostic.before.components.length} → ${diagnostic.after.components.length}`:"…");
    summary.append(metrics);
    const attention=[];
    if(diagnostic?.after.components.length>diagnostic?.before.components.length)attention.push("连通块增加，请确认新增独立路线或断开的区域是否符合用途。");
    if(diagnostic?.after.trapped>diagnostic?.before.trapped)attention.push(`被困格增加 ${diagnostic.after.trapped-diagnostic.before.trapped}，请核对路线。`);
    if(file.floor_regions_changed)attention.push("高度区间发生变化，请核对楼层判定。");
    if(file.notice)attention.push(file.notice);
    if(attention.length) {
      const callout=element("div",undefined,"pr-review-callout pr-review-warning");
      callout.append(element("strong","待确认"),...attention.map(text=>element("p",text)));summary.append(callout);
    }
    if(!diagnostic)summary.append(element("p",checksError||"正在后台检查连通性…",checksError?"pr-review-error":"pr-review-note"));
    const legend=element("div",undefined,"pr-review-transitions");legend.id="prReviewLegend";
    for(const [key,count] of Object.entries(file.transitions)) {
      const row=element("div"),label=element("span",TRANSITIONS[key].label);
      label.style.setProperty("--transition-color",`rgb(${TRANSITIONS[key].color.slice(0,3).join(",")})`);
      row.append(label,element("b",number(count)));legend.append(row);
    }
    summary.append(legend);
    const list=element("dl");
    const row=(name,value)=>{list.append(element("dt",name),element("dd",String(value)));};
    if (!file.transitions["2>1"]) row("解除原有阻挡",0);
    row("高度区间",file.floor_regions_changed ? "发生变化，请核对" : "未变");
    row("元数据",file.metadata_changes.length ? file.metadata_changes.join("、") : "未变");
    if (diagnostic) {
      row("可走格",`${number(diagnostic.before.free)} → ${number(diagnostic.after.free)}`);
      row("孤立 / 被困",`${diagnostic.before.lone} / ${diagnostic.before.trapped} → ${diagnostic.after.lone} / ${diagnostic.after.trapped}`);
    }
    summary.append(list);
    if(file.metadata_changes.length) {
      const details=element("details"),before=element("pre",JSON.stringify(file.metadata_before,null,2)),after=element("pre",JSON.stringify(file.metadata_after,null,2));
      details.append(element("summary","查看元数据修改前 / 后"),before,after);summary.append(details);
    }
  }

  async function selectRegion(next) {
    if (!regions.length) return;
    const request=++selection, current=generation;
    index=(next+regions.length)%regions.length;
    $("prReviewRegions").value=String(index);
    $("prReviewRegionCount").textContent=`${index+1} / ${regions.length}`;
    $("prReviewHover").textContent="移动鼠标查看格子变化";
    const nextFile=report.files[regions[index].fileIndex];
    if (file !== nextFile) { file=nextFile;buildRasters(); }
    $("prReviewPrev").disabled=$("prReviewNext").disabled=regions.length<2;
    renderSummary();
    try {
      status(`正在定位 ${index+1}/${regions.length}…`);
      const ready = await host.select(file,regions[index].bounds,()=>request===selection && current===generation && !closed);
      if (request!==selection || current!==generation || closed) return;
      status(ready===false ? "当前地图没有可用底图或标定；检查摘要仍可查看" : `${report.token?"合并预览":"只读审阅"} · 变化区 ${index+1}/${regions.length}`,ready===false?"warning":"ready");
      host.render();
    } catch(error) { if (request===selection && !closed) status("定位失败："+error.message); }
  }

  async function load() {
    if (!host.canOpen()) { status("请先结束正在进行的编辑操作");return; }
    mergeUI.reset();mergeViews(false);if(!["diff","before","after"].includes(view))setView("diff");
    const request=++generation;
    controller?.abort();worker?.terminate();clearTimeout(diagnosticTimer);
    controller=new AbortController();
    $("prReviewLoad").disabled=true;$("prReviewLoad").textContent="加载中…";
    panel.setAttribute("aria-busy","true");status("正在获取 PR 与地图差异…","loading");
    try {
      const response=await fetch("/api/pr-review/load",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pr:$("prReviewInput").value.trim()}),signal:controller.signal});
      const data=await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || "读取 PR 失败");
      if (request!==generation || closed) return;
      if (!host.canOpen()) throw new Error("编辑操作尚未结束，请结束操作后重新加载");
      if (!active) { await host.activate();active=true; }
      report=data;regions=regionsOf(report,host.mapLabel);file=null;checks=null;checksError="";index=0;
      preferences.pr=String(data.number);remember();
      syncOpenSelection(data.number);
      document.body.classList.add("pr-review-active");
      $("prReviewHeading").textContent=`PR #${data.number} 审阅`;
      $("prReviewContent").hidden=false;
      dock.hidden=!regions.length;
      $("prReviewLink").textContent=`#${data.number} ${data.title}`;$("prReviewLink").href=data.url;
      $("prReviewCounts").textContent=`${data.changed_paths.length} 个文件 · ${data.files.length} 份网格 · ${regions.length} 个变化区`;
      $("prReviewRevision").textContent=`${data.repository}\n${data.base.slice(0,8)} → ${data.head.slice(0,8)}\n固定版本比较；重新加载获取最新提交。`;
      $("prReviewRegions").disabled=!regions.length;
      $("prReviewRegions").replaceChildren(...regions.map((region,i)=>{const option=element("option",`${i+1}. ${region.label}`);option.value=String(i);return option;}));
      renderChecks();
      if (regions.length) {
        const jobWorker=new Worker(new URL("./checks-worker.js",import.meta.url),{type:"module"});worker=jobWorker;
        const failed=message=>{
          jobWorker.terminate();
          if(request!==generation||closed)return;
          clearTimeout(diagnosticTimer);worker=null;checksError=message;renderSummary();
        };
        diagnosticTimer=setTimeout(()=>failed("检查超时，请重新加载后重试"),60000);
        jobWorker.onmessage=({data:result})=>{
          jobWorker.terminate();
          if (request!==generation || closed) return;
          clearTimeout(diagnosticTimer);worker=null;
          if (result.error) failed("检查失败："+result.error);
          else {checks=result.files;renderSummary();}
        };
        jobWorker.onerror=event=>failed("检查失败："+event.message);
        jobWorker.postMessage(report.files);
        await selectRegion(0);
        if(request===generation && !closed)dock.querySelector(`[data-pr-view="${view}"]`).focus({preventScroll:true});
      } else {
        raster.clear();$("prReviewSummary").textContent="没有可比较的变化网格，请查看文件检查结果。";
        status("PR 已加载，没有网格变化区");host.render();
      }
    } catch(error) {
      if (request===generation && error.name!=="AbortError") {
        status("加载失败："+error.message,"error");
        if(report && !checks) {checksError="检查已中止，请重新加载";renderSummary();}
      }
    }
    finally { if(request===generation){$("prReviewLoad").disabled=false;$("prReviewLoad").textContent="加载 ↵";panel.removeAttribute("aria-busy");} }
  }

  $("prReviewLoad").onclick=load;
  $("prReviewRefresh").onclick=refreshOpenPRs;
  $("prReviewOpenSelect").onchange=()=>{
    const value = $("prReviewOpenSelect").value;
    if (!value) return;
    $("prReviewInput").value=value;
    syncOpenSelection();
    void load();
  };
  $("prReviewInput").oninput=()=>syncOpenSelection();
  $("prReviewInput").onkeydown=event=>{if(event.key==="Enter"&&!$("prReviewLoad").disabled)void load();};
  $("prReviewRegions").onchange=event=>void selectRegion(Number(event.target.value));
  $("prReviewPrev").onclick=()=>void selectRegion(index-1);
  $("prReviewNext").onclick=()=>void selectRegion(index+1);
  $("prReviewFocus").onclick=()=>void selectRegion(index);
  const updateOpacity=()=>{
    preferences.opacity=Number($("prReviewOpacity").value);remember();
    $("prReviewOpacityValue").value=Math.round(preferences.opacity*100)+"%";host.render();
  };
  $("prReviewOpacity").oninput=updateOpacity;
  for(const button of dock.querySelectorAll("[data-pr-view]"))button.onclick=()=>setView(button.dataset.prView);
  setView(view);updateOpacity();
  $("prReviewCollapse").onclick=()=>setCollapsed(!panel.classList.contains("is-collapsed"));
  for(const surface of [panel,dock])for(const type of ["pointerdown","pointermove","pointerup","wheel","dblclick","contextmenu"])surface.addEventListener(type,event=>event.stopPropagation());
  document.addEventListener("keydown",event=>{
    if(event.defaultPrevented || document.querySelector("dialog[open]") || !active || closed || event.ctrlKey || event.metaKey || event.altKey || event.repeat)return;
    if(event.target.closest?.("input,textarea,select,[contenteditable=true]"))return;
    const key=event.key.toLowerCase();
    if(key==="j")void selectRegion(index+1);
    else if(key==="k")void selectRegion(index-1);
    else if(["1","2","3"].includes(key))setView((report?.token?{1:"diff",2:"current",3:"result"}:{1:"diff",2:"before",3:"after"})[key]);
    else if(key==="f")void selectRegion(index);
    else if(key==="h")setCollapsed(!panel.classList.contains("is-collapsed"));
    else if(key==="escape")void api.close();
    else return;
    event.preventDefault();
  });

  const api = {
    get active() { return active; },
    get report() { return report; },
    get viewportInsets() {
      return {left:panel.offsetLeft+panel.offsetWidth+16,
        bottom:dock.hidden?16:dock.offsetHeight+32};
    },
    open() {
      mergeUI.open();
      closed=false;panel.hidden=false;setCollapsed(false);document.body.classList.add("pr-review-open");
      if(!active){$("prReviewContent").hidden=true;$("prReviewHeading").textContent="数据 PR 审阅";status("输入 PR，开始地图审阅。");}
      $("prReviewInput").focus();$("prReviewInput").select();
      void refreshOpenPRs();
    },
    async close() {
      mergeUI.reset(true);mergeViews(false);
      closed=true;generation++;selection++;controller?.abort();worker?.terminate();worker=null;clearTimeout(diagnosticTimer);
      listGeneration++;listController?.abort();
      panel.hidden=true;dock.hidden=true;$("prReviewLoad").disabled=false;$("prReviewLoad").textContent="加载 ↵";panel.removeAttribute("aria-busy");
      document.body.classList.remove("pr-review-active","pr-review-open","pr-review-collapsed");
      const restore=active;active=false;raster.clear();file=null;report=null;checks=null;
      if (restore) await host.close();host.render();host.returnFocus?.();
    },
    draw(context, visible, activeKey, transformFor) {
      if (!active || !file || file.map_category==="independent" || `${file.map}@${file.zoom}`!==activeKey) return;
      const document = view==="diff" ? {origin:file.origin,cellSize:file.cellSize} : documents[view];
      if (!document || (view==="diff"&&!file.comparable)) return;
      const transform=transformFor(document);if(!transform)return;
      syncVisible(document,transform,visible,view==="diff");
      context.save();context.globalAlpha=Number($("prReviewOpacity").value);
      raster.drawAll(context, view==="diff"?diffToken:document,transform,visible);
      context.restore();
      const conflict=mergeUI.conflict;
      if(conflict?.bounds && conflict.path===file.path){
        const t=transformFor({origin:file.origin,cellSize:file.cellSize});
        if(t){
          const b=conflict.bounds,points=[[b.x0,b.z0],[b.x1,b.z0],[b.x1,b.z1],[b.x0,b.z1]].map(([x,z])=>{
            const ix=(x-file.origin[0])/file.cellSize,iz=(z-file.origin[1])/file.cellSize;
            return [t.a00*ix+t.a01*iz+t.a02,t.a10*ix+t.a11*iz+t.a12];
          });
          context.save();context.strokeStyle="#ffb94e";context.lineWidth=2;context.setLineDash([5,3]);context.beginPath();
          points.forEach(([x,z],i)=>i?context.lineTo(x,z):context.moveTo(x,z));context.closePath();context.stroke();context.restore();
        }
      }
    },
    hover(x,z) {
      if(!active||!file)return;
      if(report?.token){
        const states=[["ancestor","祖先"],["current","主线"],["incoming","PR"],["result","结果"]].map(([side,label])=>{
          const doc=documents[side],snapshot=file[side];
          if(!doc)return label+" 无网格";
          const ix=Math.floor((x-doc.origin[0])/doc.cellSize),iz=Math.floor((z-doc.origin[1])/doc.cellSize);
          return label+" "+(ix<0||iz<0||ix>=snapshot.shape[1]||iz>=snapshot.shape[0]?"范围外":STATE_NAMES[doc.getCell(ix,iz)]);
        });
        $("prReviewHover").textContent=`X ${x.toFixed(2)} · Z ${z.toFixed(2)} · ${states.join(" / ")}`;return;
      }
      const transition=transitionAt(file,x,z,lookup);
      $("prReviewHover").textContent=`X ${x.toFixed(2)} · Z ${z.toFixed(2)}${transition ? " · "+STATE_NAMES[transition[0]]+" → "+STATE_NAMES[transition[1]] : " · 无逐格变化"}`;
    },
  };
  $("prReviewClose").onclick=()=>void api.close();
  return api;
}

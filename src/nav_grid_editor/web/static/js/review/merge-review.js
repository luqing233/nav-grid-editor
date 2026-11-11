// Merge requests are isolated from the editor document and bound to a session.
export function initMergeReview(panel, callbacks) {
  const launch = document.createElement("details");
  launch.className = "pr-merge-launch";
  launch.innerHTML = `<summary>选择多个 PR · 联合预览</summary>
    <div id="prMergeChoices" role="group" aria-label="选择要整合的 PR"></div>
    <label for="prMergeQueue">PR 队列 · 按依赖顺序</label>
    <div class="pr-review-input-row"><input id="prMergeQueue" placeholder="勾选 PR，或输入 4, 5" autocomplete="off"><button id="prMergePrepare" type="button">联合预览</button></div>
    <p>留空使用当前 PR。可导出结果，或提交到独立分支并创建集成 PR。</p>
    <div id="prMergeStatus" role="status"></div>`;
  panel.querySelector(".pr-review-loader").append(launch);
  const controls = document.createElement("section");
  controls.className = "pr-merge-controls";controls.hidden=true;
  controls.innerHTML = `<strong id="prMergeSummary"></strong><p id="prMergeProgress"></p>
    <label for="prMergeConflict">冲突区域 / 元数据</label><select id="prMergeConflict"></select>
    <pre id="prMergeValues"></pre>
    <div class="pr-merge-choices"><button type="button" data-merge-choice="current">采用当前主线</button><button type="button" data-merge-choice="incoming">采用此 PR</button><button type="button" data-merge-choice="base">恢复共同祖先</button></div>
    <div class="pr-merge-choices"><button id="prMergePublish" type="button">提交并推送，创建集成 PR</button><button id="prMergeExport" type="button">导出合并结果</button><button id="prMergeBack" type="button">返回 PR 审阅</button></div>
    <p id="prMergePublishTarget"></p>`;
  panel.querySelector(".pr-review-scroll").prepend(controls);
  const $ = id => panel.querySelector("#"+id);
  let data=null, request=0, controller=null, busy=false;
  const publication=document.createElement("section");publication.className="pr-merge-publication";publication.hidden=true;
  publication.innerHTML='<strong>集成 PR</strong><p id="prPublishStatus" role="status"></p><a id="prPublishLink" target="_blank" rel="noreferrer" hidden>打开集成 PR ↗</a><button id="prPublishRetry" type="button" hidden>重试发布</button><button id="prPublishMerge" type="button" hidden>合入目标分支</button>';
  launch.after(publication);
  let publishToken="",publishState=null,publishTimer=null,publishRequest=0;
  const publishKey="nav-grid-editor.pr-publication.v1";
  try{publishToken=sessionStorage.getItem(publishKey)||"";}catch{/* Optional recovery. */}
  const phases={idle:"准备发布",checking:"检查账号权限与远端版本",validating:"检查合并结果",committing:"创建合并提交",lfs:"同步 Git LFS 对象",pushing:"推送集成分支",creating_pr:"创建集成 PR",complete:"集成 PR 已创建，等待你确认合入",failed:"发布失败",merging:"正在合入目标分支",merged:"已合入目标分支"};
  function showPublication(state){
    publishState=state;publication.hidden=false;
    $("prPublishStatus").textContent=`${phases[state.phase]||state.phase}${state.error?"："+state.error:""}\n${state.branch||""}${state.pushed&&!state.url?" · 分支已推送，重试会继续创建 PR":""}${state.merge_error?"\n未合入："+state.merge_error:""}${state.merge_sha?"\n合并提交 "+state.merge_sha.slice(0,8):""}`;
    $("prPublishLink").hidden=!state.url;
    if(state.url){$("prPublishLink").href=state.url;$("prPublishLink").textContent=`打开集成 PR #${state.number} ↗`;}
    $("prPublishRetry").hidden=state.running || state.phase!=="failed";
    $("prPublishMerge").hidden=!state.url || !!state.merged;
    $("prPublishMerge").disabled=!!state.running;
    $("prPublishMerge").textContent="确认合入 "+(state.base||"目标分支");
    lock(busy);
  }
  async function publish(action="publish",savedToken=""){
    const token=savedToken || (action==="publish"&&data&&!publishState?.running?data.token:publishToken);
    if(!token)return;
    if(token!==publishToken)publishState=null;
    publishToken=token;try{sessionStorage.setItem(publishKey,token);}catch{/* Optional recovery. */}
    const id=++publishRequest;clearTimeout(publishTimer);
    if(action==="publish" || action==="merge_to_base")showPublication({...publishState,running:true,phase:action==="publish"?"checking":"merging",error:"",merge_error:""});
    try{
      const response=await fetch("/api/pr-review/merge",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({action,token})});
      const state=await response.json();if(id!==publishRequest)return;
      if(!response.ok || !state.ok)throw new Error(state.error||"发布请求失败");
      showPublication(state);
      if(state.running)publishTimer=setTimeout(()=>void publish("publication_status"),1200);
    }catch(error){if(id===publishRequest)showPublication({...publishState,running:false,...(action==="merge_to_base"?{phase:"complete",merge_error:error.message}:{phase:"failed",error:error.message})});}
  }
  const message=text=>{$("prMergeStatus").textContent=text;};
  function lock(value) {
    busy=value;
    $("prMergePrepare").disabled=value;
    $("prMergeQueue").disabled=value;
    for(const box of $("prMergeChoices").querySelectorAll("input"))box.disabled=value;
    $("prMergeExport").disabled=value || !data?.ready || (data.token===publishToken && !!publishState?.running);
    const frozen=data?.token===publishToken && publishState;
    $("prMergePublish").disabled=value || !data?.ready || !!publishState?.running || !!frozen;
    $("prMergeConflict").disabled=value || !data?.conflicts.length;
    for(const button of controls.querySelectorAll("[data-merge-choice]"))button.disabled=value || !data?.conflicts.length || !!frozen;
  }
  function conflict() {return data?.conflicts.find(item=>item.id===$("prMergeConflict").value) || null;}
  function showConflict(focus=true) {
    const item=conflict();
    $("prMergeValues").textContent=item ? (item.kind==="cells"
      ? `${item.path}\n${item.count} 格 · X ${item.bounds.x0}…${item.bounds.x1} · Z ${item.bounds.z0}…${item.bounds.z1}\n${item.choice ? "已选择："+({current:"当前主线",incoming:"此 PR",base:"共同祖先"}[item.choice]) : "待解决；预览暂用当前主线"}`
      : `${item.path}\n${item.label}\n`+[ ["base","共同祖先"],["current","当前主线"],["incoming","此 PR"] ].map(([key,label])=>label+"："+(item.absent?.includes(key)?"不存在（删除）":JSON.stringify(item[key]).slice(0,1400))).join("\n")) : "没有需要处理的冲突。";
    for(const button of controls.querySelectorAll("[data-merge-choice]"))button.setAttribute("aria-pressed",String(button.dataset.mergeChoice===item?.choice));
    if(focus && item)callbacks.focus(item);
    callbacks.render();
  }
  async function send(action, choices) {
    if(busy)return;
    const id=++request;controller?.abort();controller=new AbortController();lock(true);
    if(action==="prepare"){data=null;controls.hidden=true;callbacks.begin();}
    message(action==="export"?"正在校验最新版本和连接端点…":"正在计算三方合并…");
    try {
      const values=$("prMergeQueue").value.trim();
      const prs=values?values.split(/[,，\s]+/).filter(Boolean):[panel.querySelector("#prReviewInput").value.trim()];
      const response=await fetch("/api/pr-review/merge",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({action,prs,token:data?.token,choices}),signal:controller.signal});
      if(!response.ok){const error=await response.json();throw new Error(error.error || "合并失败");}
      if(action==="export") {
        const blob=await response.blob();if(id!==request)return;
        const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="grid-merge.zip";a.click();
        setTimeout(()=>URL.revokeObjectURL(url),1000);
        message("已导出合并文件和版本清单；当前工作区未改变。");return;
      }
      const next=await response.json();if(id!==request)return;
      const previous=$("prMergeConflict").value;data=next;controls.hidden=false;
      panel.classList.add("is-merging");launch.open=false;
      launch.querySelector("summary").textContent="合并队列 "+data.prs.map(p=>"#"+p.number).join(" → ")+" · 调整";
      $("prMergeSummary").textContent=data.ready?"联合预览就绪 · 可以发布或导出":`${data.unresolved} 个冲突待解决`;
      $("prMergeProgress").textContent=`${data.branch} @ ${data.target.slice(0,8)} · 已处理 ${data.completed.length}/${data.prs.length} 个 PR${data.ready?"":` · 当前 #${data.active_pr}`}`;
      $("prMergePublishTarget").textContent=`将 ${data.prs.map(p=>"#"+p.number).join("、")} 提交到 ${data.repository} 的新分支，并向 ${data.branch} 创建 PR。`;
      $("prMergeConflict").replaceChildren(...data.conflicts.map(c=>new Option(`${c.choice?"✓":"待处理"} #${c.pr} · ${c.path} · ${c.label}`,c.id)));
      $("prMergeConflict").value=data.conflicts.find(c=>!c.choice)?.id || (data.conflicts.some(c=>c.id===previous)?previous:data.conflicts[0]?.id) || "";
      $("prMergeConflict").hidden=!data.conflicts.length;
      controls.querySelector('label[for="prMergeConflict"]').hidden=!data.conflicts.length;
      controls.querySelector(".pr-merge-choices").hidden=!data.conflicts.length;
      await callbacks.result(data);if(id!==request)return;
      showConflict();message(data.ready?"合并预览已完成；导出时会再次检查远端版本和连接端点。":"选择冲突区域后确认采用哪一版。未解决冲突会暂停后续 PR。");
    } catch(error) {if(id===request && error.name!=="AbortError")message("失败："+error.message);}
    finally {if(id===request)lock(false);}
  }
  $("prMergePrepare").onclick=()=>void send("prepare");
  $("prMergeExport").onclick=()=>void send("export");
  $("prMergePublish").onclick=()=>void publish();
  $("prPublishRetry").onclick=()=>void publish("publish",publishToken);
  $("prPublishMerge").onclick=()=>void publish("merge_to_base",publishToken);
  $("prMergeBack").onclick=()=>callbacks.exit();
  $("prMergeConflict").onchange=()=>showConflict();
  for(const button of controls.querySelectorAll("[data-merge-choice]"))button.onclick=()=>{
    const item=conflict();if(item)void send("resolve",{[item.id]:button.dataset.mergeChoice});
  };
  const queueValues=()=>$("prMergeQueue").value.trim().split(/[,，\s]+/).filter(Boolean);
  function changedQueue(){
    const selected=queueValues();
    for(const box of $("prMergeChoices").querySelectorAll("input"))box.checked=selected.includes(box.value);
    if(data){data=null;controls.hidden=true;panel.classList.remove("is-merging");callbacks.begin();}
    launch.open=true;lock(busy);message("已选择 "+selected.length+" 个 PR，点击联合预览。");
  }
  $("prMergeQueue").oninput=changedQueue;
  return {
    offer(pulls){
      const selected=queueValues();$("prMergeChoices").replaceChildren();
      for(const pr of [...pulls].sort((a,b)=>a.number-b.number)){
        const label=document.createElement("label"),box=document.createElement("input"),text=document.createElement("span");
        box.type="checkbox";box.value=String(pr.number);box.checked=selected.includes(box.value);box.disabled=busy;
        box.setAttribute("aria-label","合并 PR #"+pr.number);
        text.textContent=`#${pr.number} ${pr.title}${pr.draft?"（草稿）":""}`;
        box.onchange=()=>{const values=queueValues().filter(v=>v!==box.value);if(box.checked)values.push(box.value);$("prMergeQueue").value=values.join(", ");changedQueue();};
        label.append(box,text);$("prMergeChoices").append(label);
      }
      if(pulls.length>1&&!data)launch.open=true;
    },
    open(){if(publishToken)void publish("publication_status");},
    get conflict(){return conflict();},
    reset(pausePublication=false){request++;controller?.abort();if(pausePublication){clearTimeout(publishTimer);publishRequest++;}data=null;controls.hidden=true;panel.classList.remove("is-merging");
      launch.querySelector("summary").textContent="选择多个 PR · 联合预览";lock(false);message("");},
  };
}

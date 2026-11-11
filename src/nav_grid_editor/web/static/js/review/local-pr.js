// Saved working data -> reviewed snapshot -> isolated branch and regular PR.
export function initLocalPR({hasUnsaved=()=>false, saveAll=async()=>{}}={}) {
  const dialog=document.createElement("dialog");
  dialog.id="localPRDialog";dialog.setAttribute("aria-labelledby","localPRTitle");
  dialog.innerHTML=`<header class="dialog-heading"><h2 id="localPRTitle">新建数据 PR</h2><button id="localPRClose" type="button" aria-label="关闭新建 PR">×</button></header>
    <div class="local-pr-body">
      <p>提交本地已保存的导航网格、连接和层声明。选择文件、检查后再发布。</p>
      <p id="localPRUnsaved" hidden>还有未保存的网格；请先保存。<button id="localPRSave" type="button">保存全部网格</button></p>
      <label for="localPRAccount">提交账号</label><div class="pr-review-input-row"><select id="localPRAccount"><option value="">请选择账号</option></select><button id="localPRRefresh" type="button">刷新账号</button></div>
      <details><summary>账号从哪里来？</summary><p>列出本机 GitHub CLI 已登录的 github.com 账号。需要新增账号时，在终端执行：</p><code>gh auth login --hostname github.com --web</code><p>完成登录后点击“刷新账号”。所选账号用于推送、创建 PR 和提交作者（GitHub 隐私邮箱），不会切换全局默认账号。需要对数据仓库有推送权限；本入口暂不自动创建 fork。凭证不会发送给网页或写入发布文件。</p></details>
      <label for="localPRInputTitle">PR 标题</label><input id="localPRInputTitle" maxlength="160" value="feat(data): 更新导航网格">
      <div class="local-pr-check"><button id="localPRPrepare" type="button">检查已保存修改</button><span id="localPRTarget"></span></div>
      <div id="localPRFiles" role="group" aria-label="选择要提交的导航文件"></div>
      <p id="localPRStatus" role="status"></p>
      <div id="localPRJob" hidden><p id="localPRJobStatus" role="status"></p><a id="localPRLink" target="_blank" rel="noreferrer" hidden></a><button id="localPRRetry" type="button" hidden>重试发布</button></div>
    </div><footer class="dialog-actions"><button id="localPRPublish" type="button" disabled>提交并推送，创建 PR</button></footer>`;
  document.body.append(dialog);
  const $=id=>dialog.querySelector("#"+id);
  let preview=null, paths=null, busy=false, job=null, timer=null, serial=0;
  const phases={idle:"等待发布",checking:"检查账号与版本",validating:"校验网格与连接",committing:"生成提交",lfs:"同步 Git LFS 数据",pushing:"推送分支",creating_pr:"创建 PR",complete:"PR 已创建",failed:"发布失败"};
  const key="nav-grid-editor.local-pr.v1";
  const status=text=>{$("localPRStatus").textContent=text;};
  function lock(value) {
    busy=value;const frozen=!!job?.running;
    for(const id of ["localPRAccount","localPRRefresh","localPRPrepare","localPRInputTitle","localPRSave"])$(id).disabled=value||frozen;
    for(const box of $("localPRFiles").querySelectorAll("input"))box.disabled=value||frozen;
    $("localPRPublish").disabled=value||frozen||!preview||!$("localPRInputTitle").value.trim()||hasUnsaved();
    $("localPRUnsaved").hidden=!hasUnsaved();
    $("localPRRetry").disabled=value||frozen;
  }
  async function request(body) {
    const response=await fetch("/api/local-pr",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||"请求失败");return data;
  }
  function showJob(data) {
    job=data;$("localPRJob").hidden=false;
    $("localPRJobStatus").textContent=`${phases[data.phase]||data.phase} · ${data.account}\n${data.branch}${data.error?"\n"+data.error:""}`;
    $("localPRLink").hidden=!data.url;if(data.url){$("localPRLink").href=data.url;$("localPRLink").textContent=`打开 PR #${data.number} ↗`;}
    $("localPRRetry").hidden=data.running||data.phase!=="failed";
    if(data.id)try{sessionStorage.setItem(key,data.id);}catch{/* optional recovery */}
    clearTimeout(timer);if(data.running&&dialog.open)timer=setTimeout(()=>void poll(data.id),1500);
    lock(busy);
  }
  async function poll(id) {try{showJob(await request({action:"status",id}));}catch(error){status("无法读取发布状态："+error.message);}}
  async function accounts() {
    lock(true);status("正在读取本机 GitHub 账号…");
    const previous=$("localPRAccount").value;
    try {
      const response=await fetch("/api/local-pr/accounts"),data=await response.json();
      if(!response.ok||!data.ok)throw new Error(data.error||"读取账号失败");
      $("localPRAccount").replaceChildren(new Option("请选择提交账号",""));
      for(const account of data.accounts){const option=new Option(`${account.login}${account.active?" · 当前默认":""}${account.available?"":" · 需重新验证登录"}`,account.login);$("localPRAccount").append(option);}
      $("localPRAccount").value=data.accounts.some(a=>a.login===previous)?previous:(data.accounts.find(a=>a.active)?.login||"");
      preview=null;paths=null;$("localPRFiles").replaceChildren();$("localPRTarget").textContent="";
      status(data.accounts.length?"选择账号后，检查要提交的文件。":"未发现已登录账号，请按账号说明登录后刷新。");
    } catch(error){status(error.message);}finally{lock(false);}
  }
  async function prepare() {
    if(busy||job?.running)return;
    if(hasUnsaved()){status("请先保存全部网格，再检查修改。");lock(false);return;}
    if(!$("localPRAccount").value){status("请选择提交账号。");return;}
    const id=++serial;preview=null;lock(true);status("正在检查保存的文件、账号权限和远端版本…");
    try {
      const data=await request({action:"prepare",account:$("localPRAccount").value,...(paths?{paths}: {})});
      if(id!==serial)return;preview=data;paths=data.files.map(f=>f.path);
      $("localPRFiles").replaceChildren();
      for(const file of data.files){const label=document.createElement("label"),box=document.createElement("input"),text=document.createElement("span");
        box.type="checkbox";box.checked=true;box.value=file.path;box.setAttribute("aria-label","提交 "+file.path);
        text.textContent=`${file.operation} · ${file.path}${file.changes===null?"":` · ${file.changes.toLocaleString()} 个变化格`}`;
        box.onchange=()=>{paths=[...$("localPRFiles").querySelectorAll("input:checked")].map(b=>b.value);preview=null;status("文件选择已改变，请重新检查。");lock(false);};
        label.append(box,text);$("localPRFiles").append(label);
      }
      $("localPRTarget").textContent=`${data.repository} → ${data.base} · 账号 ${data.account}`;
      status(`已检查 ${data.files.length} 份修改。发布时自动更新 dataset.json 校验值；只创建 PR，之后可进入审阅。`);
    }catch(error){if(id===serial)status(error.message);}finally{if(id===serial)lock(false);}
  }
  async function publish(retry=false) {
    if(busy||job?.running||(!retry&&!preview))return;
    if(!retry&&hasUnsaved()){preview=null;status("出现了未保存网格，请保存后重新检查。");lock(false);return;}
    lock(true);
    try{const data=await request(retry?{action:"retry",id:job.id}:{action:"publish",token:preview.token,account:$("localPRAccount").value,title:$("localPRInputTitle").value});preview=null;showJob(data);}
    catch(error){status(error.message);}finally{lock(false);}
  }
  $("localPRClose").onclick=()=>dialog.close();dialog.addEventListener("close",()=>clearTimeout(timer));
  $("localPRRefresh").onclick=()=>void accounts();$("localPRPrepare").onclick=()=>void prepare();
  $("localPRPublish").onclick=()=>void publish();$("localPRRetry").onclick=()=>void publish(true);
  $("localPRAccount").onchange=()=>{preview=null;paths=null;$("localPRFiles").replaceChildren();$("localPRTarget").textContent="";status("账号已改变，请重新检查修改。");lock(false);};
  $("localPRInputTitle").oninput=()=>lock(false);
  $("localPRSave").onclick=async()=>{lock(true);try{await saveAll();preview=null;paths=null;status("已保存全部网格，请检查修改。");}catch(error){status(error.message);}finally{lock(false);}};
  const open=async()=>{if(!dialog.open)dialog.showModal();if(busy)return;lock(false);let id=job?.id;try{id ||= sessionStorage.getItem(key);}catch{/* optional */}if(id)await poll(id);if(!job?.running)await accounts();};
  document.getElementById("btnLocalPR").onclick=()=>void open();
  return {open};
}

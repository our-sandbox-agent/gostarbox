import './style.css';
import './brand.css';
import { createFileDeleter, deleteStoredFile } from './file-deletion.js';
import { planUpload, findConflicts, writeFiles, createUploadWriter } from './file-upload.js';
import { normalizePolicy, restoreClock, deadline, advance, setState, recordActivity } from './lifecycle.js';
import { readState, createTabSync } from './tab-sync.js';
const icons={box:'<path d="m12 3 9 5v9l-9 5-9-5V8zm0 0v10m-9-5 9 5 9-5m-9 5v9"/>',grid:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',terminal:'<path d="m5 6 6 6-6 6m9 0h6"/>',folder:'<path d="M3 7V5h7l2 3h9v12H3z"/>',chart:'<path d="M4 3v18h17M8 16v-5m5 5V7m5 9V4"/>',book:'<path d="M4 4h7l1 2 1-2h7v16h-7l-1 1-1-1H4zM12 6v15"/>',plus:'<path d="M12 5v14M5 12h14"/>',search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',upload:'<path d="M12 16V3m-5 5 5-5 5 5M3 15v6h18v-6"/>',copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v12h5"/>',play:'<path d="m8 4 12 8-12 8z"/>',pause:'<path d="M8 4v16M16 4v16"/>',arrow:'<path d="m8 5 7 7-7 7"/>',check:'<path d="m5 12 4 4L19 6"/>'};
const icon=(n)=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[n]||icons.box}</svg>`;
const brandMark='<svg class="brand-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true"><rect x="1" y="1" width="38" height="38" rx="11" fill="#c1ef8d"/><ellipse cx="20" cy="20" rx="16" ry="7" transform="rotate(-35 20 20)" stroke="#526b43" stroke-width="1.2"/><path d="M14 27V13L26 27V13" stroke="#21341f" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/><circle cx="32" cy="11" r="2.5" fill="#21341f"/></svg>';
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const rates={Active:.12,Idle:.03,Suspend:.005};
const competitors=[['本產品 (示意)',null],['E2B (reserved)',0.1656],['Daytona',0.1656],['Fly Sprites',0.315],['Modal',0.3808],['Vercel (active CPU + mem)',0.3408]];
let policy;try{policy=JSON.parse(localStorage.getItem('sandbox-policy'))||{};}catch{policy={};}policy=normalizePolicy(policy);
const savePolicy=()=>{try{localStorage.setItem('sandbox-policy',JSON.stringify(policy));}catch{}};
const fmt=n=>{n=Math.max(0,Math.round(n));return Math.floor(n/60)+':'+String(n%60).padStart(2,'0');};
const seed=()=>[
{id:'sbx_8f2a1',name:'frontend-playground',agent:'Claude',status:'Active',cpu:2,created:Date.now(),seconds:{Active:1240,Idle:460,Suspend:0},logs:['Sandbox initialized · Ubuntu 24.04','Workspace mounted at /workspace','Claude session ready. Waiting for a task.']},
{id:'sbx_9c3b2',name:'api-experiments',agent:'Codex',status:'Idle',cpu:4,created:Date.now(),seconds:{Active:780,Idle:2220,Suspend:0},logs:['Codex session ready.','Task finished. Sandbox is idle.']},
{id:'sbx_2d4e3',name:'research-agent',agent:'Harness',status:'Suspend',cpu:2,created:Date.now(),seconds:{Active:340,Idle:120,Suspend:5800},logs:['Harness session initialized.','Sandbox suspended. Workspace preserved (demo).']}
];
const storedState=readState(localStorage,'sandbox-v1');
let boxes=storedState?.boxes;
if(!Array.isArray(boxes)||!boxes.length||!boxes.every(b=>rates[b.status]!==undefined&&b.seconds&&b.logs))boxes=seed();
boxes.forEach(b=>{restoreClock(b,Date.now());b.snapshots||=[];b.repo||='';});
const touch=b=>recordActivity(b,Date.now(),policy);
let selected=boxes[0].id,view='sandboxes',filter='All',query='',tab='terminal',agent='Claude',files=[],fileError='';
const sync=createTabSync({storage:localStorage,key:'sandbox-v1',revision:storedState?.revision||0,get:()=>boxes,adopt:next=>{boxes=next;boxes.forEach(b=>{b.snapshots||=[];b.repo||='';});}});
const reselect=()=>{if(!boxes.some(b=>b.id===selected))selected=boxes[0]?.id;};
const refresh=()=>{if(sync.refresh()){reselect();return true;}return false;};
const save=()=>{tick(false);try{if(!sync.write()){reselect();render();}}catch{toast('瀏覽器儲存空間不足，變更只保留至此次頁面關閉。');}};
const current=()=>boxes.find(b=>b.id===selected)||boxes[0];
const money=n=>'$'+n.toFixed(4);
const cost=b=>Object.entries(b.seconds).reduce((s,[k,v])=>s+v/3600*rates[k]*(b.cpu/2),0);
const badge=s=>`<span class="badge ${s.toLowerCase()}"><i></i>${s}</span>`;
function toast(t){document.querySelector('#toast').textContent=t;document.querySelector('#toast').classList.add('show');clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>document.querySelector('#toast').classList.remove('show'),3200);}
const dbPromise=new Promise((resolve,reject)=>{const req=indexedDB.open('sandbox-files',1);req.onupgradeneeded=()=>req.result.createObjectStore('files',{keyPath:'key'});req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});
async function fileOp(mode,fn){const db=await dbPromise;return new Promise((resolve,reject)=>{const tx=db.transaction('files',mode);const request=fn(tx.objectStore('files'));tx.oncomplete=()=>resolve(request?.result);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});}
const currentKeys=()=>fileOp('readonly',s=>s.getAllKeys());
async function readFiles(){try{files=await fileOp('readonly',s=>s.getAll());fileError='';}catch{fileError='無法開啟本機檔案儲存空間。';}render();}
function render(){const b=current();document.querySelector('#app').innerHTML=`
<aside><a class="brand" href="#" aria-label="NexSpace 星際工作站首頁">${brandMark}<span class="brand-copy"><b>NexSpace</b><small>星際工作站</small></span></a><div class="workspace"><span class="workspace-mark">O</span><span>Our workspace<small>Personal workspace</small></span><span class="chevron">⌄</span></div><div class="nav-label">WORKSPACE</div><nav>${[['sandboxes','grid','Sandboxes'],['files','folder','Files'],['usage','chart','Usage']].map(([v,i,t])=>`<button data-view="${v}" class="${view===v?'selected':''}">${icon(i)}${t}${v==='sandboxes'?`<span class="nav-count">${boxes.length}</span>`:''}</button>`).join('')}</nav><div class="aside-bottom"><div class="demo-card"><span class="small-tag">NEXSPACE / PREVIEW</span><strong>下一個想法，準備啟航。</strong><p>探索 Agent 的獨立工作空間。</p><div><i></i> 模擬環境 · 無雲端費用</div></div><button class="docs" data-view="guide">${icon('book')}Quick start<span>↗</span></button><div class="profile"><span class="avatar">O</span><div>NexSpace<small>Developer preview</small></div><span class="version">v0.1</span></div></div></aside>
<div class="main-shell"><header><div class="breadcrumbs">Workspace ${icon('arrow')} <strong>${{sandboxes:'Sandboxes',files:'Files',usage:'Usage',guide:'Quick start'}[view]}</strong></div><div class="header-right"><span class="preview-pill">互動雛形 · 本機模擬</span><span class="avatar small">O</span></div></header><main>
${view==='sandboxes'?`<div class="page-title"><div><div class="eyebrow">NEXSPACE / AGENT WORKSPACE</div><h1>星際工作站<span class="title-dot">.</span></h1><p>從這裡，展開你的下一個任務。</p></div><button class="primary" data-create>${icon('plus')}New sandbox</button></div>
<section class="launch"><div><div class="launch-label">OUR VISION / 產品願景</div><h2>關上筆電，任務繼續。</h2><p>給 AI Agent 一個獨立工作空間。</p><div class="agent-switch">${['Claude','Codex','Harness'].map(a=>`<button data-agent="${a}" class="${agent===a?'on':''}"><span class="agent-symbol ${a.toLowerCase()}">${a==='Claude'?'✳':a==='Codex'?'⌘':'H'}</span>${a}</button>`).join('')}</div></div><div class="command-area"><div class="terminal-caption"><span>QUICK LAUNCH</span><span>模擬指令</span></div><div class="command"><span class="prompt">$</span><code>sandbox ${agent.toLowerCase()}</code><button class="icon-button" data-copy aria-label="複製啟動指令">${icon('copy')}</button></div><div class="command-foot"><span>選擇 Agent，體驗啟動流程。</span><button data-launch>試用雛形 ${icon('arrow')}</button></div></div><div class="orbit" aria-hidden="true">${brandMark}</div></section>
<div class="prototype-note"><span class="prototype-tag">目前為互動雛形</span><p>只在此瀏覽器模擬操作；不會執行真實 Agent、建立雲端資源或收費。關閉頁面後，模擬計時會暫停。</p></div>
<div class="stats">${['Active','Idle','Suspend'].map(s=>`<div><span class="stat-label">${badge(s)}<small>${s==='Active'?'Agent 執行中':s==='Idle'?'已就緒，等待工作':'已掛起，保留工作區'}</small></span><div class="stat-bottom"><strong>${boxes.filter(b=>b.status===s).length.toString().padStart(2,'0')}</strong><span>${money(rates[s])}<small> / hr · 示意起價</small></span></div></div>`).join('')}</div>
<section class="sandboxes-section"><div class="section-head"><div class="tabs">${['All','Active','Idle','Suspend'].map(f=>`<button data-filter="${f}" class="${filter===f?'on':''}">${f==='All'?'All sandboxes':f}${f==='All'?`<span>${boxes.length}</span>`:''}</button>`).join('')}</div><label class="search">${icon('search')}<input id="search" aria-label="搜尋沙盒" placeholder="Search sandboxes…" value="${esc(query)}"></label></div><div class="table-scroll"><table><thead><tr><th>SANDBOX</th><th>STATUS</th><th>RESOURCES</th><th>SESSION COST <span title="示意費率，非實際帳單">ⓘ</span></th><th></th></tr></thead><tbody>${boxes.filter(x=>(filter==='All'||x.status===filter)&&x.name.toLowerCase().includes(query.toLowerCase())).map(x=>`<tr class="${selected===x.id?'chosen':''}"><td><button class="name-button" data-select="${x.id}"><span class="box-icon">${icon('box')}</span><span><strong>${esc(x.name)}</strong><small>${esc(x.id)}<span class="separator">/</span>${esc(x.agent)}</small></span></button></td><td>${badge(x.status)}</td><td><span class="resource">${x.cpu} vCPU <span>·</span> ${x.cpu*2} GB</span></td><td class="mono" data-cost="${x.id}">${money(cost(x))}</td><td><button class="icon-button" data-select="${x.id}" aria-label="開啟 ${esc(x.name)}">${icon('arrow')}</button></td></tr>`).join('')||'<tr><td colspan="5" class="empty">找不到符合的沙盒。</td></tr>'}</tbody></table></div></section>${detail(b)}`:view==='files'?`<div class="page-title"><div><div class="eyebrow">BRING YOUR WORK ALONG.</div><h1>Workspace files<span class="title-dot">.</span></h1><p>檔案與資料夾保存在此瀏覽器，不會傳送到伺服器。</p></div></div><label class="sandbox-picker">Workspace <select id="file-box">${boxes.map(x=>`<option value="${x.id}" ${x.id===selected?'selected':''}>${esc(x.name)}</option>`).join('')}</select></label>${filePanel(b)}`:view==='usage'?usage():guide()}
<footer><span>${icon('box')} NexSpace · 星際工作站</span><span>Browser-local demo <span class="separator">/</span> 所有費率與執行紀錄皆為示意</span></footer></main></div>`;
bind();}
function detail(b){return `<section class="detail"><div class="detail-head"><div><span class="tiny-dot"></span><strong>${esc(b.name)}</strong><span class="detail-path">/workspace</span>${b.repo?`<span class="detail-path">· ${esc(b.repo.replace(/^https?:\/\/(www\.)?/,''))}</span>`:''}${b.status==='Active'?`<span class="auto-hint" data-countdown="${b.id}">auto-idle in ${fmt((deadline(b,policy)-Date.now())/1000)}</span>`:b.status==='Idle'?`<span class="auto-hint" data-countdown="${b.id}">auto-suspend in ${fmt((deadline(b,policy)-Date.now())/1000)}</span>`:''}</div><div class="actions"><button class="subtle" data-snapshot ${b.status==='Suspend'?'disabled':''}>${icon('copy')}Snapshot</button>${b.status==='Active'?'<button class="subtle" data-state="Idle">Set idle</button>':''}${b.status!=='Active'?`<button class="subtle" data-state="Active">${icon('play')}Resume</button>`:''}${b.status!=='Suspend'?`<button class="subtle" data-state="Suspend">${icon('pause')}Suspend</button>`:''}</div></div><div class="detail-tabs">${[['terminal','terminal','Terminal'],['files','folder','Files'],['activity','chart','Activity']].map(([v,i,t])=>`<button data-tab="${v}" class="${tab===v?'on':''}">${icon(i)}${t}</button>`).join('')}<span>Demo session</span></div>${tab==='terminal'?`<div class="terminal-output"><div class="terminal-greeting">${icon('terminal')} ${esc(b.agent)} <span>in ${esc(b.name)}</span></div><div class="log-lines">${b.logs.slice(-6).map((l,i)=>`<div><span>${String(i+1).padStart(2,'0')}</span>${esc(l)}</div>`).join('')}</div><form id="terminal-form"><label for="command-input">❯</label><input id="command-input" placeholder="${b.status==='Suspend'?'Resume sandbox to continue':'輸入 help 查看可用模擬指令'}" autocomplete="off" ${b.status==='Suspend'?'disabled':''}><span>${b.status.toLowerCase()}</span></form></div>`:tab==='files'?filePanel(b):`<div class="activity">${b.snapshots.length?`<div class="snapshots"><strong>Snapshots</strong>${b.snapshots.map(sn=>`<div>${icon('copy')}<span>${esc(sn.name)}<small> · ${new Date(sn.at).toLocaleTimeString()}</small></span><button class="subtle" data-fork="${sn.id}">Fork</button></div>`).join('')}</div>`:''}${b.logs.map(l=>`<div>${icon('check')}<span>${esc(l)}</span></div>`).join('')}</div>`}</section>`;}
function filePanel(b){const list=files.filter(f=>f.box===b.id);return `<section class="files-panel"><div class="section-head"><div><strong>/workspace</strong><small> ${list.length} files · 此瀏覽器內儲存</small></div><div class="actions"><button class="subtle" data-upload="folder" ${b.status==='Suspend'?'disabled':''}>${icon('folder')}Upload folder</button><button class="primary compact" data-upload="file" ${b.status==='Suspend'?'disabled':''}>${icon('upload')}Upload files</button></div></div><input id="file-input" type="file" multiple hidden><input id="folder-input" type="file" webkitdirectory multiple hidden><div class="drop-zone" id="drop-zone">${icon('upload')}<span>${b.status==='Suspend'?'先 Resume 沙盒，即可繼續操作檔案':'拖曳檔案到這裡，或選擇檔案／資料夾'}</span><small>每個檔案最多 10 MB · 資料夾保留相對路徑</small></div>${fileError?`<p role="alert">${fileError}</p>`:''}<div class="file-list">${list.map(f=>`<div>${icon('folder')}<span>${esc(f.path)}</span><small>${(f.blob.size/1024).toFixed(1)} KB</small><button class="subtle" data-download="${esc(f.key)}">Download</button><button class="subtle delete-file" data-delete="${esc(f.key)}" aria-label="Delete ${esc(f.path)}" ${b.status==='Suspend'?'disabled':''}>Delete</button></div>`).join('')||'<p class="empty">這個工作空間還沒有檔案。</p>'}</div></section>`;}
function usage(){return `<div class="page-title"><div><div class="eyebrow">PAY FOR THE STATE YOU NEED.</div><h1>Usage<span class="title-dot">.</span></h1><p>依狀態累計的模擬費用，非真實帳單。</p></div><div class="total"><small>Estimated session total</small><strong id="total-cost">${money(boxes.reduce((a,b)=>a+cost(b),0))}</strong></div></div><div class="pricing-grid">${['Active','Idle','Suspend'].map(s=>`<section>${badge(s)}<h2>${money(rates[s])}<small> / hr</small></h2><p>${{Active:'Agent 運算與互動期間。',Idle:'環境就緒，暫無執行工作。',Suspend:'暫停運算，保留工作區。'}[s]}</p><small>基準：2 vCPU / 4 GB · 4 vCPU 示意為 2 倍</small></section>`).join('')}</div><div class="notice">費率僅用來驗證產品流程，尚未定價。計時與倒數會在重新整理後接續；關閉頁面期間暫停，不模擬離線計費。</div>
<section class="policy"><div><strong>Auto-tiering policy</strong><small>沒有終端輸入、檔案操作時自動降級，這是「不為等待 LLM 付全價」的關鍵。</small></div><label>Active → Idle after <select id="idle-after">${[60,120,300,600].map(v=>`<option value="${v}" ${policy.idleAfter===v?'selected':''}>${v/60} min</option>`).join('')}</select></label><label>Idle → Suspend after <select id="suspend-after">${[120,300,900,1800].map(v=>`<option value="${v}" ${policy.suspendAfter===v?'selected':''}>${v/60} min</option>`).join('')}</select></label></section>
<section class="compare"><div><strong>同一小時工作的示意成本</strong><small>假設 2 vCPU / 4 GB，一小時內 Agent 只有 20 分鐘真的在運算，其餘 40 分鐘在等 LLM 回覆。競品費率取自 2026-09 公開定價，僅供比較。</small></div><table><thead><tr><th>Provider</th><th>計費方式</th><th>1 小時成本</th></tr></thead><tbody>${competitors.map(([n,r])=>`<tr class="${r===null?'chosen':''}"><td>${n}</td><td>${r===null?'Active 20 min + Idle 40 min':n.startsWith('Vercel')?'active CPU + 全時記憶體':n.startsWith('Fly')?'閒置自動暫停（示意以全時計）':'保留 vCPU/GiB 全時計費'}</td><td class="mono">${money(r===null?rates.Active/3+rates.Idle*2/3:r)}</td></tr>`).join('')}</tbody></table></section><div class="usage-rows">${boxes.map(b=>`<div><strong>${esc(b.name)}</strong>${badge(b.status)}<span class="mono" data-cost="${b.id}">${money(cost(b))}</span></div>`).join('')}</div>`;}
function guide(){return `<div class="page-title"><div><div class="eyebrow">FROM ZERO TO WORKSPACE.</div><h1>Quick start<span class="title-dot">.</span></h1><p>先體驗流程，再連接真正的執行環境。</p></div></div><div class="guide"><section><span>01</span><h2>Choose your agent</h2><p>在 Sandboxes 選擇 Claude、Codex 或 Harness，點選 Try in console 建立模擬工作空間。</p><code>sandbox claude</code></section><section><span>02</span><h2>Bring your files</h2><p>上傳檔案或整個資料夾，保留相對路徑；下載可取回原始檔案。資料只保存在目前瀏覽器。</p></section><section><span>03</span><h2>Pause. Pick up. Continue.</h2><p>Active 執行工作；Idle 保持就緒；Suspend 暫停並保留檔案。Resume 後繼續操作。</p></section></div><div class="notice">目前沒有可安裝的 CLI，也尚未接入 Claude / Codex API、gVisor 或 Firecracker。終端支援 help、ls、pwd、status、clear、sandbox claude / codex / harness；其他命令不會在主機執行。</div>`;}
function create(name,a,cpu,repo=''){refresh();tick(false);if(boxes.some(b=>b.name===name)){toast('這個名稱已經存在，請換一個名稱。');return false;}const b={id:'sbx_'+crypto.randomUUID().slice(0,8),name,agent:a,cpu:Number(cpu),status:'Active',created:Date.now(),lastActivity:Date.now(),repo,snapshots:[],seconds:{Active:0,Idle:0,Suspend:0},logs:['Sandbox initialized (demo) · Ubuntu 24.04','Workspace mounted at /workspace',...(repo?['Cloned '+repo+' into /workspace (demo)']:[]),a+' session ready. Waiting for a task.']};restoreClock(b,Date.now());boxes.unshift(b);selected=b.id;view='sandboxes';filter='All';query='';tab='terminal';save();render();toast('Sandbox 已建立 · 模擬環境');return true;}
function bind(){bindFileDeletion();document.querySelectorAll('[data-view]').forEach(el=>el.onclick=()=>{view=el.dataset.view;render();});document.querySelector('.brand').onclick=e=>{e.preventDefault();view='sandboxes';render();};document.querySelectorAll('[data-create]').forEach(el=>el.onclick=()=>document.querySelector('#create-dialog').showModal());document.querySelectorAll('[data-agent]').forEach(el=>el.onclick=()=>{agent=el.dataset.agent;render();});document.querySelector('[data-copy]')?.addEventListener('click',async()=>{try{await navigator.clipboard.writeText('sandbox '+agent.toLowerCase());toast('已複製啟動指令（CLI 尚未實作）');}catch{toast('無法存取剪貼簿，請選取指令複製。');}});document.querySelector('[data-launch]')?.addEventListener('click',()=>create(agent.toLowerCase()+'-workspace-'+Date.now().toString().slice(-5),agent,2));document.querySelectorAll('[data-filter]').forEach(el=>el.onclick=()=>{filter=el.dataset.filter;render();});document.querySelector('#search')?.addEventListener('input',e=>{const pos=e.target.selectionStart;query=e.target.value;render();const input=document.querySelector('#search');input.focus();input.setSelectionRange(pos,pos);});document.querySelectorAll('[data-select]').forEach(el=>el.onclick=()=>{selected=el.dataset.select;render();});document.querySelectorAll('[data-tab]').forEach(el=>el.onclick=()=>{tab=el.dataset.tab;render();});document.querySelectorAll('[data-state]').forEach(el=>el.onclick=()=>{refresh();const b=current();setState(b,el.dataset.state,Date.now(),policy);b.logs.push('State changed to '+el.dataset.state+' (manual, demo)');save();render();});document.querySelector('[data-snapshot]')?.addEventListener('click',()=>{refresh();snapshot(current());});document.querySelectorAll('[data-fork]').forEach(el=>el.onclick=()=>{refresh();fork(current(),el.dataset.fork);});document.querySelector('#idle-after')?.addEventListener('change',e=>{tick(false);policy.idleAfter=Number(e.target.value);savePolicy();save();render();});document.querySelector('#suspend-after')?.addEventListener('change',e=>{tick(false);policy.suspendAfter=Number(e.target.value);savePolicy();save();render();});document.querySelector('#terminal-form')?.addEventListener('submit',e=>{e.preventDefault();const command=document.querySelector('#command-input').value.trim();if(!command)return;refresh();tick(false);const b=current();if(b.status==='Suspend')return;touch(b);if(b.status==='Idle'){setState(b,'Active',Date.now(),policy);b.logs.push('Resumed to Active on input (demo)');}b.logs.push('$ '+command);const parts=command.split(/\s+/);if(command==='clear')b.logs=[];else if(command==='help')b.logs.push('Available: help, ls, pwd, status, clear, git status, sandbox <claude|codex|harness>, sandbox ls, sandbox connect <id>, sandbox suspend, sandbox snapshot [name]');else if(command==='git status')b.logs.push(b.repo?'On branch main · '+files.filter(f=>f.box===b.id).length+' untracked file(s) (demo)':'fatal: not a git repository (create the sandbox with a repo URL)');else if(command==='sandbox ls')boxes.forEach(x=>b.logs.push(x.id+'  '+x.status.padEnd(8)+x.agent.padEnd(8)+x.name));else if(command==='sandbox suspend'){setState(b,'Suspend',Date.now(),policy);b.logs.push('Suspended, workspace preserved (demo)');}else if(parts[0]==='sandbox'&&parts[1]==='snapshot'){snapshot(b,parts.slice(2).join(' '));return;}else if(parts[0]==='sandbox'&&parts[1]==='connect'){const t=boxes.find(x=>x.id===parts[2]);if(t){selected=t.id;if(t.status!=='Active'){setState(t,'Active',Date.now(),policy);t.logs.push('Resumed by connect (demo)');}}else b.logs.push('No sandbox with id '+parts[2]);}else if(command==='pwd')b.logs.push('/workspace');else if(command==='status')b.logs.push(b.status+' · '+b.cpu+' vCPU · '+b.cpu*2+' GB · demo');else if(command==='ls')b.logs.push(files.filter(f=>f.box===b.id).map(f=>f.path).join('  ')||'(workspace is empty)');else if(parts[0]==='sandbox'&&parts.length===2&&['claude','codex','harness'].includes(parts[1])){b.agent=parts[1][0].toUpperCase()+parts[1].slice(1);b.status='Active';b.logs.push(b.agent+' session ready (simulation).');}else b.logs.push('Demo only: command not executed. Type help for available commands.');save();render();document.querySelector('#command-input')?.focus();});document.querySelector('#file-box')?.addEventListener('change',e=>{selected=e.target.value;render();});document.querySelectorAll('[data-upload]').forEach(el=>el.onclick=()=>document.querySelector('#'+el.dataset.upload+'-input').click());['file','folder'].forEach(t=>document.querySelector('#'+t+'-input')?.addEventListener('change',e=>upload([...e.target.files])));const drop=document.querySelector('#drop-zone');if(drop){drop.ondragover=e=>{e.preventDefault();drop.classList.add('over');};drop.ondragleave=()=>drop.classList.remove('over');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');upload([...e.dataTransfer.files]);};}document.querySelectorAll('[data-download]').forEach(el=>el.onclick=()=>{const f=files.find(f=>f.key===el.dataset.download);const url=URL.createObjectURL(f.blob);const a=document.createElement('a');a.href=url;a.download=f.path.split('/').pop();a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});}
async function upload(items){refresh();tick(false);const b=current();if(b.status==='Suspend'){toast('請先 Resume 沙盒。');return;}if(!items.length){toast('請使用 Upload folder 選取資料夾。');return;}let entries;try{entries=planUpload(b.id,items);}catch(e){toast(e.message||'選取內容無效。');return;}const conflicts=findConflicts(entries,await currentKeys());if(conflicts.length)askOverwrite(b,entries,conflicts);else await performUpload(b.id,entries,'skip',new Set());}
function tick(redraw=true){const now=Date.now();let changed=false;boxes.forEach(b=>{if(advance(b,now,policy))changed=true;});if(changed&&redraw){save();render();}}
function snapshot(b,name){name=name||'snap-'+(b.snapshots.length+1);b.snapshots.push({id:crypto.randomUUID().slice(0,8),name,at:Date.now()});b.logs.push('Snapshot "'+name+'" created · memory + /workspace (demo)');touch(b);save();render();toast('已建立快照 '+name+'（模擬）');}
async function fork(b,sid){const sn=b.snapshots.find(s=>s.id===sid);if(!sn)return;const name=b.name+'-'+sn.name;if(!create(name,b.agent,b.cpu,b.repo))return;const nb=boxes[0];nb.logs.splice(-1,0,'Forked from '+b.name+' @ '+sn.name+' (demo)');try{for(const f of files.filter(f=>f.box===b.id))await fileOp('readwrite',s=>s.put({key:nb.id+':'+f.path,box:nb.id,path:f.path,blob:f.blob}));}catch{}await readFiles();}
setInterval(()=>document.querySelectorAll('[data-countdown]').forEach(el=>{const b=boxes.find(x=>x.id===el.dataset.countdown);if(!b)return;el.textContent=(b.status==='Active'?'auto-idle in ':'auto-suspend in ')+fmt((deadline(b,policy)-Date.now())/1000);}),1000);
setInterval(()=>{tick();save();document.querySelectorAll('[data-cost]').forEach(el=>{const b=boxes.find(x=>x.id===el.dataset.cost);el.textContent=money(cost(b));});const total=document.querySelector('#total-cost');if(total)total.textContent=money(boxes.reduce((a,b)=>a+cost(b),0));},5000);
document.querySelector('[data-close]').onclick=()=>document.querySelector('#create-dialog').close();document.querySelector('#create-form').onsubmit=e=>{e.preventDefault();const f=new FormData(e.target);tick();if(create(f.get('name').trim(),f.get('agent'),f.get('size'),(f.get('repo')||'').trim())){document.querySelector('#create-dialog').close();e.target.reset();}};

// Keep the confirmation outside #app so timer-driven renders cannot dismiss it.
const deleteDialog = document.createElement('dialog');
deleteDialog.id = 'delete-file-dialog';
deleteDialog.setAttribute('aria-labelledby', 'delete-file-title');
deleteDialog.innerHTML = `<h2 id="delete-file-title">刪除檔案？</h2>
<p>將永久刪除此工作空間中的檔案，無法復原。</p>
<p id="delete-file-workspace"></p><code id="delete-file-path"></code>
<p id="delete-file-error" role="alert"></p>
<div class="actions"><button class="subtle" id="delete-file-cancel" autofocus>取消</button><button class="primary" id="delete-file-confirm">永久刪除</button></div>`;
document.body.append(deleteDialog);
let deleteTarget = null, deletePending = false;
const deleteError = deleteDialog.querySelector('#delete-file-error');
const deleteConfirm = deleteDialog.querySelector('#delete-file-confirm');
const deleteCancel = deleteDialog.querySelector('#delete-file-cancel');
const deleteFile = createFileDeleter({
  getBox: id => boxes.find(b => b.id === id),
  settle: () => tick(false),
  remove: async key => deleteStoredFile(await dbPromise, key),
  activity: (b, target) => { touch(b); b.logs.push('Deleted '+target.path+' (browser-local).'); save(); }
});
function bindFileDeletion() {
  document.querySelectorAll('[data-delete]').forEach(button => {
    button.onclick = () => {
      const file = files.find(f => f.key === button.dataset.delete);
      if (!file) return;
      deleteTarget = { key: file.key, box: file.box, path: file.path };
      deleteDialog.querySelector('#delete-file-workspace').textContent = boxes.find(b => b.id === file.box)?.name || file.box;
      deleteDialog.querySelector('#delete-file-path').textContent = file.path;
      deleteError.textContent = '';
      deleteDialog.showModal();
    };
  });
}
deleteCancel.onclick = () => deleteDialog.close();
deleteDialog.addEventListener('cancel', event => { if (deletePending) event.preventDefault(); });
deleteConfirm.onclick = async () => {
  if (deletePending || !deleteTarget) return;
  refresh();
  const target = { ...deleteTarget };
  deletePending = true;
  deleteConfirm.disabled = deleteCancel.disabled = true;
  deleteConfirm.textContent = '刪除中…';
  deleteError.textContent = '';
  try {
    await deleteFile(target);
    files = files.filter(f => f.key !== target.key);
    deleteDialog.close();
    render();
    toast('已永久刪除 '+target.path);
  } catch (error) {
    deleteError.textContent = error.message || '刪除失敗，請重試。';
    render();
  } finally {
    deletePending = false;
    deleteConfirm.disabled = deleteCancel.disabled = false;
    deleteConfirm.textContent = '永久刪除';
  }
};

window.addEventListener('pagehide',()=>{tick(false);save();});
window.addEventListener('storage',e=>{const before=boxes.map(b=>b.id).join();if(sync.onStorage(e)){reselect();if(boxes.map(b=>b.id).join()!==before)render();}});

const overwriteDialog = document.createElement('dialog');
overwriteDialog.id = 'upload-overwrite-dialog';
overwriteDialog.setAttribute('aria-labelledby', 'overwrite-title');
overwriteDialog.innerHTML = `<h2 id="overwrite-title">覆寫已存在的檔案？</h2>
<p>選取內容中有 <b id="overwrite-count">0</b> 個檔案與此工作空間現有檔案的完整路徑相同。</p>
<p id="overwrite-workspace"></p><ul id="overwrite-paths"></ul>
<p id="overwrite-changed" role="alert"></p>
<div class="actions"><button class="subtle" id="overwrite-cancel" autofocus>取消整批</button><button class="subtle" id="overwrite-skip">略過衝突檔案</button><button class="primary" id="overwrite-confirm">覆寫衝突檔案</button></div>`;
document.body.append(overwriteDialog);
let overwriteCtx = null, overwritePending = false;
const overwriteChanged = overwriteDialog.querySelector('#overwrite-changed');
const overwriteCancel = overwriteDialog.querySelector('#overwrite-cancel');
const overwriteSkip = overwriteDialog.querySelector('#overwrite-skip');
const overwriteConfirm = overwriteDialog.querySelector('#overwrite-confirm');
const uploadFiles = createUploadWriter({
  getBox: id => boxes.find(b => b.id === id),
  settle: () => tick(false),
  currentKeys,
  write: async entries => writeFiles(await dbPromise, entries),
  activity: (b, c) => { touch(b); b.logs.push(c.overwritten ? 'Added '+c.written+' file(s), overwrote '+c.overwritten+' existing (browser-local).' : 'Added '+c.written+' file(s) to /workspace (browser-local).'); save(); }
});
function showConflicts(list, changed) {
  overwriteCtx.confirmedKeys = new Set(list.map(e => e.key));
  overwriteDialog.querySelector('#overwrite-paths').innerHTML = list.map(e => `<li>${esc(e.path)}</li>`).join('');
  overwriteDialog.querySelector('#overwrite-count').textContent = list.length;
  overwriteChanged.textContent = changed ? '等待期間目標已改變，以上為最新衝突清單，請重新確認。' : '';
}
function askOverwrite(b, entries, conflicts) {
  overwriteCtx = { boxId: b.id, entries, confirmedKeys: new Set() };
  overwriteDialog.querySelector('#overwrite-workspace').textContent = b.name;
  showConflicts(conflicts, false);
  overwriteDialog.showModal();
}
async function performUpload(boxId, entries, mode, confirmedKeys) {
  try {
    const r = await uploadFiles({ boxId, entries, mode, confirmedKeys });
    if (r.status === 'reconfirm') { showConflicts(r.conflicts, true); return; }
    if (!r.written && r.skipped) toast('已略過 '+r.skipped+' 個衝突檔案，未寫入任何檔案。');
    else if (r.overwritten) toast('已儲存 '+r.written+' 個檔案（覆寫 '+r.overwritten+' 個既有檔案）');
    else if (r.skipped) toast('已儲存 '+r.written+' 個；略過 '+r.skipped+' 個衝突檔案。');
    else toast('已儲存 '+r.written+' 個檔案至本機工作空間');
    if (overwriteDialog.open) overwriteDialog.close();
    await readFiles();
  } catch (e) {
    if (overwriteDialog.open) overwriteDialog.close();
    toast(e.message || '上傳失敗，請重試。');
    await readFiles();
  }
}
overwriteCancel.onclick = () => overwriteDialog.close();
overwriteDialog.addEventListener('cancel', event => { if (overwritePending) event.preventDefault(); });
async function chooseOverwrite(mode) {
  if (!overwriteCtx || overwritePending) return;
  refresh();
  overwritePending = true;
  overwriteConfirm.disabled = overwriteSkip.disabled = overwriteCancel.disabled = true;
  overwriteConfirm.textContent = '寫入中…';
  overwriteChanged.textContent = '';
  try { await performUpload(overwriteCtx.boxId, overwriteCtx.entries, mode, overwriteCtx.confirmedKeys); }
  finally { overwritePending = false; overwriteConfirm.disabled = overwriteSkip.disabled = overwriteCancel.disabled = false; overwriteConfirm.textContent = '覆寫衝突檔案'; }
}
overwriteSkip.onclick = () => chooseOverwrite('skip');
overwriteConfirm.onclick = () => chooseOverwrite('overwrite');
window.addEventListener('pageshow',e=>{if(e.persisted){sync.refresh();boxes.forEach(b=>restoreClock(b,Date.now()));render();}});
render();readFiles();

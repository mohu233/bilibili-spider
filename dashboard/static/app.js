'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=value=>Number(value||0).toLocaleString('zh-CN');
const dec=value=>Number(value||0).toFixed(1);
const shortTime=value=>value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'尚无记录';
const modelNames={'mobilenetv3_v0.1_dist':'MobileNetV3','caformer_s36_v0.1':'CAFormer S36',yolo:'YOLOv8'};
const traversalNames={pending:'待遍历',partial:'部分完成',complete:'遍历完成',failed:'采集失败',legacy_done:'历史完成'};
let token='',view='overview',page=1,total=0,pageSize=30,state=null,models={},polling=false,drawerUid=null,toastTimer,searchTimer;
let referencePage=1,referenceTotal=0,referenceTimer;
const titles={overview:['总览','让每一次发现，<span>都有迹可循。</span>','从一颗种子开始，建立可追溯的 Furry 关系网络。'],
  seeds:['种子 UID','为下一次发现，<span>种下一颗种子。</span>','这里是尚未完成粉丝与关注遍历的 UID，按价值安排探索顺序。'],
  yes:['Furry Yes','发现更多，<span>也了解更多。</span>','确认匹配的账号都在这里，包括待遍历和已经完成遍历的种子。'],
  no:['Furry No','保留每一个判断，<span>随时可以修正。</span>','已排除的 UID 不参与关系扩散；人工判定可以恢复或调整。'],
  settings:['Cookie 池与设置','让采集，<span>保持合适的节奏。</span>','管理本机登录账号、模型与每轮采集范围。']};

async function api(path,data){
  const options={cache:'no-store',headers:{'Accept':'application/json'}};
  if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-Panel-Token']=token;options.body=JSON.stringify(data)}
  const response=await fetch(path,options);
  const result=await response.json();
  if(!response.ok)throw new Error(result.error||'操作失败');
  return result;
}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?6500:3500)}
function showError(error){toast(error.message||'无法完成操作',true)}
async function busy(button,fn){const text=button?.textContent;if(button){button.disabled=true;button.textContent='处理中…'}try{return await fn()}catch(error){showError(error)}finally{if(button){button.disabled=false;button.textContent=text}if(state)updateJob()}}
function avatar(row){return `<span class="avatar"><span>${esc((row.name||'U').slice(0,1))}</span>${row.image_path?`<img src="/avatar/${encodeURIComponent(row.uid)}?v=${encodeURIComponent(row.collected_at||'imported')}" alt="${esc(row.name||'用户')}的头像" loading="lazy">`:''}</span>`}
function evidenceLabel(row){if(row.manual==='yes')return '人工确认';if(row.manual==='no')return '人工排除';return row.evidence?.model?modelNames[row.evidence.model]||'模型识别':row.source?.startsWith('legacy')&&row.verdict!=='unknown'?'历史分类':'待识别'}
function traversal(row){const name=traversalNames[row.traversal]||'待遍历';const tone=row.traversal==='complete'?'':row.traversal==='partial'||row.traversal==='legacy_done'?'warning':row.traversal==='failed'?'error':'neutral';return `<span class="status-badge ${tone}">${esc(name)}</span><small>关注 ${esc(row.followings_state==='complete'?'已完成':`下次第 ${row.followings_page} 页`)} · 粉丝 ${esc(row.followers_state==='complete'?'已完成':`下次第 ${row.followers_page} 页`)}</small>`}
function setView(){
  const next=location.hash.slice(1);view=Object.hasOwn(titles,next)?next:'overview';page=1;
  document.querySelectorAll('[data-route]').forEach(el=>el.classList.toggle('active',el.dataset.route===view));
  const [crumb,title,description]=titles[view];$('breadcrumb').textContent=crumb;$('pageTitle').innerHTML=title;$('pageDescription').textContent=description;
  $('overviewSection').hidden=view!=='overview';$('settingsSection').hidden=view!=='settings';$('dataSection').hidden=!['seeds','yes','no'].includes(view);
  $('scoringCard').hidden=view==='settings';$('exportButton').hidden=!['seeds','yes','no'].includes(view);
  if(['seeds','yes','no'].includes(view)){
    $('searchInput').value='';$('statusFilter').value='';$('sortSelect').value='score';
    const info={seeds:['SEED POOL','等待探索的种子','种子页只展示尚未完成遍历且未被排除的 UID。模型不确定的记录需要人工审核。'],yes:['FURRY YES','确认匹配的账号','已完成与待遍历的 Furry 账号统一保留。已完成记录不再进入待遍历种子池。'],no:['FURRY NO','保留排除记录','排除记录不进入采集队列；发现误判时，可在详情中确认 Furry 或恢复自动结果。']}[view];
    $('dataEyebrow').textContent=info[0];$('dataTitle').textContent=info[1];$('tableHelp').textContent=info[2];
    const empty={seeds:['从第一颗种子开始','添加 UID，或从参考资料提取新种子。后续资料与头像会重新采集。'],yes:['还没有确认的 Furry','模型确认或人工审核通过的账号会出现在这里。'],no:['还没有排除记录','模型或人工排除的 UID 将保留在这里，方便后续修正。']}[view];
    $('dataEmpty').querySelector('h3').textContent=empty[0];$('dataEmpty').querySelector('p').textContent=empty[1];
    $('dataEmpty').querySelector('button').hidden=view!=='seeds';
    $('importBenchmark').hidden=view!=='seeds';$('importLegacy').hidden=view!=='seeds';
    loadProfiles().catch(showError);
  }
  if(view==='settings'&&state)fillSettings(state.config);
}
function updateCounts(){
  const counts=state.counts;
  for(const [id,key]of [['statSeeds','seeds'],['statYes','yes'],['statNo','no'],['statComplete','complete'],['navSeeds','seeds'],['navYes','yes'],['navNo','no']])$(id).textContent=number(counts[key]);
  $('statRelations').textContent=`已记录 ${number(state.relation_count)} 条有向关系`;
  const hist=state.histogram,max=Math.max(1,...hist);
  $('histogram').innerHTML=hist.map((n,i)=>`<div class="hist-bin"><b>${number(n)}</b><div class="hist-bar" data-height="${n/max*90}"></div><small>${i*20}–${i===4?100:(i+1)*20-1}</small></div>`).join('');
  document.querySelectorAll('.hist-bar').forEach(el=>el.style.height=Math.max(2,Number(el.dataset.height))+'px');
  $('leaders').innerHTML=state.leaders.length?state.leaders.map((r,i)=>`<div class="leader-row"><span class="leader-rank">0${i+1}</span><div class="leader-info"><a href="#yes" data-action="detail" data-uid="${esc(r.uid)}"><b>${esc(r.name||r.uid)}</b></a><small>直接发现 ${number(r.furry_found)} 个 Furry</small></div><b class="leader-score">${dec(r.score)}</b></div>`).join(''):'<div class="leader-empty"><span>◈</span><p>还没有确认的 Furry 种子</p><small>采集或人工确认后，得分会出现在这里</small></div>';
}
function updateJob(){
  const job=state?.job,running=!!state?.worker_active,labels={running:'采集中',stopping:'正在暂停',completed:'本轮完成',paused:'已暂停',interrupted:'上次运行中断',blocked:'等待处理',failed:'运行失败'};
  const options=job?JSON.parse(job.options):state?.config||{};
  const max=options.max_users||10,processed=job?.processed||0;
  $('jobBadge').textContent=job?labels[job.state]||job.state:'尚未启动';
  $('jobTitle').textContent=running?job?.current_uid?`正在探索 UID ${job.current_uid}`:'正在准备采集':job?labels[job.state]||'本轮已结束':'准备好开始了吗？';
  $('jobDescription').textContent=job?.message||(running?'按种子得分依次处理，分页进度自动保存。':'添加种子与 Cookie 后，启动第一轮采集。');
  $('jobProgressText').textContent=`${processed} / ${max} 个 UID`;
  $('jobPhase').textContent=job?.phase||(running?'任务进行中':'等待启动');
  $('jobProgressBar').style.width=Math.min(100,processed/max*100)+'%';
  $('startButton').disabled=running;$('startButton').textContent=job?'▶ 开始下一轮':'▶ 开始采集';
  $('stopButton').disabled=!running||job?.state==='stopping';
  if(state?.config.cooldown_until&&state.config.cooldown_until>new Date().toISOString()){
    $('jobDescription').textContent=`平台冷却至 ${shortTime(state.config.cooldown_until)}，请稍后再启动。`;$('startButton').disabled=true;
  }
}
function updateEvents(){
  $('events').innerHTML=state.events.length?state.events.map(r=>`<div class="event-row ${esc(r.level)}"><time>${shortTime(r.time)}</time><i class="event-dot"></i><span class="event-message">${esc(r.message)}</span>${r.uid?`<a href="#${['yes','no','seeds'].includes(view)?view:'seeds'}" class="event-uid" data-action="detail" data-uid="${esc(r.uid)}">${esc(r.uid)} ↗</a>`:''}</div>`).join(''):'<div class="events-empty">还没有采集动态。添加种子或启动采集后，这里会记录实际发生的操作。</div>';
}
function updateCookies(){
  const accounts=state.accounts,active=accounts.filter(x=>x.enabled&&x.status!=='expired').length;
  $('cookieSummary').innerHTML=`<span>账号 ${accounts.length}</span><span>启用候选 ${active}</span><span>已停用 ${accounts.filter(x=>!x.enabled).length}</span>`;
  const names={unverified:'未验证',valid:'验证有效',expired:'已失效',error:'验证失败'};
  $('cookieList').innerHTML=accounts.length?accounts.map((r,i)=>`<div class="cookie-row"><div class="account-avatar">${String(i+1).padStart(2,'0')}</div><div class="account-info"><b>${esc(r.label)}</b><small>${r.account_name?esc(r.account_name)+' · ':''}验证：${shortTime(r.checked_at)} · 分配 ${r.request_count} 次</small></div><span class="secret-mask" aria-label="Cookie 已隐藏">••••••••</span><span class="status-badge ${!r.enabled?'neutral':r.status==='expired'||r.status==='error'?'error':r.status==='valid'?'':'warning'}">${!r.enabled?'已停用':names[r.status]||'未验证'}</span><div class="cookie-actions"><button class="button secondary compact" data-action="validate-cookie" data-id="${esc(r.id)}">验证</button><button class="button secondary compact" data-action="toggle-cookie" data-id="${esc(r.id)}" data-enabled="${r.enabled?'false':'true'}">${r.enabled?'停用':'启用'}</button></div></div>`).join(''):'<div class="leader-empty"><span>⚿</span><p>Cookie 池为空</p><small>在下方添加完整 Cookie，或从项目根目录导入</small></div>';
}
function fillSettings(config){
  $('maxUsers').value=config.max_users;$('pagesPerRun').value=config.pages_per_run;$('requestInterval').value=config.request_interval;
  $('modelSelect').value=config.model;$('positiveThreshold').value=config.positive_threshold;updateModelHint();
}
function updateModelHint(){
  const name=$('modelSelect').value,available=models[name],ready=available?.weights&&available?.runtime;
  $('modelStatus').textContent=ready?'模型就绪':available?.weights?'缺少运行依赖':'未找到模型权重';
  $('modelStatus').className='status-badge '+(ready?'':'warning');
  $('modelHint').textContent=ready?'本地权重可用，头像不会上传至第三方。':'请按 PANEL.md 配置模型与运行环境。';
  const threshold=Number($('positiveThreshold').value||.85);
  $('thresholdHint').textContent=`分数 ≥ ${threshold.toFixed(2)} 自动确认；≤ ${(1-threshold).toFixed(2)} 自动排除；中间区间留待人工审核。`;
}
async function poll(){
  if(polling)return;polling=true;
  try{
    state=await api('/api/state');$('connection').innerHTML='<i></i> 本地已连接';$('connection').classList.remove('offline');$('fatal').hidden=true;
    updateCounts();updateJob();updateEvents();updateCookies();
    $('lastRefresh').textContent='最后刷新 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if(['seeds','yes','no'].includes(view))await loadProfiles();
  }catch(error){$('connection').innerHTML='<i></i> 连接已断开';$('connection').classList.add('offline');$('fatal').textContent='无法连接本地面板。请确认 panel.py 仍在运行，恢复后会自动重连。';$('fatal').hidden=false}
  finally{polling=false}
}
async function loadProfiles(){
  if(!['seeds','yes','no'].includes(view))return;
  const snapshotView=view;
  const query=new URLSearchParams({view,page:String(page),q:$('searchInput').value,status:$('statusFilter').value,sort:$('sortSelect').value});
  const data=await api('/api/profiles?'+query);
  if(view!==snapshotView)return;
  total=data.total;pageSize=data.page_size;$('dataCount').textContent=`共 ${number(total)} 个 UID`;
  $('dataEmpty').hidden=total>0;$('profileRows').innerHTML=data.results.map(row=>{
    const score=row.evidence?.furry_score;
    return `<tr><td><div class="user-cell">${avatar(row)}<div><a href="#${view}" data-action="detail" data-uid="${esc(row.uid)}" class="user-name">${esc(row.name||'待获取资料')}</a><span class="user-id">UID ${esc(row.uid)}</span></div></div></td><td class="classification"><span class="mini-state ${esc(row.verdict)}">${esc(evidenceLabel(row))}</span><small>${score===undefined?(row.collection_status==='needs_review'?'等待人工审核':row.verdict==='unknown'?'尚无模型判定':row.manual?'人工优先':'已有分类记录'):`头像分数 ${Number(score).toFixed(3)}`}</small></td><td class="traversal-cell">${traversal(row)}</td><td><b class="discovery-count">${number(row.furry_found)}</b><small class="discovery-meta">已判定关系 ${number(row.evaluated_found)}</small></td><td class="score-cell"><b>${dec(row.score)}</b><span class="score-meter"><i data-width="${Math.max(0,Math.min(100,Number(row.score)))}"></i></span><small>基础 ${dec(row.base_score)} + 父级 ${dec(row.inherited_score)}</small></td><td><div class="row-actions"><button data-action="detail" data-uid="${esc(row.uid)}">详情 ↗</button></div></td></tr>`;
  }).join('');
  document.querySelectorAll('.score-meter i').forEach(el=>el.style.width=el.dataset.width+'%');
  $('paginationText').textContent=`第 ${page} / ${Math.max(1,Math.ceil(total/pageSize))} 页 · ${number(total)} 条`;
  $('previousPage').disabled=page<=1;$('nextPage').disabled=page*pageSize>=total;
}
async function detail(user){
  const row=await api('/api/profile?uid='+encodeURIComponent(user));drawerUid=row.uid;
  const evidence=row.evidence||{};
  const safeEvidence={模型:evidence.model?modelNames[evidence.model]||evidence.model:'尚未识别',分数:evidence.furry_score===undefined?'—':Number(evidence.furry_score).toFixed(4),阈值:evidence.threshold??'—',判定时间:evidence.classified_at?shortTime(evidence.classified_at):'—',简介关键词:evidence.keywords||[],错误:evidence.error||'无'};
  $('drawerContent').innerHTML=`<div class="eyebrow">UID DETAILS</div><div class="drawer-heading">${avatar(row)}<div><h2>${esc(row.name||'待获取资料')}</h2><p>UID ${esc(row.uid)}</p></div></div><div class="drawer-block"><h3>身份与采集状态</h3><p><span class="mini-state ${esc(row.verdict)}">${row.verdict==='yes'?'Furry Yes':row.verdict==='no'?'Furry No':'待识别'}</span> <span class="status-badge neutral">${esc(traversalNames[row.traversal]||row.traversal)}</span></p><p class="form-note">来源：${esc(row.source)} · 父级：${esc(row.parent_uid||'手动种子 / 无')}<br>关注下次第 ${row.followings_page} 页 · 粉丝下次第 ${row.followers_page} 页<br>最后采集：${shortTime(row.collected_at)}</p>${row.last_error?`<p class="alert">${esc(row.last_error)}</p>`:''}</div><div class="drawer-block"><h3>种子价值</h3><div class="score-total">${dec(row.score)}<small>/ 100</small></div><div class="score-breakdown">${[['基础分','base_score'],['父级加成','inherited_score'],['发现收益','discovery_score'],['命中质量','quality_score']].map(([label,key])=>`<div><b>${dec(row[key])}</b><small>${label}</small></div>`).join('')}</div><p class="form-note">直接发现 ${row.furry_found} 个唯一 Furry，已判定关系 ${row.evaluated_found} 个。父级首次发现时得分 ${dec(row.parent_score_snapshot)}。</p><div class="drawer-form-row"><label>调整手动基础分（0–20）<input type="number" id="detailBase" min="0" max="20" step="0.5" value="${Number(row.base_score)}"></label><button class="button secondary compact" data-action="save-base">保存</button></div></div><div class="drawer-block"><h3>人工审核</h3><label>判定备注<textarea id="reviewReason" rows="2" maxlength="500" placeholder="记录判定依据（可选）">${esc(row.manual_reason||'')}</textarea></label><div class="review-actions"><button class="button primary compact" data-action="decide" data-decision="yes">✓ 确认 Furry</button><button class="button danger compact" data-action="decide" data-decision="no">× 排除</button><button class="button secondary compact" data-action="decide" data-decision="clear">恢复自动判定</button></div><p class="form-note">人工结果优先；重跑模型不会覆盖。排除后，会同步撤回父级的发现收益。</p></div><div class="drawer-block"><h3>用户简介</h3><p class="drawer-intro">${esc(row.intro||'暂无简介数据')}</p></div><div class="drawer-block"><h3>头像模型证据</h3><pre class="evidence-box">${esc(JSON.stringify(safeEvidence,null,2))}</pre><p class="form-note">简介关键词仅作为证据，不单独触发自动确认。模型失败不等于非 Furry。</p></div><div class="drawer-block"><h3>关系记录 · ${number(row.relations_count)} 个唯一 UID</h3>${row.children.length?row.children.map(r=>`<div class="relations-item"><a href="#${view}" data-action="detail" data-uid="${esc(r.uid)}">${esc(r.name||r.uid)}</a><span>${r.direction==='followers'?'粉丝':r.direction==='followings'?'关注':'历史关系'} · ${esc(r.verdict)}</span></div>`).join(''):'<p class="form-note">尚无已保存关系。</p>'}<p class="form-note">最多显示 100 条方向记录；同一 UID 的粉丝与关注关系只计一次发现收益。</p><div class="review-actions"><button class="button secondary compact" data-action="requeue">↻ 重新遍历关系</button><button class="button secondary compact" data-action="refresh-detail">刷新详情</button></div></div>`;
  $('drawerBackdrop').hidden=false;$('detailDrawer').hidden=false;
}
function closeDrawer(){drawerUid=null;$('drawerBackdrop').hidden=true;$('detailDrawer').hidden=true}
function addSeed(){ $('seedDialog').showModal();$('seedUids').focus() }
async function openReferences(){
  const query=new URLSearchParams({q:$('referenceSearch').value,page:String(referencePage)});
  const result=await api('/api/references?'+query);referenceTotal=result.total;
  $('referenceRows').innerHTML=result.results.length?result.results.map(r=>`<tr><td>${esc(r.uid)}<span class="user-id">${esc(r.name||'旧记录无昵称')}</span></td><td>${esc(r.source)}</td><td>${esc(r.previous_hit||'无')}</td><td><button class="button secondary compact" data-action="reference-seed" data-uid="${esc(r.uid)}">仅提取 UID</button></td></tr>`).join(''):'<tr><td colspan="4">暂无历史参考。可以载入原项目 JSON，原文件将保持不变。</td></tr>';
  $('referenceCount').textContent=`共 ${referenceTotal} 条参考 · 第 ${referencePage} 页`;
  $('referencePrevious').disabled=referencePage<=1;$('referenceNext').disabled=referencePage*50>=referenceTotal;
  if(!$('referenceDialog').open)$('referenceDialog').showModal();
}

document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  event.preventDefault();const action=button.dataset.action;
  if(action==='add-seed')return addSeed();if(action==='close-seed')return $('seedDialog').close();
  if(action==='detail')return detail(button.dataset.uid).catch(showError);
  busy(button,async()=>{
    if(action==='toggle-cookie'){await api('/api/cookies/toggle',{id:button.dataset.id,enabled:button.dataset.enabled==='true'});toast('账号状态已更新')}
    if(action==='validate-cookie'){const result=await api('/api/cookies/validate',{id:button.dataset.id});toast(result.message,!result.valid)}
    if(action==='decide'){await api('/api/decision',{uid:drawerUid,decision:button.dataset.decision,reason:$('reviewReason').value});toast('人工判定已保存，相关种子得分已重算');await detail(drawerUid)}
    if(action==='save-base'){await api('/api/base-score',{uid:drawerUid,base_score:Number($('detailBase').value)});toast('基础分已更新');await detail(drawerUid)}
    if(action==='requeue'){await api('/api/requeue',{uid:drawerUid});toast('已重新安排遍历，已有关系保留并去重');await detail(drawerUid)}
    if(action==='refresh-detail')await detail(drawerUid);
    if(action==='reference-seed'){await api('/api/seeds',{uids:button.dataset.uid,base_score:10,verdict:'unknown'});toast('已提取 UID 为全新种子，旧分类与分数未带入');}
    await poll();
  });
});
document.addEventListener('error',event=>{if(event.target.tagName==='IMG'&&event.target.closest('.avatar'))event.target.remove() },true);
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer()});
$('closeDrawer').addEventListener('click',closeDrawer);$('drawerBackdrop').addEventListener('click',closeDrawer);
$('addSeedButton').addEventListener('click',addSeed);$('refreshButton').addEventListener('click',()=>poll());
$('searchInput').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;loadProfiles().catch(showError)},250)});
for(const id of ['statusFilter','sortSelect'])$(id).addEventListener('change',()=>{page=1;loadProfiles().catch(showError)});
$('previousPage').addEventListener('click',()=>{page=Math.max(1,page-1);loadProfiles().catch(showError)});
$('nextPage').addEventListener('click',()=>{page++;loadProfiles().catch(showError)});
$('exportButton').addEventListener('click',()=>{location.href='/api/export?view='+encodeURIComponent(view)});
$('seedForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const result=await api('/api/seeds',{uids:$('seedUids').value,base_score:Number($('seedBase').value),verdict:$('seedVerdict').value});
  $('seedDialog').close();$('seedUids').value='';toast(`已添加 ${result.added} 个种子，跳过 ${result.duplicates} 个重复 UID`);
  if(view!=='seeds')location.hash='seeds';await poll();
})});
$('cookieForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  await api('/api/cookies',{label:$('cookieLabel').value,cookie:$('cookieValue').value});$('cookieValue').value='';$('cookieLabel').value='';toast('账号已保存，建议先验证再采集');await poll();
})});
$('settingsForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const config={max_users:Number($('maxUsers').value),pages_per_run:Number($('pagesPerRun').value),request_interval:Number($('requestInterval').value),model:$('modelSelect').value,positive_threshold:Number($('positiveThreshold').value)};
  await api('/api/settings',config);toast('采集设置已保存');await poll();
})});
$('modelSelect').addEventListener('change',updateModelHint);$('positiveThreshold').addEventListener('input',updateModelHint);
$('startButton').addEventListener('click',event=>busy(event.currentTarget,async()=>{await api('/api/jobs/start',{});toast('采集任务已启动');await poll()}));
$('stopButton').addEventListener('click',event=>busy(event.currentTarget,async()=>{await api('/api/jobs/stop',{});toast('已请求暂停，当前请求结束后保留分页退出');await poll()}));
$('importLegacy').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/import',{benchmark:false});toast(`载入 ${result.added} 条历史参考，没有沿用旧分类与分数`);await openReferences();await poll()}));
$('importBenchmark').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/import',{benchmark:true});toast(`导入 ${result.added} 个 UID；头像实验记录作为待识别种子`);await poll()}));
$('importCookies').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/cookies/import',{});toast(`导入 ${result.added} 个账号，跳过重复或格式无效条目 ${result.skipped} 个`);await poll()}));
$('viewReferences').addEventListener('click',()=>openReferences().catch(showError));
$('closeReferences').addEventListener('click',()=>$('referenceDialog').close());
$('loadReferenceFiles').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/import',{benchmark:false});toast(`载入 ${result.added} 条历史参考，未沿用旧分类`);await openReferences();await poll()}));
$('referenceSearch').addEventListener('input',()=>{clearTimeout(referenceTimer);referenceTimer=setTimeout(()=>{referencePage=1;openReferences().catch(showError)},250)});
$('referencePrevious').addEventListener('click',()=>{referencePage=Math.max(1,referencePage-1);openReferences().catch(showError)});
$('referenceNext').addEventListener('click',()=>{referencePage++;openReferences().catch(showError)});
window.addEventListener('hashchange',setView);
(async()=>{try{const boot=await api('/api/bootstrap');token=boot.token;models=boot.models;$('projectPath').textContent=boot.project_root;await poll();if(state)fillSettings(state.config);setView()}catch(error){$('fatal').hidden=false;$('fatal').textContent='面板初始化失败：'+error.message}setInterval(poll,4000)})();

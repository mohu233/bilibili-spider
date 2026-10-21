'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=value=>Number(value||0).toLocaleString('zh-CN');
const dec=value=>Number(value||0).toFixed(1);
const shortTime=value=>value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):'尚无记录';
const modelNames={'mobilenetv3_v0.1_dist':'MobileNetV3','caformer_s36_v0.1':'CAFormer S36',yolo:'YOLOv8'};
const traversalNames={pending:'待遍历',partial:'部分完成',complete:'遍历结束',failed:'采集失败',legacy_done:'历史完成'};
let token='',view='overview',page=1,total=0,pageSize=30,state=null,models={},polling=false,drawerUid=null,toastTimer,searchTimer,profileRequest=0;
let referencePage=1,referenceTotal=0,referenceTimer;
let collectorRevision=0;
let cookieDeleteTarget=null,cookieDeleteSubmitting=false;
const cookieBusyHint='正在采集或验证，请先停用并等待当前任务结束后再删除。';
let quickReviewOpen=false,quickReviewMinimized=false,quickReviewRow=null,quickReviewLoading=false,quickReviewSubmitting=false,quickReviewRequest=0,quickReviewDrag=null,quickReviewScopeKey='';
const quickReviewSeen=new Set();
const profileViews=['candidates','seeds','yes','no'];
const titles={overview:['总览','让每一次发现，<span>都有迹可循。</span>','优先检查现有候选，确认身份后再按设置安排种子的关系采集。'],
  candidates:['候选 UID','先看看线索，<span>再确认身份。</span>','待判定或待审核的账号先留在候选池；任一强信号确认后才晋升种子。'],
  seeds:['种子 UID','为下一次发现，<span>种下一颗种子。</span>','已确认账号保留在种子池；关系采集按候选优先或种子周期设置安排。'],
  yes:['Furry Yes','发现更多，<span>也了解更多。</span>','确认匹配的账号都在这里，包括待遍历和已经完成遍历的种子。'],
  no:['Furry No','保留每一个判断，<span>随时可以修正。</span>','已排除的 UID 不参与关系扩散；人工判定可以恢复或调整。'],
  settings:['Cookie 池与设置','让采集，<span>保持合适的节奏。</span>','管理本机登录账号、模型与单批或连续采集。']};

async function api(path,data){
  const options={cache:'no-store',headers:{'Accept':'application/json'}};
  if(data!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.headers['X-Panel-Token']=token;options.body=JSON.stringify(data)}
  const response=await fetch(path,options);
  const result=await response.json();
  if(!response.ok){const error=new Error(result.error||'操作失败');error.code=result.code;error.status=response.status;throw error}
  return result;
}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?6500:3500)}
function showError(error){toast(error.message||'无法完成操作',true)}
async function busy(button,fn){const text=button?.textContent;if(button){button.disabled=true;button.textContent='处理中…'}try{return await fn()}catch(error){showError(error)}finally{if(button){button.disabled=false;button.textContent=text}if(state)updateJob()}}
function avatar(row){return `<span class="avatar"><span>${esc((row.name||'U').slice(0,1))}</span>${row.image_path?`<img src="/avatar/${encodeURIComponent(row.uid)}?v=${encodeURIComponent(row.collected_at||'imported')}" alt="${esc(row.name||'用户')}的头像" loading="lazy">`:''}</span>`}
const sourceNames={intro:'简介强信号',video_title:'视频标题强信号',video_tag:'视频标签强信号',avatar:'头像模型'};
function candidatesFirst(config=state?.config){return config?.collection_scope!=='seed_cycle'}
function fastEvidence(config=state?.config){return config?.evidence_mode!=='full'}
function collectionDescription(config=state?.config){return candidatesFirst(config)?'先处理全局待判定候选；自动积压清空后，再继续种子关系采集。':'按种子周期抓取粉丝与关注，再检查当前种子的关联候选；没有种子时从全局候选启动。'}
function evidenceDescription(config=state?.config){return fastEvidence(config)?'快速判定先检查简介与头像；任一强信号确认后跳过后续视频请求，未命中时再检查视频标题与标签。':'完整采样检查简介、头像及近期视频标题与标签；任一强信号即可确认。'}
function identityScore(row){
  const value=row.identity_score??row.evidence?.identity_score;
  if(value===undefined||value===null||(!row.collected_at&&row.evidence?.identity_score===undefined&&row.evidence?.identity_components===undefined))return null;
  return Number.isFinite(Number(value))?Math.max(0,Math.min(100,Number(value))):null;
}
function identityDetails(row){
  const score=identityScore(row),parts=row.evidence?.identity_components||{};
  const components=[['头像 · 40 分','avatar'],['简介 · 20 分','intro'],['视频标题 · 20 分','video_title'],['视频标签 · 20 分','video_tag']];
  return `<div class="drawer-block"><h3>Furry 匹配分</h3><div class="score-total">${score===null?'待评分':dec(score)}${score===null?'':'<small>/ 100</small>'}</div><div class="score-breakdown">${components.map(([label,key])=>`<div><b>${Number.isFinite(parts[key])?dec(parts[key]):'未评分'}</b><small>${label}</small></div>`).join('')}</div><p class="form-note">头像分数 × 40，加上简介、视频标题、视频标签的强信号各 20 分。只按已保存证据计分，快速判定跳过的项目不补分；用于选择种子，不是身份概率。</p></div>`;
}
function evidenceLabel(row){
  if(row.manual==='yes')return '人工确认';if(row.manual==='no')return '人工排除';
  const sources=row.evidence?.decision_sources;
  if(Array.isArray(sources)&&sources.length)return sources.map(key=>sourceNames[key]||key).join('、');
  return row.evidence?.model?modelNames[row.evidence.model]||'头像模型':row.collection_status==='needs_review'?'待人工审核':'待检查身份';
}
function candidateStatus(row){return `<span class="status-badge ${row.collection_status==='needs_review'?'warning':'neutral'}">${row.collection_status==='needs_review'?'待人工审核':row.collection_status==='failed'?'检查失败':'待身份判定'}</span><small>确认 Furry 后才遍历关系</small>`}
function keywordLine(label,words){return `<p class="evidence-line"><b>${esc(label)}</b>${Array.isArray(words)&&words.length?words.map(esc).join('、'):'未发现'}</p>`}
function evidenceDetails(row){
  const e=row.evidence||{},intro=e.intro,video=e.videos,avatarEvidence=e.avatar;
  const introReady=intro?.success===true,introStatus=introReady?'已获取':intro?.success===false?'未能获取':row.collected_at?'已获取，未保存关键词检查':'未获取';
  const introWords=introReady?keywordLine('强信号',intro.strong_keywords)+keywordLine('弱线索',intro.weak_keywords)+keywordLine('已忽略的否定词',intro.negated_keywords):'';
  const items=Array.isArray(video?.items)?video.items:[];
  const videoSkipped=video?.skipped===true||!!video?.skip_reason||!!video?.skipped_reason;
  const earlyVideoStop=videoSkipped||(video?.sample_complete===false&&!video?.error);
  const videoStatus=earlyVideoStop?(items.length?`已采样 ${number(video.sample_size??items.length)} 个视频 · 命中后停止`:'快速判定已跳过'):video?.success===true?`已采样 ${number(video.sample_size??items.length)} 个视频`:video?.success===false?'视频信息未能完整获取':'未获取';
  const videoSkipNote=earlyVideoStop?`<p class="form-note">${esc(video.skip_reason||video.skipped_reason||'已获得强信号，后续视频或标签请求已跳过。')} 跳过的内容没有作为空视频或空标签参与排除判定。</p>`:'';
  const videoItems=items.map(item=>`<div class="video-evidence-item"><b>${esc(item.title||'未获取标题')}</b>${item.bvid?`<small>${esc(item.bvid)}</small>`:''}<div class="evidence-tags">${Array.isArray(item.tags)&&item.tags.length?item.tags.map(tag=>`<span>${esc(tag)}</span>`).join(''):item.tags_skipped||item.tags_skip_reason||item.tags_skipped_reason?`<small>${esc(item.tags_skip_reason||item.tags_skipped_reason||'已命中强信号，跳过标签请求')}</small>`:item.tags_success===false||!Array.isArray(item.tags)?'<small>未获取标签</small>':'<small>未返回标签</small>'}${item.tags_success===false&&item.tags?.length?'<small>标签获取未完成</small>':''}</div></div>`).join('');
  const videoChecks=[['标题强信号',video?.strong_title_keywords],['标签强信号',video?.strong_tag_keywords],['弱线索',video?.weak_keywords],['已忽略的否定词',video?.negated_keywords]];
  const videoWords=videoChecks.filter(([,words])=>video?.success===true||(Array.isArray(words)&&words.length)).map(([label,words])=>keywordLine(label,words)).join('');
  const score=avatarEvidence?.furry_score??e.furry_score,hasScore=score!==undefined&&score!==null&&Number.isFinite(Number(score));
  const model=avatarEvidence?.model||e.model;
  const avatarStatus=avatarEvidence?.success===false?'识别失败':hasScore?'已识别':'未获取';
  const avatarData={状态:avatarStatus,模型:model?modelNames[model]||model:'未获取',Furry分数:hasScore?Number(score).toFixed(4):'未获取',确认阈值:e.threshold??'未获取',判定时间:e.classified_at?shortTime(e.classified_at):'未获取'};
  const avatarError=avatarEvidence?.error||e.error;
  return `<div class="drawer-block"><h3>判定依据</h3><p class="decision-source">${esc(evidenceLabel(row))}</p><p class="form-note">${esc(row.manual?row.manual_reason||'人工判定优先于自动检查。':e.automatic_reason||'尚未保存综合判定原因。')}</p><p class="form-note">当前采用宽松规则：简介、视频标题或标签、头像中任一强信号确认即可晋升；证据不足的留在候选中，明确不符合的归入 Furry No。</p></div><div class="drawer-block"><h3>简介检查 <span class="subtle">${esc(introStatus)}</span></h3><p class="drawer-intro">${esc(row.intro||((introReady||row.collected_at)?'简介为空':'未获取简介'))}</p>${introWords}${intro?.error?`<p class="evidence-error">${esc(intro.error)}</p>`:''}</div><div class="drawer-block"><h3>视频标题与标签 <span class="subtle">${esc(videoStatus)}</span></h3>${videoItems||'<p class="form-note">没有已保存的视频标题和标签。</p>'}${videoWords}${videoSkipNote}${video?.error?`<p class="evidence-error">获取错误：${esc(video.error)}</p>`:''}<p class="form-note">这里只展示实际采样并返回的内容；获取失败不能视为没有视频或没有标签。</p></div><div class="drawer-block"><h3>头像检查</h3><pre class="evidence-box">${esc(JSON.stringify(avatarData,null,2))}</pre>${avatarError?`<p class="evidence-error">识别错误：${esc(avatarError)}</p>`:''}<p class="form-note">头像识别失败不会直接判为非 Furry。</p></div>`;
}
function restrictedRelations(row){return ['followings','followers'].some(direction=>row[direction+'_state']==='unavailable')}
function traversalLabel(row){return restrictedRelations(row)?'公开获取受限':traversalNames[row.traversal]||'待遍历'}
function relationDirectionLabel(row,direction){return row[direction+'_state']==='unavailable'?'公开获取受限':row[direction+'_state']==='complete'?'已到公开列表末尾':`下次第 ${row[direction+'_page']} 页`}
function traversal(row){const name=traversalLabel(row);const tone=restrictedRelations(row)?'warning':row.traversal==='complete'?'':row.traversal==='partial'||row.traversal==='legacy_done'?'warning':row.traversal==='failed'?'error':'neutral';return `<span class="status-badge ${tone}">${esc(name)}</span><small>关注 ${esc(relationDirectionLabel(row,'followings'))} · 粉丝 ${esc(relationDirectionLabel(row,'followers'))}</small>`}
function setView(){
  const next=location.hash.slice(1);view=Object.hasOwn(titles,next)?next:'overview';page=1;profileRequest++;clearTimeout(searchTimer);resetQuickReviewScope(true);
  document.querySelectorAll('[data-route]').forEach(el=>el.classList.toggle('active',el.dataset.route===view));
  const [crumb,title,description]=titles[view];$('breadcrumb').textContent=crumb;$('pageTitle').innerHTML=title;$('pageDescription').textContent=description;
  $('overviewSection').hidden=view!=='overview';$('settingsSection').hidden=view!=='settings';$('dataSection').hidden=!profileViews.includes(view);
  $('scoringCard').hidden=view==='settings';$('exportButton').hidden=!profileViews.includes(view);
  $('basisFilter').hidden=view!=='yes';$('basisFilter').value='';
  $('quickReviewButton').hidden=!['candidates','yes'].includes(view);
  $('quickReviewButton').textContent=view==='yes'?'✓ 复核头像确认':'✓ 快速人工审核';
  $('quickReviewLaunchLabel').textContent=view==='yes'?'复核头像确认':'快速人工审核';
  for(const option of $('quickReviewMode').querySelectorAll('option'))option.hidden=view==='yes'?option.value!=='avatar_only':option.value==='avatar_only';
  $('quickReviewMode').value=view==='yes'?'avatar_only':'review';updateQuickReviewVisibility();
  if(profileViews.includes(view)){
    $('searchInput').value='';$('statusFilter').value='';$('sortSelect').value='score';
    const info={candidates:['CANDIDATE POOL','等待检查的候选','候选身份检查按采集设置安排；任一强信号确认后才晋升种子。'],seeds:['SEED POOL','等待探索的种子','关系采集按采集范围设置安排；待人工审核不会阻塞后续任务。'],yes:['FURRY YES','确认匹配的账号','已完成与待遍历的 Furry 账号统一保留。已完成记录不再进入待遍历种子池。'],no:['FURRY NO','保留排除记录','排除记录不进入采集队列；发现误判时，可在详情中确认 Furry 或恢复自动结果。']}[view];
    $('dataEyebrow').textContent=info[0];$('dataTitle').textContent=info[1];$('tableHelp').textContent=info[2];
    $('scoreColumn').textContent='Furry 匹配分';$('traversalColumn').textContent=view==='candidates'?'身份检查':'关系遍历';
    $('sortSelect').querySelector('option[value="score"]').textContent='Furry 匹配分优先';
    const empty={candidates:['还没有待检查的候选','添加 UID，或从参考资料提取候选。先检查身份，确认后才成为种子。'],seeds:['还没有待遍历的种子','先在候选页检查身份，或手动添加已明确确认的 Furry 账号。'],yes:['还没有确认的 Furry','模型确认或人工审核通过的账号会出现在这里。'],no:['还没有排除记录','模型或人工排除的 UID 将保留在这里，方便后续修正。']}[view];
    $('dataEmpty').querySelector('h3').textContent=empty[0];$('dataEmpty').querySelector('p').textContent=empty[1];
    $('dataEmpty').querySelector('button').hidden=!['candidates','seeds'].includes(view);
    $('importBenchmark').hidden=view!=='candidates';$('importLegacy').hidden=view!=='candidates';
    updateProfileHelp();loadProfiles().catch(showError);
  }
  if(view==='settings'&&state)fillSettings(state.config);
}
function updateCounts(){
  const counts=state.counts;
  for(const [id,key]of [['statCandidates','candidates'],['navCandidates','candidates'],['statSeeds','seeds'],['statYes','yes'],['statNo','no'],['statComplete','complete'],['navSeeds','seeds'],['navYes','yes'],['navNo','no']])$(id).textContent=number(counts[key]);
  $('statReview').textContent=`待判定 ${number(counts.pending_candidates??Math.max(0,counts.candidates-counts.review))} 个 · 待审核 ${number(counts.review)} 个`;
  $('statRelations').textContent=`已记录 ${number(state.relation_count)} 条关系${counts.restricted?` · 已结束种子中 ${number(counts.restricted)} 个公开获取受限`:''}`;
  $('quickReviewCount').textContent=view==='yes'?'逐个复核':`${number(counts.review)} 待审核`;
  const hist=state.histogram,max=Math.max(1,...hist);
  $('histogram').innerHTML=hist.map((n,i)=>`<div class="hist-bin"><b>${number(n)}</b><div class="hist-bar" data-height="${n/max*90}"></div><small>${i*20}–${i===4?100:(i+1)*20-1}</small></div>`).join('');
  document.querySelectorAll('.hist-bar').forEach(el=>el.style.height=Math.max(2,Number(el.dataset.height))+'px');
  $('leaders').innerHTML=state.leaders.length?state.leaders.map((r,i)=>`<div class="leader-row"><span class="leader-rank">0${i+1}</span><div class="leader-info"><a href="#yes" data-action="detail" data-uid="${esc(r.uid)}"><b>${esc(r.name||r.uid)}</b></a><small>直接发现 ${number(r.furry_found)} 个 Furry · 图谱收益分</small></div><b class="leader-score">${dec(r.score)}</b></div>`).join(''):'<div class="leader-empty"><span>◈</span><p>还没有确认的 Furry 种子</p><small>图谱收益仅用于查看发现成果，不决定种子选择</small></div>';
}
function updateJob(){
  const job=state?.job,running=!!state?.worker_active,labels={running:'采集中',stopping:'正在暂停',completed:'本批完成',paused:'已暂停',interrupted:'上次运行中断',blocked:'等待处理',failed:'运行失败'};
  const options=job?JSON.parse(job.options):state?.config||{};
  const max=options.max_users||10,processed=job?.processed||0,continuous=options.continuous===true;
  const queue=state?.queue||{},pending=queue.pending_candidates??state?.counts.pending_candidates??0,runnable=queue.runnable_candidates,selected=queue.selected_seed_uid;
  const priority=candidatesFirst(running?options:state?.config),backlog=queue.mode==='candidate_backlog',waitingBacklog=priority&&queue.mode==='waiting'&&pending>0,activeCycle=!!selected&&!backlog&&!waitingBacklog;
  const cycleTotal=Number(queue.cycle_total_candidates||0),cyclePending=Number(queue.cycle_pending_candidates||0),cycleRunnable=Number(queue.cycle_runnable_candidates||0),cycleReview=Number(queue.cycle_review_candidates||0),cycleChecked=Number(queue.cycle_checked_candidates||0);
  const restrictedDirections=[...new Set(Array.isArray(queue.cycle_restricted_directions)?queue.cycle_restricted_directions:[])].map(direction=>({followers:'粉丝',followings:'关注'}[direction])).filter(Boolean);
  const waiting=running&&job?.state==='running'&&continuous&&!activeCycle&&queue.mode==='waiting';
  const retrying=running&&job?.state==='running'&&activeCycle&&queue.mode==='waiting';
  const classifyingCycle=activeCycle&&queue.cycle_phase==='classify_candidates';
  $('jobBadge').textContent=running&&waitingBacklog?'等待候选重试':retrying?'等待本轮重试':waiting?'等待新任务':running&&job?.state==='running'&&continuous?'连续采集中':job?labels[job.state]||job.state:'尚未启动';
  $('jobTitle').textContent=job?.state==='stopping'?'正在保存进度并暂停':waiting?'等待新的待处理任务':running?job?.current_uid?`正在探索 UID ${job.current_uid}`:'正在准备采集':job?labels[job.state]||'本批已结束':'准备好开始了吗？';
  $('jobDescription').textContent=job?.message||(waiting?'当前没有可执行任务；新增候选或人工确认账号后，会自动继续。':running?collectionDescription(options):'添加 UID 与 Cookie 后，选择单批或连续采集。');
  if(running&&job?.state==='running'&&backlog){
    $('jobTitle').textContent=job.current_uid?`候选优先 · 判定 UID ${job.current_uid}`:'优先处理现有候选';
    $('jobDescription').textContent=`全局待判定 ${number(pending)} 个${runnable!==undefined?`，当前可执行 ${number(runnable)} 个`:''}；先完成这些候选的自动检查，再继续种子关系采集。待人工审核不计入自动积压。${selected?`种子 UID ${selected} 的周期进度已保留。`:''}`;
  }else if(running&&job?.state==='running'&&waitingBacklog){
    $('jobTitle').textContent='候选优先 · 等待失败任务重试';
    $('jobDescription').textContent=`剩余待判定 ${number(pending)} 个，当前均在等待重试${queue.candidate_wait_until?`，最早 ${shortTime(queue.candidate_wait_until)} 可继续`:''}。候选优先期间保留种子周期进度。`;
  }else if(retrying){
    $('jobTitle').textContent=`种子 UID ${selected} · 等待${classifyingCycle?'关联判定':'关系采集'}重试`;
    $('jobDescription').textContent=`本轮保留当前种子，${queue.selected_seed_wait_until?`将在 ${shortTime(queue.selected_seed_wait_until)} 后继续。`:'等待暂不可用的任务恢复。'}${classifyingCycle?`关联候选剩余 ${number(cyclePending)} 个，待人工审核 ${number(cycleReview)} 个。`:'粉丝与关注抓取完成后，再检查关联账号。'}`;
  }else if(running&&job?.state==='running'&&queue.mode==='classify_candidates'){
    $('jobTitle').textContent=`种子 UID ${selected} · 判定关联账号`;
    $('jobDescription').textContent=`已检查 ${number(cycleChecked)} / ${number(cycleTotal)} 个关联账号，剩余自动判定 ${number(cyclePending)} 个${cycleRunnable!==cyclePending?`（当前可执行 ${number(cycleRunnable)} 个）`:''}。本轮自动检查结束后，再选匹配分最高的种子；待人工审核 ${number(cycleReview)} 个不阻塞下一轮。`;
  }else if(running&&job?.state==='running'&&queue.mode==='expand_seed'){
    $('jobTitle').textContent=selected?`正在遍历种子 UID ${selected}`:'正在遍历选中的种子';
    $('jobDescription').textContent=`先获取当前种子可公开的粉丝与关注，已保存 ${number(cycleTotal)} 个去重关联账号；可获取方向结束后，集中检查头像、简介、视频标题与标签。`;
  }else if(running&&job?.state==='running'&&queue.mode==='bootstrap_candidates'){
    $('jobTitle').textContent=job.current_uid?`暂无种子 · 判定候选 UID ${job.current_uid}`:'暂无种子 · 从候选启动';
    $('jobDescription').textContent=`当前没有可遍历种子，先从全局候选检查身份，找到种子后进入逐轮采集。全局待判定 ${number(pending)} 个${runnable!==undefined?`，当前可执行 ${number(runnable)} 个`:''}。`;
  }
  if(running&&job?.state==='running'&&activeCycle&&restrictedDirections.length)$('jobDescription').textContent+=`${restrictedDirections.join('、')}因隐私限制不可获取，本轮仅判定已获取的公开用户，不反复重试受限方向。`;
  $('jobProgressText').textContent=backlog||waitingBacklog?`全局剩余待判定 ${number(pending)} 个 · 累计处理 ${number(processed)} 次`:classifyingCycle?`本轮关联已检查 ${number(cycleChecked)} / ${number(cycleTotal)} 个`:activeCycle?`本轮已发现 ${number(cycleTotal)} 个关联账号`:continuous?`累计处理 ${number(processed)} 次`:`${number(processed)} / ${number(max)} 次处理`;
  $('jobPhase').textContent=job?.phase||(running?'任务进行中':'等待启动');
  $('jobProgressTrack').hidden=activeCycle?!classifyingCycle:continuous;
  $('jobProgressBar').style.width=(classifyingCycle?(cycleTotal?Math.min(100,cycleChecked/cycleTotal*100):100):Math.min(100,processed/max*100))+'%';
  $('jobBatchInfo').hidden=!continuous&&!selected&&!priority;
  $('jobBatchInfo').textContent=`${priority?'候选优先':'种子周期'} · ${fastEvidence(running?options:state?.config)?'快速判定':'完整采样'} · 全局待判定 ${number(pending)} 个${activeCycle?` · 本轮待判定 ${number(cyclePending)} 个 · 待人工审核 ${number(cycleReview)} 个`:''} · 累计处理 ${number(processed)} 次 · 每批最多 ${number(max)} 次 · 每方向每次新增 ${number(options.pages_per_run||2)} 页`;
  const nextContinuous=state?.config.continuous===true;
  $('startButton').disabled=running;
  $('startButton').textContent=nextContinuous?job&&continuous?'▶ 继续连续采集':'▶ 开始连续采集':job?'▶ 开始下一批':'▶ 开始采集';
  $('stopButton').disabled=!running||job?.state==='stopping';
  if(state?.config.cooldown_until&&state.config.cooldown_until>new Date().toISOString()){
    $('jobDescription').textContent=`平台冷却至 ${shortTime(state.config.cooldown_until)}，请稍后再启动。`;$('startButton').disabled=true;
  }
  updateCollectionLabels();updateWorkers();
}
function updateWorkers(){
  const workers=state?.workers,configured=Number(workers?.configured??state?.config?.worker_count??1),automatic=workers?.mode==='auto'||configured===0,limit=Number(workers?.limit??20);
  const available=Number(workers?.available_accounts??state?.accounts?.filter(account=>account.enabled&&account.status!=='expired').length??0);
  const requested=Number(workers?.requested??(automatic?Math.min(limit,available):configured)),effective=Number(workers?.effective??Math.min(requested,available)),items=Array.isArray(workers?.items)?workers.items:[];
  const notes=[];
  if(automatic&&available>limit)notes.push(`自动模式最多使用 ${number(limit)} 个线程，其余账号继续参与轮换。`);
  if(!automatic&&effective<requested)notes.push('可用账号少于手动上限，按实际可用账号数分配。');
  if(!available)notes.push('当前没有可用账号，请在 Cookie 池中启用有效账号。');
  if(state?.worker_active){
    if(state?.queue?.mode==='expand_seed'||items.some(item=>item.kind==='seed'))notes.push('当前采集一个种子的粉丝与关注，关系采集使用一个线程；进入关联账号判定后再并行。');
    else notes.push('上限用于候选身份判定；任务或本批额度不足时，工作线程数会少于上限。各账号每次 API 前独立等待，网络耗时另计；头像匿名下载、无固定等待。风控或登录失效停止整轮。');
  }
  $('workersStatus').hidden=!state?.worker_active&&!automatic&&configured<2;
  const setting=automatic?'自动':`手动上限 ${number(configured)}`;
  $('workersStatus').innerHTML=`<div class="workers-summary"><b>${state?.worker_active?`${number(workers?.active??items.length)} 个线程工作中`:'采集已暂停'}</b><span>${setting} · 可用账号 ${number(available)} · 判定并发上限 ${number(effective)}</span></div>${items.map(item=>`<div class="worker-row"><span class="worker-slot">${number(item.slot)}</span><div><b>${item.kind==='seed'?'种子遍历':'候选判定'} · UID ${esc(item.uid||'等待分配')}</b><small>${esc(item.account_label||'账号 '+(item.account_id??''))} · ${esc(item.phase||'准备中')}</small></div></div>`).join('')}${notes.length?`<small class="workers-note">${notes.map(esc).join(' ')}</small>`:''}`;
}
function updateEvents(){
  $('events').innerHTML=state.events.length?state.events.map(r=>`<div class="event-row ${esc(r.level)}"><time>${shortTime(r.time)}</time><i class="event-dot"></i><span class="event-message">${esc(r.message)}</span>${r.uid?`<a href="#${profileViews.includes(view)?view:'seeds'}" class="event-uid" data-action="detail" data-uid="${esc(r.uid)}">${esc(r.uid)} ↗</a>`:''}</div>`).join(''):'<div class="events-empty">还没有采集动态。添加 UID 或启动采集后，这里会记录实际发生的操作。</div>';
}
function updateCookies(){
  const accounts=state.accounts,active=accounts.filter(x=>x.enabled&&x.status!=='expired').length;
  $('cookieSummary').innerHTML=`<span>账号 ${accounts.length}</span><span>启用候选 ${active}</span><span>已停用 ${accounts.filter(x=>!x.enabled).length}</span>`;
  const names={unverified:'未验证',valid:'验证有效',expired:'已失效',error:'验证失败'};
  $('cookieList').innerHTML=accounts.length?accounts.map((r,i)=>`<div class="cookie-row"><div class="account-avatar">${String(i+1).padStart(2,'0')}</div><div class="account-info"><b>${esc(r.label)}</b><small>${r.account_name?esc(r.account_name)+' · ':''}验证：${shortTime(r.checked_at)} · 分配 ${r.request_count} 次</small></div><span class="secret-mask" aria-label="Cookie 已隐藏">••••••••</span><span class="status-badge ${!r.enabled?'neutral':r.status==='expired'||r.status==='error'?'error':r.status==='valid'?'':'warning'}">${!r.enabled?'已停用':names[r.status]||'未验证'}</span><div class="cookie-actions"><div class="cookie-action-buttons"><button class="button secondary compact" data-action="validate-cookie" data-id="${esc(r.id)}" ${r.busy?'disabled title="正在采集或验证，待当前任务结束后可验证"':''}>验证</button><button class="button secondary compact" data-action="toggle-cookie" data-id="${esc(r.id)}" data-enabled="${r.enabled?'false':'true'}">${r.enabled?'停用':'启用'}</button><button class="button danger compact" data-action="delete-cookie" data-id="${esc(r.id)}" ${r.busy?'disabled aria-describedby="cookieBusy-'+esc(r.id)+'"':''}>删除</button></div>${r.busy?`<small class="cookie-busy-note" id="cookieBusy-${esc(r.id)}">${r.enabled?'使用中，请先停用，等待当前任务结束后删除。':'当前任务结束后即可删除。'}</small>`:''}</div></div>`).join(''):'<div class="leader-empty"><span>⚿</span><p>Cookie 池为空</p><small>在下方添加完整 Cookie，或从项目根目录导入</small></div>';
  if(cookieDeleteTarget)updateCookieDeleteControls();
}
function updateCookieDeleteControls(){
  const account=cookieDeleteTarget&&state?.accounts.find(row=>String(row.id)===cookieDeleteTarget.id);
  const message=cookieDeleteTarget&&!account?'该账号已不在 Cookie 池中，请关闭此窗口。':account?.busy?cookieBusyHint:'';
  $('cookieDeleteBusy').textContent=message;$('cookieDeleteBusy').hidden=!message;
  $('confirmCookieDelete').disabled=cookieDeleteSubmitting||!account||account.busy===true;
  $('confirmCookieDelete').textContent=cookieDeleteSubmitting?'正在删除…':'删除账号';
  $('cancelCookieDelete').disabled=cookieDeleteSubmitting;
}
function openCookieDelete(id){
  if(cookieDeleteSubmitting||$('cookieDeleteDialog').open)return;
  const account=state?.accounts.find(row=>String(row.id)===String(id));
  if(!account){toast('该账号已不在 Cookie 池中',true);return}
  if(account.busy){toast(cookieBusyHint,true);return}
  cookieDeleteTarget={id:String(account.id),label:account.label||'未命名账号'};
  $('cookieDeleteLabel').textContent=cookieDeleteTarget.label;
  $('cookieDeleteError').textContent='';$('cookieDeleteError').hidden=true;
  updateCookieDeleteControls();$('cookieDeleteDialog').showModal();$('cancelCookieDelete').focus();
}
function cancelCookieDelete(){
  if(cookieDeleteSubmitting)return;
  cookieDeleteTarget=null;$('cookieDeleteDialog').close();
}
async function confirmCookieDelete(){
  if(cookieDeleteSubmitting||!cookieDeleteTarget)return;
  const account=state?.accounts.find(row=>String(row.id)===cookieDeleteTarget.id);
  if(!account||account.busy){updateCookieDeleteControls();return}
  const id=cookieDeleteTarget.id;
  cookieDeleteSubmitting=true;$('cookieDeleteError').hidden=true;updateCookieDeleteControls();
  try{
    await api('/api/cookies/delete',{id});
    cookieDeleteTarget=null;$('cookieDeleteDialog').close();
    toast('账号已从 Cookie 池删除，已采集的 UID 与关系保留');await poll();
  }catch(error){$('cookieDeleteError').textContent=error.message||'删除失败，请重试';$('cookieDeleteError').hidden=false}
  finally{cookieDeleteSubmitting=false;updateCookieDeleteControls()}
}
function ensureCollectionSettings(){
  const form=$('settingsForm'),grid=form.querySelector('.settings-grid');
  if(!$('collectionScope'))grid.insertAdjacentHTML('afterbegin','<label>采集范围<select id="collectionScope" aria-describedby="collectionScopeHint"><option value="candidates_first">候选优先：先处理已有积压</option><option value="seed_cycle">种子周期：先遍历关系，再判定关联候选</option></select><small id="collectionScopeHint">候选优先会保留种子周期进度；待人工审核不阻塞自动积压处理。</small></label><label>证据采集<select id="evidenceMode" aria-describedby="evidenceModeHint"><option value="fast">快速判定：强信号命中后减少视频请求</option><option value="full">完整采样：检查简介、头像及近期视频</option></select><small id="evidenceModeHint">快速模式先检查简介与头像；命中后跳过视频，视频命中后跳过后续标签。排除仍要求证据完整。</small></label>');
  form.closest('.settings-card').querySelector('.section-header p').textContent='可选择先处理候选积压或继续种子周期；每批结束后保存已完成的判定与分页进度。';
  $('workerCount').closest('label').querySelector('small').textContent='填 0 自动分配，或填 1–20 设置线程上限。每个线程独占一个可用账号；账号不足时按实际数量运行，种子关系采集使用一个线程。';
  $('maxUsers').closest('label').querySelector('small').textContent='全局候选与关联候选判定都计入本批上限；连续模式结束一批后自动继续。';
  $('requestInterval').closest('label').querySelector('small').textContent='每个账号每次 API 前等待（含首次），网络耗时另计；头像匿名下载、无固定等待。这是项目设置，不是 B 站保证的安全频率。';
}
function updateCollectionLabels(){
  if(view==='overview')$('pageDescription').textContent=collectionDescription();
  if(view==='seeds')$('pageDescription').textContent=candidatesFirst()?'先处理现有待判定候选，积压清空后再继续按 Furry 匹配分选择种子。':'选择 Furry 匹配分最高的种子；抓完粉丝与关注、完成关联账号自动判定后，再选择下一位。';
  const scoreHint=$('scoringCard').querySelector('.scoring-content > .form-note');
  if(scoreHint)scoreHint.textContent='简介、视频标题与标签分别命中强信号时各计 20 分。匹配分按已保存证据计算，不是身份概率；快速模式跳过的证据不补分。'+collectionDescription()+' 待人工审核不阻塞后续任务。';
  updateProfileHelp();
}
function fillSettings(config){
  ensureCollectionSettings();
  $('continuous').checked=config.continuous===true;
  $('collectionScope').value=config.collection_scope||'candidates_first';$('evidenceMode').value=config.evidence_mode||'fast';
  $('workerCount').value=String(config.worker_count??1);
  $('maxUsers').value=config.max_users;$('pagesPerRun').value=config.pages_per_run;$('requestInterval').value=config.request_interval;
  $('modelSelect').value=config.model;$('positiveThreshold').value=config.positive_threshold;updateModelHint();
}
function updateModelHint(){
  const name=$('modelSelect').value,available=models[name],ready=available?.weights&&available?.runtime;
  $('modelStatus').textContent=ready?'模型就绪':available?.weights?'缺少运行依赖':'未找到模型权重';
  $('modelStatus').className='status-badge '+(ready?'':'warning');
  $('modelHint').textContent=ready?'本地权重可用，头像不会上传至第三方。':'请按 PANEL.md 配置模型与运行环境。';
  const threshold=Number($('positiveThreshold').value||.85);
  $('thresholdHint').textContent=`头像分数 ≥ ${threshold.toFixed(2)} 为强信号；简介或视频有强信号也可确认。低分不单独排除。`;
}
async function poll(){
  if(polling)return;polling=true;
  try{
    state=await api('/api/state');$('connection').innerHTML='<i></i> 本地已连接';$('connection').classList.remove('offline');$('fatal').hidden=true;
    updateCounts();updateJob();updateEvents();updateCookies();
    $('lastRefresh').textContent='最后刷新 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
    if(profileViews.includes(view))await loadProfiles();
  }catch(error){$('connection').innerHTML='<i></i> 连接已断开';$('connection').classList.add('offline');$('fatal').textContent='无法连接本地面板。请确认 panel.py 仍在运行，恢复后会自动重连。';$('fatal').hidden=false}
  finally{polling=false}
}
function profileFilters(){return {view,q:$('searchInput').value,status:$('statusFilter').value,sort:$('sortSelect').value,basis:view==='yes'?$('basisFilter').value:''}}
function profileFilterKey(){return new URLSearchParams(profileFilters()).toString()}
function avatarOnlyAutomatic(row){
  const sources=row.evidence?.decision_sources;
  return row.verdict==='yes'&&row.automatic==='yes'&&row.manual===null&&Array.isArray(sources)&&sources.length===1&&sources[0]==='avatar';
}
function updateProfileHelp(){
  if(view==='candidates')$('tableHelp').textContent=evidenceDescription()+' '+collectionDescription();
  if(view==='seeds')$('tableHelp').textContent=collectionDescription()+' 已完成记录保留在 Furry Yes；待人工审核不阻塞后续任务。';
  if(view==='yes'){
    const filtered=$('basisFilter').value==='avatar_only';
    $('tableHelp').textContent=filtered?'这里只列出仅凭头像模型自动确认、尚无人工判定的账号。可逐个审核；人工确认后移出此筛选，排除后移入 Furry No 并退出种子队列。':'已完成与待遍历的 Furry 账号统一保留。可筛选“仅凭头像自动确认”，逐个复核可能的误判。';
    $('dataEmpty').querySelector('h3').textContent=filtered?'没有符合筛选的头像确认账号':'还没有确认的 Furry';
    $('dataEmpty').querySelector('p').textContent=filtered?'可调整搜索或遍历状态；已人工确认、人工排除和有其他确认依据的账号不在此筛选中。':'模型确认或人工审核通过的账号会出现在这里。';
  }
}
async function loadProfiles(){
  if(!profileViews.includes(view))return;
  const snapshotView=view,snapshotPage=page,scope=profileFilterKey(),request=++profileRequest;
  const query=new URLSearchParams({...profileFilters(),page:String(page)});
  const data=await api('/api/profiles?'+query);
  if(view!==snapshotView||page!==snapshotPage||scope!==profileFilterKey()||request!==profileRequest)return;
  total=data.total;pageSize=data.page_size;$('dataCount').textContent=`共 ${number(total)} 个 UID`;
  $('dataEmpty').hidden=total>0;$('profileRows').innerHTML=data.results.map(row=>{
    const score=row.evidence?.furry_score,matchScore=identityScore(row);
    return `<tr><td><div class="user-cell">${avatar(row)}<div><a href="#${view}" data-action="detail" data-uid="${esc(row.uid)}" class="user-name">${esc(row.name||'待获取资料')}</a><span class="user-id">UID ${esc(row.uid)}</span></div></div></td><td class="classification"><span class="mini-state ${esc(row.verdict)}">${esc(evidenceLabel(row))}</span><small>${score===undefined?(row.collection_status==='needs_review'?'等待人工审核':row.verdict==='unknown'?'尚无强信号确认':row.manual?'人工优先':'已有分类记录'):`头像分数 ${Number(score).toFixed(3)}`}</small></td><td class="traversal-cell">${view==='candidates'?candidateStatus(row):traversal(row)}</td><td><b class="discovery-count">${number(row.furry_found)}</b><small class="discovery-meta">已判定关系 ${number(row.evaluated_found)}</small></td><td class="score-cell"><b>${matchScore===null?'待评分':dec(matchScore)}</b><span class="score-meter"><i data-width="${matchScore??0}"></i></span><small>${matchScore===null?'尚无身份评分证据':'证据得分 / 100 · 不是概率'}</small></td><td><div class="row-actions">${view==='candidates'||view==='yes'&&avatarOnlyAutomatic(row)?`<button data-action="quick-review" data-uid="${esc(row.uid)}">审核</button>`:''}<button data-action="detail" data-uid="${esc(row.uid)}">详情 ↗</button></div></td></tr>`;
  }).join('');
  document.querySelectorAll('.score-meter i').forEach(el=>el.style.width=el.dataset.width+'%');
  $('paginationText').textContent=`第 ${page} / ${Math.max(1,Math.ceil(total/pageSize))} 页 · ${number(total)} 条`;
  $('previousPage').disabled=page<=1;$('nextPage').disabled=page*pageSize>=total;
}
async function detail(user){
  const row=await api('/api/profile?uid='+encodeURIComponent(user));drawerUid=row.uid;
  $('drawerContent').innerHTML=`<div class="eyebrow">UID DETAILS</div><div class="drawer-heading">${avatar(row)}<div><h2>${esc(row.name||'待获取资料')}</h2><p>UID ${esc(row.uid)}</p></div></div><div class="drawer-block"><h3>身份与采集状态</h3><p><span class="mini-state ${esc(row.verdict)}">${row.verdict==='yes'?'Furry Yes':row.verdict==='no'?'Furry No':'候选 UID'}</span> <span class="status-badge neutral">${esc(row.verdict==='unknown'?'尚未扩散关系':traversalLabel(row))}</span></p><p class="form-note">来源：${esc(row.source)} · 父级：${esc(row.parent_uid||'手动录入 / 无')}<br>关注：${esc(relationDirectionLabel(row,'followings'))} · 粉丝：${esc(relationDirectionLabel(row,'followers'))}<br>最后采集：${shortTime(row.collected_at)}</p>${row.last_error?`<p class="alert">${esc(row.last_error)}</p>`:''}</div>${identityDetails(row)}<div class="drawer-block"><h3>图谱发现收益分</h3><div class="score-total">${dec(row.score)}<small>/ 100</small></div><div class="score-breakdown">${[['基础分','base_score'],['父级加成','inherited_score'],['发现收益','discovery_score'],['命中质量','quality_score']].map(([label,key])=>`<div><b>${dec(row[key])}</b><small>${label}</small></div>`).join('')}</div><p class="form-note">直接发现 ${row.furry_found} 个唯一 Furry，已判定关系 ${row.evaluated_found} 个。父级首次发现时收益分 ${dec(row.parent_score_snapshot)}。此分数用于查看关系发现成果，不参与当前种子选择。</p><div class="drawer-form-row"><label>调整手动基础分（0–20）<input type="number" id="detailBase" min="0" max="20" step="0.5" value="${Number(row.base_score)}"></label><button class="button secondary compact" data-action="save-base">保存</button></div></div><div class="drawer-block"><h3>人工审核</h3><label>判定备注<textarea id="reviewReason" rows="2" maxlength="500" placeholder="记录判定依据（可选）">${esc(row.manual_reason||'')}</textarea></label><div class="review-actions"><button class="button primary compact" data-action="decide" data-decision="yes">✓ 确认 Furry</button><button class="button danger compact" data-action="decide" data-decision="no">× 排除</button><button class="button secondary compact" data-action="decide" data-decision="clear">恢复自动判定</button></div><p class="form-note">人工结果优先；重跑模型不会覆盖。排除后，会同步撤回父级的发现收益。</p></div>${evidenceDetails(row)}<div class="drawer-block"><h3>关系记录 · ${number(row.relations_count)} 个唯一 UID</h3>${row.children.length?row.children.map(r=>`<div class="relations-item"><a href="#${view}" data-action="detail" data-uid="${esc(r.uid)}">${esc(r.name||r.uid)}</a><span>${r.direction==='followers'?'粉丝':r.direction==='followings'?'关注':'历史关系'} · ${esc(r.verdict)}</span></div>`).join(''):'<p class="form-note">尚无已保存关系。</p>'}<p class="form-note">最多显示 100 条方向记录；同一 UID 的粉丝与关注关系只计一次发现收益。</p><div class="review-actions"><button class="button secondary compact" data-action="requeue" ${row.verdict!=='yes'?'disabled':''}>↻ 重新遍历关系</button><button class="button secondary compact" data-action="refresh-detail">刷新详情</button></div></div>`;
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

function quickReviewScope(){
  if(view==='yes'&&$('basisFilter').value==='avatar_only')return {...profileFilters(),mode:'avatar_only'};
  if(view==='candidates')return {view:'candidates',basis:'',q:'',status:$('quickReviewMode').value==='review'?'review':'',sort:'score',mode:$('quickReviewMode').value,table:profileFilterKey()};
  return null;
}
function quickReviewScopeCurrent(key){const scope=quickReviewScope();return quickReviewOpen&&scope&&JSON.stringify(scope)===key&&quickReviewScopeKey===key}
function ensureQuickReviewScope(){
  const scope=quickReviewScope();if(!scope)return null;
  const key=JSON.stringify(scope);
  if(quickReviewScopeKey!==key){resetQuickReviewScope();quickReviewScopeKey=key}
  return {...scope,key};
}
function resetQuickReviewScope(close=false){
  quickReviewRequest++;quickReviewLoading=false;quickReviewRow=null;quickReviewSeen.clear();quickReviewScopeKey='';
  if(close)quickReviewOpen=false;
  $('quickReviewReason').value='';$('quickReviewPosition').textContent='待审核';
  $('quickReviewBody').innerHTML='';quickReviewError();quickReviewControls();updateQuickReviewVisibility();
}
function quickReviewEligible(row,scope,direct=false){
  if(scope.view==='candidates')return row.verdict==='unknown'&&(direct||scope.mode!=='review'||row.collection_status==='needs_review');
  if(!avatarOnlyAutomatic(row))return false;
  if(scope.status==='review'&&row.collection_status!=='needs_review')return false;
  if(['pending','partial','complete','failed','legacy_done'].includes(scope.status)&&row.traversal!==scope.status)return false;
  if(scope.q){
    const pattern=scope.q.slice(0,100).split('').map(char=>char==='%'?'.*':char==='_'?'.':char.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('');
    const match=new RegExp(pattern,'i');if(!match.test(String(row.uid))&&!match.test(row.name||''))return false;
  }
  return true;
}
function updateQuickReviewVisibility(){
  const visible=view==='candidates'||view==='yes';
  $('quickReviewCount').textContent=view==='yes'?'逐个复核':`${number(state?.counts.review)} 待审核`;
  $('quickReviewPanel').hidden=!visible||!quickReviewOpen;
  $('quickReviewLauncher').hidden=!visible||quickReviewOpen;
  $('quickReviewLauncher').setAttribute('aria-expanded',String(quickReviewOpen));
  $('quickReviewExpanded').hidden=quickReviewMinimized;
  $('quickReviewPanel').classList.toggle('minimized',quickReviewMinimized);
  $('minimizeQuickReview').textContent=quickReviewMinimized?'＋':'−';
  $('minimizeQuickReview').setAttribute('aria-label',quickReviewMinimized?'展开审核窗':'收起审核窗');
  if(visible&&quickReviewOpen)requestAnimationFrame(clampQuickReviewPosition);
}
function quickReviewControls(){
  const disabled=quickReviewLoading||quickReviewSubmitting;
  for(const id of ['quickReviewYes','quickReviewNo','quickReviewSkip'])$(id).disabled=disabled||!quickReviewRow;
  $('quickReviewReason').disabled=disabled||!quickReviewRow;
  $('quickReviewMode').disabled=disabled||view==='yes';$('reloadQuickReview').disabled=disabled;
}
function quickReviewError(message=''){$('quickReviewError').textContent=message;$('quickReviewError').hidden=!message}
function renderQuickReview(row){
  quickReviewRow=row;quickReviewError();$('quickReviewReason').value='';
  if(!row){
    $('quickReviewBody').innerHTML=`<div class="quick-review-empty"><span>✓</span><h3>这一轮已看完</h3><p>${view==='yes'?'当前筛选下没有未查看的头像确认账号。可调整搜索、遍历状态，或重新开始这一轮。':'暂时没有未查看的候选。可重新开始这一轮，或切换到“全部候选”。'}</p></div>`;
    quickReviewControls();return;
  }
  const e=row.evidence||{},score=identityScore(row),parts=e.identity_components||{},avatarEvidence=e.avatar||{},avatarScore=avatarEvidence.furry_score??e.furry_score;
  const hasAvatarScore=avatarScore!==undefined&&avatarScore!==null&&Number.isFinite(Number(avatarScore));
  const avatarUrl=row.image_path?`/avatar/${encodeURIComponent(row.uid)}?v=${encodeURIComponent(row.collected_at||'imported')}`:'';
  const video=e.videos,items=Array.isArray(video?.items)?video.items:[];
  const strong=[...(Array.isArray(e.intro?.strong_keywords)?e.intro.strong_keywords:[]),...(Array.isArray(video?.strong_title_keywords)?video.strong_title_keywords:[]),...(Array.isArray(video?.strong_tag_keywords)?video.strong_tag_keywords:[])];
  const weak=[...(Array.isArray(e.intro?.weak_keywords)?e.intro.weak_keywords:[]),...(Array.isArray(video?.weak_keywords)?video.weak_keywords:[])];
  const tagHtml=words=>[...new Set(words)].map(word=>`<span>${esc(word)}</span>`).join('');
  $('quickReviewPosition').textContent=`本轮已看 ${number(quickReviewSeen.size)} 位`;
  $('quickReviewBody').innerHTML=`<div class="quick-review-name"><div><h3>${esc(row.name||'资料尚未获取')}</h3><a href="https://space.bilibili.com/${encodeURIComponent(row.uid)}" target="_blank" rel="noopener noreferrer">UID ${esc(row.uid)} · 访问主页 ↗</a></div><span class="mini-state ${esc(row.verdict)}">${row.verdict==='yes'?'Furry Yes':row.verdict==='no'?'Furry No':row.collection_status==='needs_review'?'待人工审核':'待身份检查'}</span></div><div class="quick-review-profile"><button class="quick-review-avatar" data-action="quick-review-avatar" ${avatarUrl?'':'disabled'} title="${avatarUrl?'点击查看头像大图':'头像尚未采集'}" aria-label="${avatarUrl?'查看头像大图':'头像尚未采集'}">${avatarUrl?`<img src="${avatarUrl}" alt="${esc(row.name||'用户')}的头像">`:'<span>◇<small>头像尚未采集</small></span>'}${avatarUrl?'<small>点击放大 ↗</small>':''}</button><div class="quick-review-metrics"><div class="quick-review-match"><span>Furry 匹配分</span><b>${score===null?'待评分':dec(score)}${score===null?'':'<small>/ 100</small>'}</b></div><dl><div><dt>基础分</dt><dd>${dec(row.base_score)}</dd></div><div><dt>头像分数</dt><dd>${hasAvatarScore?Number(avatarScore).toFixed(3):'未评分'}</dd></div><div><dt>粉丝 / 关注</dt><dd>${row.fan_count===null||row.fan_count===undefined?'—':number(row.fan_count)} / ${row.follow_count===null||row.follow_count===undefined?'—':number(row.follow_count)}</dd></div></dl><p>基础分计入图谱收益；匹配分是证据得分。</p></div></div><div class="quick-review-parts">${[['头像','avatar',40],['简介','intro',20],['标题','video_title',20],['标签','video_tag',20]].map(([name,key,max])=>`<div><span>${name}</span><b>${Number.isFinite(parts[key])?dec(parts[key]):'—'}<small> / ${max}</small></b></div>`).join('')}</div><section class="quick-review-section quick-review-clues"><h4>判定线索 <span>${esc(evidenceLabel(row))}</span></h4><p>${esc(row.manual?row.manual_reason||'已有人工判定，人工结果优先。':e.automatic_reason||'尚未完成自动检查，可先查看已获取资料。')}</p>${strong.length?`<div class="evidence-tags review-strong">${tagHtml(strong)}</div>`:''}${weak.length?`<div class="evidence-tags">${tagHtml(weak)}</div>`:''}</section><section class="quick-review-section quick-review-introduction"><h4>简介 <span>${e.intro?.success===false?'获取失败':e.intro?.success===true||row.collected_at?'已获取':'未获取'}</span></h4><p class="quick-review-intro">${esc(row.intro||((e.intro?.success===true||row.collected_at)?'简介为空':'简介尚未采集'))}</p>${e.intro?.error?`<p class="evidence-error">${esc(e.intro.error)}</p>`:''}</section><section class="quick-review-section quick-review-videos"><h4>近期视频与标签 <span>${video?.success===true?`已采样 ${number(video.sample_size??items.length)} 个`:video?.success===false?'未完整获取':'未获取'}</span></h4>${items.map(item=>`<article class="quick-review-video"><b>${esc(item.title||'未获取标题')}</b>${item.bvid?`<a href="https://www.bilibili.com/video/${encodeURIComponent(item.bvid)}" target="_blank" rel="noopener noreferrer">${esc(item.bvid)} ↗</a>`:''}<div class="evidence-tags">${Array.isArray(item.tags)&&item.tags.length?tagHtml(item.tags):`<small>${item.tags_success===false||!Array.isArray(item.tags)?'标签尚未获取':'未返回标签'}</small>`}</div>${item.tags_success===false&&item.tags?.length?'<small>标签获取未完成</small>':''}</article>`).join('')||'<p>没有已保存的视频。未获取不等于没有内容。</p>'}${video?.error?`<p class="evidence-error">${esc(video.error)}</p>`:''}</section><div class="quick-review-meta"><span>${esc(avatarEvidence.model?modelNames[avatarEvidence.model]||avatarEvidence.model:e.model?modelNames[e.model]||e.model:'尚未运行头像模型')} · ${shortTime(row.collected_at)}</span><button class="text-link" data-action="detail" data-uid="${esc(row.uid)}">完整详情 ↗</button></div>${avatarEvidence.error?`<p class="evidence-error">头像识别：${esc(avatarEvidence.error)}</p>`:''}`;
  $('quickReviewBody').scrollTop=0;quickReviewControls();
}
async function loadQuickReviewUid(uid){
  const scope=ensureQuickReviewScope();if(!scope||quickReviewSubmitting)return;
  const request=++quickReviewRequest;quickReviewLoading=true;quickReviewRow=null;quickReviewControls();quickReviewError();
  $('quickReviewBody').innerHTML='<div class="quick-review-empty"><span>◌</span><p>正在读取审核资料…</p></div>';
  try{
    const row=await api('/api/profile?uid='+encodeURIComponent(uid));if(request!==quickReviewRequest||!quickReviewScopeCurrent(scope.key))return;
    if(!quickReviewEligible(row,scope,true)){quickReviewSeen.add(String(row.uid));quickReviewLoading=false;await nextQuickReview();toast('该账号的判定已变化，已跳到下一位');return}
    renderQuickReview(row);
  }
  catch(error){if(request===quickReviewRequest&&quickReviewScopeCurrent(scope.key)){renderQuickReview(null);quickReviewError(error.message||'无法读取资料')}}
  finally{if(request===quickReviewRequest){quickReviewLoading=false;quickReviewControls()}}
}
async function nextQuickReview(reset=false){
  if(quickReviewLoading||quickReviewSubmitting)return;
  const scope=ensureQuickReviewScope();if(!scope)return;
  if(reset)quickReviewSeen.clear();else if(quickReviewRow)quickReviewSeen.add(String(quickReviewRow.uid));
  const request=++quickReviewRequest;quickReviewLoading=true;quickReviewRow=null;quickReviewControls();quickReviewError();
  $('quickReviewReason').value='';$('quickReviewPosition').textContent=`本轮已看 ${number(quickReviewSeen.size)} 位`;
  $('quickReviewBody').innerHTML=`<div class="quick-review-empty"><span>◌</span><p>正在寻找下一位${scope.view==='yes'?'头像确认账号':'候选'}…</p></div>`;
  try{
    let nextPage=1;
    while(true){
      const query=new URLSearchParams({view:scope.view,basis:scope.basis,q:scope.q,status:scope.status,sort:scope.sort,page:String(nextPage)});
      const result=await api('/api/profiles?'+query);
      if(request!==quickReviewRequest||!quickReviewScopeCurrent(scope.key))return;
      for(const candidate of result.results){
        if(quickReviewSeen.has(String(candidate.uid)))continue;
        const row=await api('/api/profile?uid='+encodeURIComponent(candidate.uid));
        if(request!==quickReviewRequest||!quickReviewScopeCurrent(scope.key))return;
        if(!quickReviewEligible(row,scope)){quickReviewSeen.add(String(row.uid));continue}
        renderQuickReview(row);return;
      }
      if(!result.results.length||nextPage*(result.page_size||30)>=result.total)break;
      nextPage++;
    }
    renderQuickReview(null);
  }catch(error){if(request===quickReviewRequest&&quickReviewScopeCurrent(scope.key)){renderQuickReview(null);quickReviewError(error.message||'无法读取审核队列')}}
  finally{if(request===quickReviewRequest){quickReviewLoading=false;quickReviewControls()}}
}
function openQuickReview(uid){
  if(!['candidates','yes'].includes(view))return;
  if(view==='yes'&&$('basisFilter').value!=='avatar_only'){
    $('basisFilter').value='avatar_only';page=1;profileRequest++;resetQuickReviewScope();updateProfileHelp();loadProfiles().catch(showError);
  }
  quickReviewOpen=true;quickReviewMinimized=false;updateQuickReviewVisibility();
  if(uid)return loadQuickReviewUid(uid);
  if(!quickReviewRow&&!quickReviewLoading)return nextQuickReview();
}
function closeQuickReview(){resetQuickReviewScope(true)}
window.imageReviewHost={context:()=>profileFilters(),request:api,refresh:()=>poll(),hideQuick:closeQuickReview,
  openDetail:uid=>detail(uid)};
async function decideQuickReview(decision){
  if(!quickReviewRow||quickReviewLoading||quickReviewSubmitting)return;
  const scope=ensureQuickReviewScope();if(!scope||!quickReviewRow||!quickReviewEligible(quickReviewRow,scope,true))return;
  const uid=String(quickReviewRow.uid),request=quickReviewRequest,reason=$('quickReviewReason').value;quickReviewSubmitting=true;quickReviewControls();quickReviewError();
  try{
    if(scope.view==='yes'){
      const current=await api('/api/profile?uid='+encodeURIComponent(uid));
      if(request!==quickReviewRequest||!quickReviewScopeCurrent(scope.key))return;
      if(!quickReviewEligible(current,scope)){quickReviewRow=null;quickReviewSeen.add(uid);quickReviewSubmitting=false;toast('该账号已不符合此筛选，未覆盖其判定');await nextQuickReview();await poll();return}
    }
    await api('/api/decision',{uid,decision,reason});
    toast(decision==='yes'?(scope.view==='yes'?'已人工确认，移出仅凭头像筛选':'已人工确认 Furry，进入种子队列'):'已人工排除，移出种子队列并保留记录');
    const sameScope=request===quickReviewRequest&&quickReviewScopeCurrent(scope.key);
    if(sameScope){quickReviewSeen.add(uid);if(quickReviewRow&&String(quickReviewRow.uid)===uid)quickReviewRow=null}
    quickReviewSubmitting=false;
    if(sameScope&&!quickReviewRow&&!quickReviewLoading)await nextQuickReview();
    await poll();
  }catch(error){if(quickReviewOpen&&request===quickReviewRequest)quickReviewError(error.message||'判定未保存，请重试');else showError(error)}
  finally{
    quickReviewSubmitting=false;quickReviewControls();
    if(quickReviewOpen&&!quickReviewRow&&!quickReviewLoading&&request!==quickReviewRequest&&quickReviewScope())nextQuickReview();
  }
}
function clampQuickReviewPosition(){
  const panel=$('quickReviewPanel');if(panel.hidden||!panel.style.left)return;
  const rect=panel.getBoundingClientRect(),margin=12;
  panel.style.left=Math.max(margin,Math.min(rect.left,Math.max(margin,innerWidth-rect.width-margin)))+'px';
  panel.style.top=Math.max(margin,Math.min(rect.top,Math.max(margin,innerHeight-rect.height-margin)))+'px';
}

document.addEventListener('click',event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  event.preventDefault();const action=button.dataset.action;
  if(action==='add-seed')return addSeed();if(action==='close-seed')return $('seedDialog').close();
  if(action==='detail')return detail(button.dataset.uid).catch(showError);
  if(action==='quick-review')return openQuickReview(button.dataset.uid);
  if(action==='delete-cookie')return openCookieDelete(button.dataset.id);
  if(action==='quick-review-avatar'){
    if(!quickReviewRow?.image_path)return;
    $('quickReviewAvatarImage').src=`/avatar/${encodeURIComponent(quickReviewRow.uid)}?v=${encodeURIComponent(quickReviewRow.collected_at||'imported')}`;
    $('quickReviewAvatarTitle').textContent=quickReviewRow.name||`UID ${quickReviewRow.uid}`;
    return $('quickReviewAvatarDialog').showModal();
  }
  busy(button,async()=>{
    if(action==='toggle-cookie'){await api('/api/cookies/toggle',{id:button.dataset.id,enabled:button.dataset.enabled==='true'});toast('账号状态已更新')}
    if(action==='validate-cookie'){const result=await api('/api/cookies/validate',{id:button.dataset.id});toast(result.message,!result.valid)}
    if(action==='decide'){await api('/api/decision',{uid:drawerUid,decision:button.dataset.decision,reason:$('reviewReason').value});toast('人工判定已保存，相关图谱收益已重算');await detail(drawerUid)}
    if(action==='save-base'){await api('/api/base-score',{uid:drawerUid,base_score:Number($('detailBase').value)});toast('基础分已更新');await detail(drawerUid)}
    if(action==='requeue'){await api('/api/requeue',{uid:drawerUid});toast('已重新安排遍历，已有关系保留并去重');await detail(drawerUid)}
    if(action==='refresh-detail')await detail(drawerUid);
    if(action==='reference-seed'){await api('/api/seeds',{uids:button.dataset.uid,base_score:10,verdict:'unknown'});toast('已提取 UID 为待检查候选，旧分类与分数未带入');}
    await poll();
  });
});
document.addEventListener('error',event=>{
  if(event.target.tagName!=='IMG')return;
  const largeAvatar=event.target.closest('.quick-review-avatar');
  if(largeAvatar){largeAvatar.innerHTML='<span>◇<small>头像暂时无法读取</small></span>';largeAvatar.disabled=true;largeAvatar.title='本地头像暂时无法读取';return}
  if(event.target.closest('.avatar'))event.target.remove();
},true);
document.addEventListener('keydown',event=>{if(event.key==='Escape')closeDrawer()});
$('quickReviewButton').addEventListener('click',()=>openQuickReview());
$('quickReviewLauncher').addEventListener('click',()=>openQuickReview());
$('closeQuickReview').addEventListener('click',closeQuickReview);
$('minimizeQuickReview').addEventListener('click',()=>{quickReviewMinimized=!quickReviewMinimized;updateQuickReviewVisibility()});
$('quickReviewMode').addEventListener('change',()=>{resetQuickReviewScope();nextQuickReview(true)});
$('reloadQuickReview').addEventListener('click',()=>nextQuickReview(true));
$('quickReviewSkip').addEventListener('click',()=>nextQuickReview());
$('quickReviewYes').addEventListener('click',()=>decideQuickReview('yes'));
$('quickReviewNo').addEventListener('click',()=>decideQuickReview('no'));
$('closeQuickReviewAvatar').addEventListener('click',()=>$('quickReviewAvatarDialog').close());
$('quickReviewAvatarDialog').addEventListener('click',event=>{if(event.target===$('quickReviewAvatarDialog'))$('quickReviewAvatarDialog').close()});
$('quickReviewHandle').addEventListener('pointerdown',event=>{
  if(event.button!==0||event.target.closest('button,select,input,a,label')||innerWidth<=779)return;
  const panel=$('quickReviewPanel'),rect=panel.getBoundingClientRect();
  quickReviewDrag={pointer:event.pointerId,x:event.clientX,y:event.clientY,left:rect.left,top:rect.top};
  panel.style.left=rect.left+'px';panel.style.top=rect.top+'px';panel.style.right='auto';panel.style.bottom='auto';
  $('quickReviewHandle').setPointerCapture(event.pointerId);panel.classList.add('dragging');event.preventDefault();
});
$('quickReviewHandle').addEventListener('pointermove',event=>{
  if(!quickReviewDrag||event.pointerId!==quickReviewDrag.pointer)return;
  const panel=$('quickReviewPanel');panel.style.left=quickReviewDrag.left+event.clientX-quickReviewDrag.x+'px';panel.style.top=quickReviewDrag.top+event.clientY-quickReviewDrag.y+'px';clampQuickReviewPosition();
});
for(const eventName of ['pointerup','pointercancel','lostpointercapture'])$('quickReviewHandle').addEventListener(eventName,()=>{quickReviewDrag=null;$('quickReviewPanel').classList.remove('dragging')});
window.addEventListener('resize',clampQuickReviewPosition);
$('closeDrawer').addEventListener('click',closeDrawer);$('drawerBackdrop').addEventListener('click',closeDrawer);
$('addSeedButton').addEventListener('click',addSeed);$('refreshButton').addEventListener('click',()=>poll());
function profileFiltersChanged(){
  page=1;profileRequest++;resetQuickReviewScope(view==='yes'&&$('basisFilter').value!=='avatar_only');updateProfileHelp();
}
function reloadProfileScope(){loadProfiles().catch(showError);if(quickReviewOpen&&!quickReviewSubmitting)nextQuickReview(true)}
$('searchInput').addEventListener('input',()=>{clearTimeout(searchTimer);profileFiltersChanged();searchTimer=setTimeout(reloadProfileScope,250)});
for(const id of ['statusFilter','sortSelect','basisFilter'])$(id).addEventListener('change',()=>{clearTimeout(searchTimer);profileFiltersChanged();reloadProfileScope()});
$('previousPage').addEventListener('click',()=>{page=Math.max(1,page-1);loadProfiles().catch(showError)});
$('nextPage').addEventListener('click',()=>{page++;loadProfiles().catch(showError)});
$('exportButton').addEventListener('click',()=>{location.href='/api/export?'+new URLSearchParams(profileFilters())});
$('seedForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const result=await api('/api/seeds',{uids:$('seedUids').value,base_score:Number($('seedBase').value),verdict:$('seedVerdict').value});
  const destination=$('seedVerdict').value==='yes'?'seeds':'candidates';
  $('seedDialog').close();$('seedUids').value='';toast(`已添加 ${result.added} 个${destination==='seeds'?'已确认种子':'候选'}，跳过 ${result.duplicates} 个重复 UID`);
  if(view!==destination)location.hash=destination;await poll();
})});
$('cancelCookieDelete').addEventListener('click',cancelCookieDelete);
$('cookieDeleteDialog').addEventListener('cancel',event=>{event.preventDefault();cancelCookieDelete()});
$('cookieDeleteForm').addEventListener('submit',event=>{event.preventDefault();confirmCookieDelete()});
function cookieError(message=''){
  $('cookieError').textContent=message;$('cookieError').hidden=!message;
  $('cookieValue').setAttribute('aria-invalid',message?'true':'false');
}
function cookiePasteValue(text){
  const lines=text.trim().split(/\r\n|\r|\n/);
  const headers=lines.map((line,index)=>({match:line.match(/^[ \t]*cookie[ \t]*:[ \t]*(.*)$/i),index})).filter(item=>item.match);
  if(headers.length>1)throw new Error('检测到多个 Cookie 请求头，请只复制一条请求的 Cookie');
  if(headers.length===1){
    const {match,index}=headers[0];
    if(lines[index+1]?.trim()&&!/^[ \t]*[!#$%&'*+.^_`|~0-9A-Za-z-]+[ \t]*:/.test(lines[index+1]))throw new Error('Cookie 请求头被换行拆开，请重新复制完整的 Cookie 值');
    return match[1].trim();
  }
  if(lines.length>1)throw new Error('多行内容中未找到 Cookie: 请求头，请复制完整 Cookie 值或请求头');
  return text.trim();
}
function updateCookieLength(){
  $('cookieLength').textContent=`${Array.from($('cookieValue').value).length.toLocaleString('zh-CN')} 个字符`;
}
$('cookieValue').addEventListener('input',()=>{cookieError();updateCookieLength()});
$('cookieValue').addEventListener('paste',event=>{
  const text=event.clipboardData?.getData('text/plain');
  if(!text||(!/[\r\n]/.test(text)&&!/^\s*cookie\s*:/i.test(text)))return;
  event.preventDefault();
  try{$('cookieValue').value=cookiePasteValue(text);cookieError()}catch(error){$('cookieValue').value='';cookieError(error.message)}
  updateCookieLength();
});
$('cookieForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  cookieError();
  try{await api('/api/cookies',{label:$('cookieLabel').value,cookie:$('cookieValue').value})}
  catch(error){cookieError(error.message);throw error}
  $('cookieValue').value='';updateCookieLength();$('cookieLabel').value='';toast('账号已保存，建议先验证再采集');await poll();
})});
$('settingsForm').addEventListener('submit',event=>{event.preventDefault();busy(event.submitter,async()=>{
  const config={continuous:$('continuous').checked,collection_scope:$('collectionScope').value,evidence_mode:$('evidenceMode').value,worker_count:Number($('workerCount').value),max_users:Number($('maxUsers').value),pages_per_run:Number($('pagesPerRun').value),request_interval:Number($('requestInterval').value),model:$('modelSelect').value,positive_threshold:Number($('positiveThreshold').value)};
  await api('/api/settings',config);toast('采集设置已保存');await poll();
})});
$('modelSelect').addEventListener('change',updateModelHint);$('positiveThreshold').addEventListener('input',updateModelHint);
$('startButton').addEventListener('click',event=>busy(event.currentTarget,async()=>{if(collectorRevision<2)throw new Error('采集器已更新，请关闭原启动窗口并重新运行 start_panel.ps1，然后刷新页面。');await api('/api/jobs/start',{});toast('采集任务已启动');await poll()}));
$('stopButton').addEventListener('click',event=>busy(event.currentTarget,async()=>{await api('/api/jobs/stop',{});toast('已请求暂停，当前请求结束后保留分页退出');await poll()}));
$('importLegacy').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/import',{benchmark:false});toast(`载入 ${result.added} 条历史参考，没有沿用旧分类与分数`);await openReferences();await poll()}));
$('importBenchmark').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/import',{benchmark:true});toast(`导入 ${result.added} 个 UID；头像实验 UID 作为待检查候选`);await poll()}));
$('importCookies').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/cookies/import',{});toast(`导入 ${result.added} 个账号，跳过重复或格式无效条目 ${result.skipped} 个`);await poll()}));
$('viewReferences').addEventListener('click',()=>openReferences().catch(showError));
$('closeReferences').addEventListener('click',()=>$('referenceDialog').close());
$('loadReferenceFiles').addEventListener('click',event=>busy(event.currentTarget,async()=>{const result=await api('/api/import',{benchmark:false});toast(`载入 ${result.added} 条历史参考，未沿用旧分类`);await openReferences();await poll()}));
$('referenceSearch').addEventListener('input',()=>{clearTimeout(referenceTimer);referenceTimer=setTimeout(()=>{referencePage=1;openReferences().catch(showError)},250)});
$('referencePrevious').addEventListener('click',()=>{referencePage=Math.max(1,referencePage-1);openReferences().catch(showError)});
$('referenceNext').addEventListener('click',()=>{referencePage++;openReferences().catch(showError)});
window.addEventListener('hashchange',setView);
(async()=>{try{const boot=await api('/api/bootstrap');token=boot.token;models=boot.models;collectorRevision=Number(boot.collector_revision||0);$('projectPath').textContent=boot.project_root;await poll();if(state)fillSettings(state.config);setView();if(collectorRevision<2){$('fatal').hidden=false;$('fatal').textContent='采集器已更新。请关闭原启动窗口，重新运行 start_panel.ps1，再刷新页面后开始采集。'}}catch(error){$('fatal').hidden=false;$('fatal').textContent='面板初始化失败：'+error.message}setInterval(poll,4000)})();

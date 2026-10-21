/* The displayed page is a stable review batch: polling never changes its images. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const dialog = document.createElement('dialog');
  dialog.id = 'imageReviewDialog';
  dialog.className = 'image-review-dialog';
  dialog.setAttribute('aria-labelledby', 'imageReviewTitle');
  dialog.innerHTML = `
    <header class="image-review-header">
      <div><span class="eyebrow">IMAGE REVIEW</span><h2 id="imageReviewTitle">图片批量审核</h2></div>
      <div class="image-review-options">
        <label><span class="sr-only">审核方式</span><select id="imageReviewActionMode" aria-label="审核方式"><option value="exclude">排除模式</option><option value="select">选中模式</option></select></label>
        <label><span class="sr-only">审核范围</span><select id="imageReviewMode" aria-label="图片审核范围"><option value="review">待人工审核</option><option value="all">全部有图片候选</option><option value="avatar_only" hidden>仅凭头像自动确认</option></select></label>
        <label class="image-review-size">每页 <input id="imageReviewLimit" type="number" min="8" max="96" step="8" value="32" inputmode="numeric" aria-label="每页图片数量"> 张</label>
        <button id="imageReviewRestart" class="text-link" type="button">重新开始 ↻</button>
        <button id="imageReviewClose" class="icon-button" type="button" aria-label="关闭图片批量审核">×</button>
      </div>
    </header>
    <p id="imageReviewProgress" class="image-review-progress" aria-live="polite"></p>
    <div id="imageReviewError" class="image-review-error" role="alert" hidden></div>
    <div id="imageReviewNotice" class="image-review-notice" role="status" hidden></div>
    <section id="imageReviewGrid" class="image-review-grid" aria-label="本页待审核头像"></section>
    <section id="imageReviewDeferZone" class="image-review-defer-zone" aria-label="拖入此处暂缓判定">
      <div class="image-review-defer-heading"><b>暂缓区</b><span>只有拖入此框并松开才暂缓；框外松开不生效，点击暂缓图片可恢复</span><small id="imageReviewDeferCount">0 张</small></div>
      <div id="imageReviewDeferred" class="image-review-deferred" aria-label="本页暂缓图片"></div>
    </section>
    <footer class="image-review-footer">
      <div><p id="imageReviewInstructions">未标记确认 Furry · 点击标记 × 排除 · 拖入下方暂缓区才暂缓，不保存判定</p><span id="imageReviewCounts" aria-live="polite"></span></div>
      <div class="image-review-footer-actions"><button id="imageReviewRetry" class="button secondary" type="button" hidden>重试本次提交</button><button id="imageReviewReload" class="button secondary" type="button" hidden>重新载入本轮</button><button id="imageReviewCommit" class="button primary" type="button" disabled>确定并下一页 →</button></div>
    </footer>`;
  document.body.appendChild(dialog);

  let epoch = 0, requestNumber = 0, state = null, drag = null, opener = null;
  const cards = new Map();
  const host = () => window.imageReviewHost;
  const allowedView = () => ['candidates', 'yes'].includes(location.hash.slice(1));
  const setText = (id, value) => { $(id).textContent = value; };
  const message = (id, value) => { $(id).textContent = value; $(id).hidden = !value; };
  const isCurrent = (e, n) => state && dialog.open && epoch === e && requestNumber === n;
  const clearImageTimers = () => { cards.forEach(card => clearTimeout(card.imageTimer)); };
  const pageLimit = () => {
    const parsed = Number($('imageReviewLimit').value);
    return Number.isInteger(parsed) && parsed >= 8 && parsed <= 96 ? parsed : 32;
  };
  const imageUrl = value => {
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && url.pathname.startsWith('/avatar/') ? url.href : '';
    } catch (_) { return ''; }
  };
  function updateEntries() {
    document.querySelectorAll('[data-image-review="open"]').forEach(button => { button.hidden = !allowedView(); });
  }
  function resetMarks() {
    state.excluded.clear();
    if (state.actionMode === 'select') {
      for (const item of state.items) if (!state.deferred.has(item.uid)) state.excluded.add(item.uid);
    }
  }
  function update() {
    if (!state) return;
    const selecting = state.actionMode === 'select';
    dialog.classList.toggle('is-select-mode', selecting);
    setText('imageReviewInstructions', selecting
      ? '选中确认 Furry · 未选中全部排除 · 拖入下方暂缓区才暂缓，不保存判定'
      : '未标记确认 Furry · 点击标记 × 排除 · 拖入下方暂缓区才暂缓，不保存判定');
    let excluded = 0, deferred = 0;
    for (const item of state.items) {
      if (state.deferred.has(item.uid)) deferred++;
      else if (state.excluded.has(item.uid)) excluded++;
    }
    const confirmed = state.items.length - excluded - deferred;
    setText('imageReviewCounts', `${confirmed} 张确认 Furry　${excluded} 张排除　${deferred} 张暂缓${state.pending.size ? `　· ${state.pending.size} 张加载中` : ''}`);
    setText('imageReviewDeferCount', `${deferred} 张`);
    $('imageReviewCommit').disabled = state.busy || state.conflict || !state.batchId || !state.items.length || state.pending.size > 0;
    $('imageReviewCommit').textContent = state.submitting ? '正在保存本页…' : '确定并下一页 →';
    $('imageReviewRestart').disabled = state.busy || !!state.pendingCommit;
    $('imageReviewMode').disabled = state.busy || !!state.pendingCommit;
    $('imageReviewActionMode').disabled = state.busy || state.conflict || !!state.pendingCommit;
    $('imageReviewActionMode').value = state.actionMode;
    $('imageReviewClose').disabled = state.submitting;
    $('imageReviewRetry').disabled = state.busy;
    $('imageReviewReload').disabled = state.busy;
    for (const {element, tile, cross} of cards.values()) {
      const uid = element.dataset.uid, isDeferred = state.deferred.has(uid), isFailed = state.failed.has(uid);
      const selected = selecting && !isDeferred && !state.excluded.has(uid);
      element.classList.toggle('is-excluded', !selecting && !isDeferred && state.excluded.has(uid));
      element.classList.toggle('is-selected', selected);
      element.classList.toggle('is-deferred', isDeferred);
      element.classList.toggle('is-failed', isFailed);
      element.classList.toggle('is-loading', state.pending.has(uid));
      tile.disabled = state.busy || state.conflict || !!state.pendingCommit || state.pending.has(uid) || isFailed;
      tile.setAttribute('aria-pressed', String(selecting ? selected : !isDeferred && state.excluded.has(uid)));
      tile.setAttribute('aria-label', `UID ${uid}，${isFailed ? '图片加载失败，已暂缓' : isDeferred ? '已暂缓，点击恢复' : selecting ? selected ? '已选中确认 Furry，点击取消' : '未选中，将排除，点击选中' : state.excluded.has(uid) ? '已标记排除，点击取消' : '确认 Furry，点击标记排除'}`);
      tile.title = `UID ${uid} · ${isDeferred ? '点击恢复' : selecting ? '点击切换选中' : '点击切换排除'}，拖入下方暂缓区可暂缓`;
      cross.textContent = selecting ? '✓' : '×';
      const destination = isDeferred ? $('imageReviewDeferred') : $('imageReviewGrid');
      if (element.parentNode !== destination) destination.appendChild(element);
    }
  }
  function defer(uid, value = true) {
    if (!state || state.busy || state.conflict || state.pendingCommit || !cards.has(uid)) return;
    if (!value && state.failed.has(uid)) return;
    if (value) state.deferred.add(uid); else state.deferred.delete(uid);
    if (!value && state.actionMode === 'select') state.excluded.add(uid); else state.excluded.delete(uid);
    update();
  }
  function toggle(uid) {
    if (!state || state.busy || state.conflict || state.pendingCommit || state.pending.has(uid) || state.failed.has(uid)) return;
    if (state.deferred.has(uid)) { defer(uid, false); return; }
    if (state.excluded.has(uid)) state.excluded.delete(uid); else state.excluded.add(uid);
    update();
  }
  function finishImage(uid, success, currentEpoch, batchId) {
    if (!state || epoch !== currentEpoch || state.batchId !== batchId || !state.pending.has(uid)) return;
    clearTimeout(cards.get(uid)?.imageTimer);
    state.pending.delete(uid);
    if (!success) { state.failed.add(uid); state.deferred.add(uid); state.excluded.delete(uid); }
    update();
  }
  function layoutGrid() {
    if (!state) return;
    const width = window.innerWidth || 1400;
    const columns = width <= 700 ? 4 : width <= 1000 ? 8 : state.items.length > 48 ? 12 : state.items.length > 32 ? 10 : 8;
    $('imageReviewGrid').style.setProperty('--image-review-columns', String(columns));
    $('imageReviewGrid').style.setProperty('--image-review-rows', String(Math.max(1, Math.ceil(state.items.length / columns))));
  }
  function renderItems() {
    clearImageTimers(); cards.clear();
    $('imageReviewGrid').replaceChildren(); $('imageReviewDeferred').replaceChildren();
    layoutGrid();
    const currentEpoch = epoch, batchId = state.batchId;
    for (const item of state.items) {
      const uid = item.uid, element = document.createElement('div');
      element.className = 'image-review-card'; element.dataset.uid = uid;
      const tile = document.createElement('button');
      tile.type = 'button'; tile.className = 'image-review-tile'; tile.title = `UID ${uid} · 点击切换排除，拖入下方暂缓区可暂缓`;
      const img = document.createElement('img');
      img.alt = `UID ${uid} 的头像`; img.draggable = true; img.decoding = 'async'; img.referrerPolicy = 'no-referrer';
      const veil = document.createElement('span'); veil.className = 'image-review-veil'; veil.setAttribute('aria-hidden', 'true');
      const cross = document.createElement('span'); cross.className = 'image-review-cross'; cross.textContent = '×'; cross.setAttribute('aria-hidden', 'true');
      const failure = document.createElement('span'); failure.className = 'image-review-failure'; failure.textContent = '加载失败 · 已暂缓';
      tile.append(img, veil, cross, failure);
      element.append(tile); $('imageReviewGrid').appendChild(element);
      const record = {element, tile, cross, img, imageTimer: null}; cards.set(uid, record);
      tile.addEventListener('click', event => { if (drag?.moved) return; event.preventDefault(); toggle(uid); });
      img.addEventListener('load', () => finishImage(uid, img.naturalWidth > 0, currentEpoch, batchId));
      img.addEventListener('error', () => finishImage(uid, false, currentEpoch, batchId));
      img.addEventListener('dragstart', event => {
        if (!state || state.busy || state.conflict || state.pendingCommit || state.failed.has(uid) || state.pending.has(uid)) { event.preventDefault(); return; }
        drag = {uid, moved: true};
        event.dataTransfer.effectAllowed = 'copyMove';
        event.dataTransfer.setData('text/uri-list', img.src);
        event.dataTransfer.setData('text/plain', img.src);
        event.dataTransfer.setData('application/x-furry-review-uid', uid);
        element.classList.add('is-dragging');
      });
      img.addEventListener('dragend', () => {
        element.classList.remove('is-dragging');
        for (const id of ['imageReviewDeferZone', 'imageReviewGrid']) $(id).classList.remove('is-drag-over');
        if (!drag || drag.uid !== uid) return;
        const finishedDrag = drag; setTimeout(() => { if (drag === finishedDrag) drag = null; }, 0);
      });
      const url = imageUrl(item.image_url);
      if (url) {
        record.imageTimer = setTimeout(() => finishImage(uid, false, currentEpoch, batchId), 25000);
        img.src = url;
        if (img.complete) finishImage(uid, img.naturalWidth > 0, currentEpoch, batchId);
      } else finishImage(uid, false, currentEpoch, batchId);
    }
    update();
  }
  async function loadPage({restart = false, notice = ''} = {}) {
    if (!state || state.busy || !host()?.request) return;
    state.busy = true; state.conflict = false; state.submitting = false;
    $('imageReviewRetry').hidden = true; $('imageReviewReload').hidden = true;
    message('imageReviewError', ''); message('imageReviewNotice', notice);
    setText('imageReviewProgress', '正在准备本页图片…'); update();
    const currentEpoch = epoch, number = ++requestNumber;
    try {
      const mode = state.scope.view === 'yes' ? 'avatar_only' : $('imageReviewMode').value;
      const scope = {...state.scope, mode};
      const result = await host().request('/api/image-review/page', {session_id: restart ? null : state.sessionId, scope, limit: pageLimit()});
      if (!isCurrent(currentEpoch, number)) return;
      if (!result || !Array.isArray(result.items) || !result.session_id || (!result.done && result.items.length && !result.batch_id)) throw new Error('审核页数据不完整，请重新载入。');
      const unique = new Set();
      state.items = result.items.map(item => ({...item, uid: String(item.uid)})).filter(item => { if (unique.has(item.uid)) throw new Error('审核页出现重复 UID，请重新载入。'); unique.add(item.uid); return true; });
      state.sessionId = result.session_id; state.batchId = result.batch_id || null; state.scope = scope;
      state.excluded.clear(); state.deferred.clear(); state.failed.clear(); state.pending = new Set(state.items.map(item => item.uid));
      resetMarks();
      state.loadedPage++; state.busy = false;
      renderItems();
      const missing = Number(result.missing_images || 0);
      setText('imageReviewProgress', state.items.length ? `第 ${state.loadedPage} 页 · 本页 ${state.items.length} 张 · 本轮剩余 ${Number(result.remaining || 0).toLocaleString('zh-CN')} 张${missing ? ` · ${missing.toLocaleString('zh-CN')} 个账号无可用图片，未参与图片审核` : ''}` : `这一轮已完成${missing ? ` · ${missing.toLocaleString('zh-CN')} 个账号无可用图片，未作判定` : ''}`);
      if (!state.items.length) message('imageReviewNotice', notice || '当前范围没有更多可审核图片。暂缓项未保存判定，可重新开始一轮再查看。');
    } catch (error) {
      if (!isCurrent(currentEpoch, number)) return;
      state.busy = false; state.conflict = true;
      message('imageReviewError', error.message || '无法载入图片审核页'); $('imageReviewReload').hidden = false;
    } finally { if (isCurrent(currentEpoch, number)) update(); }
  }
  async function commit() {
    if (!state || $('imageReviewCommit').disabled || !host()?.request) return;
    state.busy = true; state.submitting = true; $('imageReviewRetry').hidden = true;
    message('imageReviewError', ''); message('imageReviewNotice', ''); update();
    const currentEpoch = epoch, number = ++requestNumber;
    const payload = state.pendingCommit || {batch_id: state.batchId, excluded: [...state.excluded].filter(uid => !state.deferred.has(uid)), deferred: [...state.deferred]};
    state.pendingCommit = payload;
    try {
      const result = await host().request('/api/image-review/commit', payload);
      if (!isCurrent(currentEpoch, number)) return;
      state.busy = false; state.submitting = false; state.batchId = null; state.pendingCommit = null;
      state.lastCommitted = payload;
      const notice = `上一页已保存：确认 ${Number(result.confirmed || 0)}，排除 ${Number(result.excluded || 0)}，暂缓 ${Number(result.deferred || 0)}。`;
      Promise.resolve().then(() => host()?.refresh?.()).catch(() => {});
      await loadPage({notice});
    } catch (error) {
      if (!isCurrent(currentEpoch, number)) return;
      state.busy = false; state.submitting = false;
      if (error.status === 409 || error.code === 'review_conflict') {
        state.conflict = true; state.pendingCommit = null; $('imageReviewReload').hidden = false;
        message('imageReviewError', `${error.message || '本页账号状态或图片已变化'}。整页未保存；已保留你的选择，请重新载入本轮后审核。`);
      } else {
        $('imageReviewRetry').hidden = false;
        message('imageReviewError', `${error.message || '提交失败'}。本页选择已保留并锁定；请重试同一批次，重复提交不会重复写入。`);
      }
    } finally { if (isCurrent(currentEpoch, number)) update(); }
  }
  async function open() {
    if (!allowedView() || !host()?.context || !host()?.request) return;
    if (dialog.open) { $('imageReviewClose').focus(); return; }
    opener = document.activeElement;
    const context = host().context(), view = context.view;
    if (!['candidates', 'yes'].includes(view)) return;
    host().hideQuick?.();
    epoch++; requestNumber++; clearImageTimers(); cards.clear();
    const scope = {view, q: context.q || '', sort: context.sort || 'score', status: context.status || '', basis: view === 'yes' ? 'avatar_only' : '', mode: view === 'yes' ? 'avatar_only' : 'review'};
    state = {scope, actionMode: $('imageReviewActionMode').value === 'select' ? 'select' : 'exclude', sessionId: null, batchId: null, items: [], excluded: new Set(), deferred: new Set(), failed: new Set(), pending: new Set(), busy: false, submitting: false, conflict: false, pendingCommit: null, loadedPage: 0};
    $('imageReviewGrid').replaceChildren(); $('imageReviewDeferred').replaceChildren();
    for (const option of $('imageReviewMode').options) option.hidden = view === 'yes' ? option.value !== 'avatar_only' : option.value === 'avatar_only';
    $('imageReviewMode').value = scope.mode;
    setText('imageReviewTitle', view === 'yes' ? '图片批量复核 · 仅凭头像确认' : '图片批量审核');
    dialog.showModal(); update(); await loadPage();
  }
  function close(force = false) {
    if (state?.submitting && !force) { message('imageReviewNotice', '本页正在保存，请等待结果后关闭。'); return; }
    epoch++; requestNumber++; clearImageTimers(); cards.clear(); state = null; drag = null;
    if (dialog.open) dialog.close();
    if (!force && opener?.isConnected) opener.focus();
  }
  function restart() {
    if (!state || state.busy) return;
    state.loadedPage = 0;
    loadPage({restart: true, notice: '已重新开始这一轮，上一页未提交的选择未保存。'});
  }
  $('imageReviewCommit').addEventListener('click', commit);
  $('imageReviewRetry').addEventListener('click', commit);
  $('imageReviewReload').addEventListener('click', restart);
  $('imageReviewRestart').addEventListener('click', restart);
  $('imageReviewMode').addEventListener('change', restart);
  $('imageReviewActionMode').addEventListener('change', () => {
    if (!state) return;
    if (state.busy || state.conflict || state.pendingCommit) { $('imageReviewActionMode').value = state.actionMode; return; }
    const next = $('imageReviewActionMode').value === 'select' ? 'select' : 'exclude';
    if (next === state.actionMode) return;
    state.actionMode = next;
    resetMarks(); update();
    message('imageReviewNotice', `已切换为${next === 'select' ? '选中模式：只选中要确认 Furry 的头像，未选中将排除' : '排除模式：只标记要排除的头像，未标记将确认 Furry'}。本页选择已重置，暂缓项保留。`);
  });
  $('imageReviewLimit').addEventListener('change', () => {
    $('imageReviewLimit').value = pageLimit();
    if (state) message('imageReviewNotice', `每页 ${pageLimit()} 张，从下一页生效；当前图片与选择保持不变。`);
  });
  $('imageReviewClose').addEventListener('click', () => close());
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  for (const [id, deferred] of [['imageReviewDeferZone', true], ['imageReviewGrid', false]]) {
    $(id).addEventListener('dragover', event => { if (!drag || state?.busy || state?.conflict) return; event.preventDefault(); event.dataTransfer.dropEffect = 'move'; $(id).classList.add('is-drag-over'); });
    $(id).addEventListener('dragleave', event => { if (!$(id).contains(event.relatedTarget)) $(id).classList.remove('is-drag-over'); });
    $(id).addEventListener('drop', event => {
      $(id).classList.remove('is-drag-over');
      if (!drag || state?.busy || state?.conflict) return;
      event.preventDefault();
      if (state.deferred.has(drag.uid) !== deferred) defer(drag.uid, deferred);
    });
  }
  document.addEventListener('click', event => { if (event.target.closest?.('[data-image-review="open"]')) { event.preventDefault(); open(); } });
  window.addEventListener('hashchange', () => { close(true); updateEntries(); });
  window.addEventListener('resize', layoutGrid);
  window.imageBatchReview = {open, close: () => close()};
  updateEntries();
})();

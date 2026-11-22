const trainingToken = () => localStorage.getItem('cdt_token');
const trainingEndpoint = '/api/training';
const levelText = {normal: '一般', watch: '关注', major: '重大', critical: '临界'};
const batchStatusText = {sealing: '封存中', sealed: '已封存', failed: '封存失败'};

function tElement(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

async function tRequest(path = '', options = {}) {
  const response = await fetch(trainingEndpoint + path, {
    method: options.method || (options.body ? 'POST' : 'GET'),
    headers: {'Authorization': `Bearer ${trainingToken() || ''}`, 'Content-Type': 'application/json'},
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `操作失败 (${response.status})`);
  return data;
}

const launcher = tElement('button', '训练样本工作台');
launcher.type = 'button';
launcher.id = 'training-workbench-open';
launcher.hidden = true;
const dialog = tElement('dialog');
dialog.id = 'training-workbench';
dialog.setAttribute('aria-labelledby', 'training-workbench-title');
const header = tElement('header');
const title = tElement('h2', '训练样本工作台');
title.id = 'training-workbench-title';
const close = tElement('button', '关闭');
close.addEventListener('click', () => dialog.close());
header.append(title, close);
const feedback = tElement('p');
feedback.className = 'training-feedback';
feedback.setAttribute('role', 'status');
const root = tElement('div');
root.className = 'training-root';
dialog.append(header, feedback, root);
document.body.append(launcher, dialog);

let role = 'viewer';

function sessionChanged() {
  let signedIn = false;
  try { signedIn = Boolean(trainingToken()); } catch { signedIn = false; }
  launcher.hidden = !signedIn;
  if (!signedIn && dialog.open) dialog.close();
}
sessionChanged();
window.addEventListener('storage', sessionChanged);
setInterval(sessionChanged, 600);

function setFeedback(text, isError = false) {
  feedback.textContent = text;
  feedback.classList.toggle('error', isError);
}

function labelPill(label, source, status) {
  const pill = tElement('span', `${levelText[label] || label}（${source === 'model' ? '模型首判' : source === 'backfill' ? '靠泊补标' : '主管更正'}${status === 'superseded' ? ' · 已替代' : ''}）`);
  pill.className = `training-pill level-${label} ${status}`;
  return pill;
}

function renderSummary(wb) {
  const s = wb.summary;
  const section = tElement('section');
  section.className = 'training-summary';
  const cards = [
    ['案件总数', s.total_cases],
    ['有当前标签', s.active_labeled_cases],
    ['已入批案件', s.cases_in_sealed_batches],
    ['已封存批次', s.sealed_batches],
  ];
  for (const [name, value] of cards) {
    const card = tElement('div');
    card.append(tElement('span', name), tElement('strong', String(value)));
    section.append(card);
  }
  return section;
}

function renderBatches(wb) {
  const section = tElement('section');
  section.className = 'training-batches';
  section.append(tElement('h3', `训练批次（${wb.batches.length}）`));
  if (!wb.batches.length) {
    section.append(tElement('p', '尚无封存批次'));
    return section;
  }
  const list = tElement('div');
  for (const batch of wb.batches) {
    const row = tElement('article');
    row.className = 'training-batch-row';
    const head = tElement('div');
    head.append(tElement('b', batch.batch_no), (() => {
      const st = tElement('span', batchStatusText[batch.status] || batch.status);
      st.className = `training-batch-status ${batch.status}`;
      return st;
    })());
    const meta = tElement('small', `收录 ${batch.item_count} 条 · 截止 ${new Date(batch.cutoff_at).toLocaleString('zh-CN')} · 交班人 ${batch.created_by}`);
    row.append(head, meta);
    if (batch.failure_reason) row.append(tElement('p', `失败原因：${batch.failure_reason}`));
    const actions = tElement('div');
    actions.className = 'training-row-actions';
    const view = tElement('button', '查看收录');
    view.addEventListener('click', () => openBatch(batch.id));
    actions.append(view);
    if (batch.status === 'failed' && role === 'supervisor') {
      const retry = tElement('button', '重试封存');
      retry.addEventListener('click', () => retryBatch(batch.id, retry));
      actions.append(retry);
    }
    row.append(actions);
    list.append(row);
  }
  section.append(list);
  if (role === 'supervisor') {
    const seal = tElement('button', '封存新训练批');
    seal.className = 'training-primary';
    seal.addEventListener('click', () => sealNewBatch(seal));
    section.append(seal);
  }
  return section;
}

function renderOpenVessels(vessels) {
  const section = tElement('section');
  section.className = 'training-vessels';
  const open = vessels.filter(v => v.state !== 'closed');
  section.append(tElement('h3', `靠泊进行中的船次（${open.length}）`));
  if (!open.length) {
    section.append(tElement('p', '所有船次靠泊均已结束'));
    return section;
  }
  const list = tElement('div');
  for (const vessel of open) {
    const row = tElement('article');
    row.className = 'training-batch-row';
    row.append(
      tElement('b', vessel.call_no),
      tElement('span', `${vessel.vessel_name} · 泊位 ${vessel.berth}`),
    );
    if (role === 'supervisor') {
      const actions = tElement('div');
      actions.className = 'training-row-actions';
      const btn = tElement('button', '结束靠泊');
      btn.addEventListener('click', () => closeBerthing(vessel.id, btn));
      actions.append(btn);
      row.append(actions);
    } else {
      row.append(tElement('small', '需主管结束靠泊后才能补标'));
    }
    list.append(row);
  }
  section.append(list);
  return section;
}

function renderPending(wb) {
  const section = tElement('section');
  section.className = 'training-pending';
  section.append(tElement('h3', `待补标案件（${wb.pending_backfill.length}）`));
  if (!wb.pending_backfill.length) {
    section.append(tElement('p', '没有待补标案件'));
  } else {
    const list = tElement('div');
    for (const item of wb.pending_backfill) {
      const row = tElement('article');
      row.className = 'training-case-row';
      row.append(
        tElement('b', item.case_no),
        tElement('span', `${item.vessel} · ${item.vessel_name}`),
        tElement('small', item.has_feature_snapshot ? '已有首次分级特征快照' : '尚无特征快照（需先成功分级）'),
      );
      if (role === 'supervisor') {
        const form = tElement('form');
        const select = tElement('select');
        for (const value of wb.allowed_labels) select.add(new Option(levelText[value], value));
        const note = tElement('input');
        note.placeholder = '实际延误说明（至少 4 字）';
        note.minLength = 4;
        note.required = true;
        const submit = tElement('button', '提交补标');
        form.append(select, note, submit);
        form.addEventListener('submit', event => {
          event.preventDefault();
          backfill(item.id, {label: select.value, note: note.value}, submit);
        });
        row.append(form);
      }
      list.append(row);
    }
    section.append(list);
  }

  const noSnapshot = wb.labeled_without_snapshot || [];
  const sub = tElement('div');
  sub.append(tElement('h4', `有标签但缺特征快照（${noSnapshot.length}）`));
  if (noSnapshot.length) {
    const ul = tElement('ul');
    for (const item of noSnapshot) {
      ul.append(tElement('li', `${item.case_no} · ${item.vessel} · 当前标签：${levelText[item.active_label] || item.active_label}（需重新成功分级后才能入批）`));
    }
    sub.append(ul);
  }
  section.append(sub);
  return section;
}

async function refresh() {
  try {
    const [wb, vessels] = await Promise.all([
      tRequest('/workbench'),
      fetch('/api/vessels', {headers: {'Authorization': `Bearer ${trainingToken() || ''}`}}).then(r => r.json()),
    ]);
    root.replaceChildren(renderSummary(wb), renderBatches(wb), renderOpenVessels(vessels), renderPending(wb));
    setFeedback(wb.batches.some(b => b.status === 'failed') ? '存在封存失败的批次，可按原批次重试' : '工作台数据已更新');
  } catch (error) {
    setFeedback(error.message, true);
  }
}

async function closeBerthing(vesselId, button) {
  button.disabled = true;
  try {
    await tRequest(`/vessels/${vesselId}/close-berthing`, {body: {}});
    setFeedback('靠泊已结束，相关案件现在可以补标');
    await refresh();
  } catch (error) {
    setFeedback(error.message, true);
    button.disabled = false;
  }
}

async function sealNewBatch(button) {
  button.disabled = true;
  try {
    const detail = await tRequest('/batches/seal', {body: {}});
    setFeedback(`批次 ${detail.batch_no} 封存成功，收录 ${detail.item_count} 条`);
    await refresh();
  } catch (error) {
    setFeedback(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function retryBatch(batchId, button) {
  button.disabled = true;
  try {
    const detail = await tRequest('/batches/seal', {body: {batch_id: batchId}});
    setFeedback(`批次 ${detail.batch_no} 重试成功，收录 ${detail.item_count} 条`);
    await refresh();
  } catch (error) {
    setFeedback(error.message, true);
    button.disabled = false;
  }
}

async function backfill(caseId, payload, button) {
  button.disabled = true;
  try {
    await tRequest(`/cases/${caseId}/backfill`, {body: payload});
    setFeedback('补标已提交，下一批封存时收录');
    await refresh();
  } catch (error) {
    setFeedback(error.message, true);
    button.disabled = false;
  }
}

async function openBatch(batchId) {
  try {
    const detail = await tRequest(`/batches/${batchId}`);
    const versionCache = {};
    const overlay = tElement('div');
    overlay.className = 'training-modal';
    const panel = tElement('div');
    panel.className = 'training-modal-panel';
    const head = tElement('header');
    head.append(tElement('h3', `${detail.batch_no} · 收录明细（${detail.items.length}）`));
    const dismiss = tElement('button', '返回');
    dismiss.addEventListener('click', () => overlay.remove());
    head.append(dismiss);
    panel.append(head);
    const list = tElement('div');
    for (const item of detail.items) {
      if (!versionCache[item.case_id]) {
        try { versionCache[item.case_id] = await tRequest(`/cases/${item.case_id}/labels`); }
        catch { versionCache[item.case_id] = {versions: []}; }
      }
      const version = (versionCache[item.case_id].versions || []).find(v => v.id === item.label_version_id);
      const row = tElement('article');
      row.className = 'training-item-row';
      row.append(
        tElement('b', item.case_no),
        labelPill(item.label, version?.source || 'model', version?.status || 'active'),
        tElement('small', `标签版本 #${item.label_version_id} · 特征键 ${Object.keys(item.feature_snapshot || {}).length} 项`),
      );
      const pre = tElement('pre', JSON.stringify(item.feature_snapshot, null, 2));
      row.append(pre);
      list.append(row);
    }
    panel.append(list);
    overlay.append(panel);
    overlay.addEventListener('click', event => { if (event.target === overlay) overlay.remove(); });
    dialog.append(overlay);
  } catch (error) {
    setFeedback(error.message, true);
  }
}

launcher.addEventListener('click', async () => {
  dialog.showModal();
  setFeedback('正在加载工作台…');
  try {
    const me = await fetch('/api/me', {headers: {'Authorization': `Bearer ${trainingToken() || ''}`}}).then(r => r.json());
    role = me.role || 'viewer';
    await refresh();
  } catch (error) {
    setFeedback(error.message, true);
  }
});

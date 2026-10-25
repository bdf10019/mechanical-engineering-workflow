import { STEPS, statusLabels, safePhotoUrl, currentStep, nodeState, canAnalyze, parseDimensions, checkPhotoFiles, revisionConflict } from './workflow-state.mjs';

const $ = id => document.getElementById(id);
const state = { token: '', task: null, tasks: [], active: 'photos', connection: { configured: false }, dirty: false, busy: false, conflict: false, consent: false };
const e = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; };
const add = (parent, ...children) => { parent.append(...children.filter(Boolean)); return parent; };

function notice(message = '', type = '') { const node = $('notice'); node.replaceChildren(); node.className = `notice ${type}`; node.hidden = !message; if (message) node.append(e('span', '', message)); }
function report(error) {
  notice(error.message || '操作未完成，請再試一次。', 'error');
  if (error.conflict) { state.conflict = true; notice('這個任務已有較新版本。你的未儲存內容仍保留，請先核對，再重新載入。', 'warning'); const reload = button('重新載入最新版本', async () => { if (state.dirty && !confirm('重新載入會放棄這一頁尚未儲存的內容，確定繼續？')) return; await loadTask(state.task.id, false); }, 'secondary small'); $('notice').append(reload); }
}

async function api(path, data) {
  const response = await fetch(path, { method: data === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: data === undefined ? {} : { 'Content-Type': 'application/json', 'X-Session-Token': state.token }, ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) { const error = new Error(body.error?.message || body.error || body.message || `操作未完成（${response.status}）`); error.conflict = revisionConflict(response.status, body); throw error; }
  return body;
}

async function run(action) { if (state.busy) return; state.busy = true; document.body.classList.add('busy'); const controls = [...document.querySelectorAll('button:not(:disabled),select:not(:disabled),textarea:not(:disabled),input:not([type=file]):not(:disabled)')]; for (const node of controls) node.disabled = true; try { await action(); } catch (error) { report(error); } finally { state.busy = false; document.body.classList.remove('busy'); for (const node of controls) if (node.isConnected) node.disabled = false; } }
function button(text, action, className = '') { const node = e('button', `button ${className}`, text); node.type = 'button'; node.addEventListener('click', () => run(action)); return node; }
function field(label, input) { const wrap = e('div', 'field'); const lbl = e('label', '', label); if (input.id) lbl.htmlFor = input.id; else { input.id = `field-${crypto.randomUUID()}`; lbl.htmlFor = input.id; } return add(wrap, lbl, input); }
function input(value = '', kind = 'text') { const node = e('input'); node.type = kind; node.value = value ?? ''; return node; }
function textarea(value = '') { const node = e('textarea'); node.value = value || ''; return node; }
function markDirty() { state.dirty = true; state.consent = false; updateSaveStatus(); }
function updateSaveStatus() { $('save-status').textContent = state.dirty ? '● 尚未儲存' : state.task ? `已儲存 · 版本 ${state.task.revision}` : '資料只存在本機'; $('save-status').className = `save-status${state.dirty ? ' unsaved' : ''}`; }
function discardAllowed() { return !state.dirty || confirm('有尚未儲存的內容。離開此步驟會放棄這些變更，確定繼續？'); }
function go(id, focus = true) { if (!discardAllowed()) return; state.dirty = false; state.consent = false; state.active = id; render(); if (focus) $('work-panel').querySelector('h2')?.focus(); }
function endpoint(suffix = '') { return `/api/tasks/${encodeURIComponent(state.task.id)}${suffix}`; }
function setTask(task) { state.task = task; state.dirty = false; state.conflict = false; state.consent = false; }

async function refreshTasks() {
  const result = await api('/api/tasks'); state.tasks = Array.isArray(result) ? result : result.tasks || [];
  const select = $('task-select'); select.replaceChildren(new Option('選擇任務', ''));
  for (const task of state.tasks) select.append(new Option(task.name, task.id));
  select.value = state.task?.id || '';
}
async function loadTask(id, check = true) { if (check && !discardAllowed()) { $('task-select').value = state.task?.id || ''; return; } if (!id) return; setTask(await api(`/api/tasks/${encodeURIComponent(id)}`)); state.active = currentStep(state.task); notice(); render(); }
async function mutate(suffix, data) { const task = await api(endpoint(suffix), { expected_revision: state.task.revision, ...data }); setTask(task); return task; }

function renderGraph() {
  const nav = $('workflow'); nav.replaceChildren();
  STEPS.forEach((step, index) => {
    const node = e('div', 'node'); const data = nodeState(state.task, step.id); const btn = e('button', 'node-button'); btn.type = 'button'; btn.setAttribute('aria-current', state.active === step.id ? 'step' : 'false'); const helpID = `help-${step.id}`; btn.setAttribute('aria-describedby', helpID);
    const top = e('span', 'node-top'); const icon = e('span', `node-icon icon-${step.id}`, step.icon); icon.setAttribute('aria-hidden', 'true'); add(top, icon, e('span', 'node-number', `0${index + 1}`));
    add(btn, top, e('span', 'node-title', step.title), e('span', `node-status ${data.status}`, `${['done', 'complete', 'completed'].includes(data.status) ? '✓ ' : ''}${statusLabels[data.status] || '待處理'}`));
    const help = e('span', 'node-help', data.reason || step.help); help.id = helpID; help.setAttribute('role', 'tooltip'); btn.addEventListener('click', () => { if (!state.busy) go(step.id); }); add(node, btn, help); nav.append(node);
  });
}
function heading(step, suffix = '') { const wrap = e('div', 'panel-heading'); const title = e('h2', '', step.title); title.tabIndex = -1; add(wrap, add(e('div'), e('p', 'step-kicker', `STEP 0${STEPS.indexOf(step) + 1}`), title), suffix ? e('span', 'badge', suffix) : null); return wrap; }
function photoGrid(photos = state.task.photos, compact = false) {
  const grid = e('div', compact ? 'package-preview' : 'photos');
  for (const photo of photos || []) { const url = safePhotoUrl(photo.url, location.origin); if (!url) continue; const img = e('img'); img.src = url; img.alt = photo.name || '任務照片'; img.loading = 'lazy'; if (compact) { grid.append(img); continue; } const cap = e('figcaption', '', photo.name || '照片'); cap.append(e('span', 'photo-meta', `${photo.width || '—'} × ${photo.height || '—'} px`)); const remove = button('移除此張', async () => { if (!confirm('從本次選圖移除此張？已保存的原件仍會保留。')) return; await mutate('/remove-photo', { photo_id: photo.id }); render(); notice('已從本次選圖移除，原件仍保留於本機。'); }, 'secondary small'); cap.append(remove); grid.append(add(e('figure', 'photo'), img, cap)); }
  return grid;
}
function render() {
  $('task-title').textContent = state.task?.name || '開始一個任務'; $('task-select').value = state.task?.id || ''; updateSaveStatus(); renderGraph();
  const next = currentStep(state.task); const step = STEPS.find(item => item.id === next); const finalBlocked = state.task && ['model', 'drawing'].includes(next); $('next-label').textContent = finalBlocked ? `${step.title}尚未接通` : state.task ? step.title : '建立任務，放入照片'; $('next-button').textContent = finalBlocked ? '查看原因' : state.task ? '前往此步 →' : '開始 →';
  const panel = $('work-panel'); panel.replaceChildren();
  if (!state.task) { const blank = add(e('div', 'empty-state'), e('div', 'empty-icon', '▧'), e('h2', '', '放照片，開始工作'), e('p', '', '1–2 張照片，加上已有尺寸與修改要求。'), button('＋ 建立任務', () => openNew(), 'primary')); panel.append(blank); return; }
  const selected = STEPS.find(item => item.id === state.active); panel.append(heading(selected));
  const renders = { photos: renderPhotos, requirements: renderRequirements, analysis: renderAnalysis, review: renderReview, model: renderLocked, drawing: renderLocked }; renders[state.active](panel);
}

function openNew() { if (!discardAllowed()) return; $('new-dialog').showModal(); $('new-name').focus(); }
function renderPhotos(panel) {
  const grid = e('div', 'panel-grid'); const left = e('div', 'panel-stack'); const right = e('div');
  if (state.task.photos?.length) left.append(photoGrid());
  const file = input('', 'file'); file.className = 'file-input'; file.accept = 'image/jpeg,image/png'; file.multiple = true; file.id = 'photos-file';
  const addPhotos = button(state.task.photos?.length ? '＋ 加入照片' : '選擇照片', () => file.click(), 'secondary'); addPhotos.disabled = (state.task.photos?.length || 0) >= 2;
  const drop = add(e('div', 'drop-area'), e('span', 'drop-mark', '＋'), addPhotos, e('span', 'quiet', `${state.task.photos?.length || 0} / 2 張 · JPEG、PNG · 每張 20 MiB 內`), file);
  file.addEventListener('change', () => run(async () => {
    let completed = false;
    try {
      const files = checkPhotoFiles(file.files, state.task.photos?.length || 0);
      for (const item of files) {
        const base64 = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('照片讀取失敗。')); reader.readAsDataURL(item); });
        await mutate('/photos', { filename: item.name, base64 });
      }
      completed = true;
    } finally {
      file.value = '';
      // A previous file may already be committed when a later one is rejected.
      render();
      await refreshTasks();
    }
    if (completed) notice('照片已儲存於本機。');
  }));
  left.append(drop); add(right, e('h3', '', '本次資料'), summary([['照片', `${state.task.photos?.length || 0} 張`], ['尺寸', `${state.task.dimensions?.length || 0} 筆`], ['保存位置', '本機']]));
  add(grid, left, right); panel.append(grid); const next = button('下一步：尺寸與要求 →', () => go('requirements'), 'primary'); next.disabled = !state.task.photos?.length; panel.append(add(e('div', 'actions'), next));
}
function summary(rows) { const list = e('ul', 'summary-list'); for (const [name, value] of rows) list.append(add(e('li'), e('span', '', name), e('strong', '', value))); return add(e('div', 'summary-box'), list); }

function renderRequirements(panel) {
  const form = e('form'); const name = input(state.task.name); name.maxLength = 80;
  const changes = textarea(state.task.requirements?.changes); const keep = textarea(state.task.requirements?.keep); const goal = textarea(state.task.requirements?.goal);
  const top = e('div', 'panel-grid'); const left = e('div'); const right = e('div');
  add(left, field('任務名稱', name), field('需要修改什麼', changes), field('哪些不能改', keep)); add(right, field('預期交付', goal), e('h3', '', '照片'), photoGrid(state.task.photos, true)); add(top, left, right); form.append(top);
  const dimRows = e('div'); const rows = [];
  function addRow(data = {}) {
    const row = e('div', 'dimension-row'); const label = input(data.label || ''); label.setAttribute('aria-label', '尺寸名稱'); label.maxLength = 120; const value = input(data.value ?? '', 'number'); value.step = 'any'; value.min = '0.000001'; value.setAttribute('aria-label', '尺寸數值');
    const unit = e('select'); unit.setAttribute('aria-label', '尺寸單位'); for (const text of ['mm', 'cm']) unit.append(new Option(text, text)); unit.value = data.unit || 'mm';
    const source = e('select'); source.setAttribute('aria-label', '尺寸來源'); for (const [v, text] of [['user_provided', '已有精準數據'], ['simulated', '模擬數據'], ['reference', '來源參照']]) source.append(new Option(text, v)); source.value = data.source || 'user_provided';
    const record = { element: row, id: data.id, label, value, unit, source }; rows.push(record); const remove = button('×', () => { row.remove(); rows.splice(rows.indexOf(record), 1); markDirty(); }, ''); remove.className = 'icon-button'; remove.setAttribute('aria-label', '移除此筆尺寸'); add(row, label, value, unit, source, remove); dimRows.append(row);
    for (const item of [label, value, unit, source]) item.addEventListener('input', markDirty);
  }
  const title = e('h3', '', '已有尺寸'); const heads = e('div', 'dim-head'); for (const text of ['尺寸名稱', '數值', '單位', '數據來源', '']) heads.append(e('span', '', text)); add(form, title, heads, dimRows); for (const data of state.task.dimensions || []) addRow(data);
  const addDimension = button('＋ 加入尺寸', () => { addRow(); markDirty(); }); addDimension.className = 'text-button'; form.append(addDimension);
  const save = e('button', 'button primary', '儲存，前往 AI 判讀 →'); save.type = 'submit'; form.append(add(e('div', 'actions'), button('← 照片', () => go('photos'), 'secondary'), save));
  for (const item of [name, changes, keep, goal]) item.addEventListener('input', markDirty);
  form.addEventListener('submit', event => { event.preventDefault(); run(async () => { const dimensions = parseDimensions(rows.map(row => ({ id: row.id, label: row.label.value, value: row.value.value, unit: row.unit.value, source: row.source.value }))); await mutate('/save', { name: name.value.trim(), requirements: { changes: changes.value.trim(), keep: keep.value.trim(), goal: goal.value.trim() }, dimensions }); await refreshTasks(); state.active = 'analysis'; render(); notice('尺寸與要求已儲存。'); }); }); panel.append(form);
}

function renderAnalysis(panel) {
  if (!state.task.photos?.length) { blocked(panel, '請先放入照片。', 'photos'); return; }
  if (!state.task.prepared) { const grid = e('div', 'panel-grid'); add(grid, add(e('div'), e('h3', '', '本次判讀資料'), photoGrid(state.task.photos, true)), summary([['照片', `${state.task.photos.length} 張`], ['尺寸', `${state.task.dimensions?.length || 0} 筆`], ['狀態', '尚未建立資料版本']])); panel.append(grid); panel.append(add(e('div', 'actions'), button('← 修改尺寸與要求', () => go('requirements'), 'secondary'), button('準備判讀資料 →', async () => { await mutate('/prepare', {}); render(); notice('已建立本次資料版本。尚未傳送。'); }, 'primary'))); return; }
  const prepared = state.task.prepared; const grid = e('div', 'panel-grid'); const left = e('div'); const right = e('div');
  add(left, e('h3', '', '確認即將送出的內容'), photoGrid(state.task.photos, true)); const list = e('ul', 'package-list'); for (const text of [`任務名稱：${state.task.name}`, `照片 ${state.task.photos.length} 張（工作圖，移除 EXIF）`, `尺寸 ${state.task.dimensions?.length || 0} 筆`, '本次修改、保留項與交付要求']) list.append(e('li', '', text)); left.append(list);
  const req = state.task.requirements || {}; for (const [label, value] of [['修改', req.changes], ['保留', req.keep], ['交付', req.goal]]) if (value) left.append(add(e('div', 'response-block'), e('h3', '', label), e('p', 'quiet', value)));
  for (const dim of state.task.dimensions || []) left.append(e('p', 'quiet', `${dim.label}：${dim.value} ${dim.unit} · ${dim.source === 'simulated' ? '模擬數據' : dim.source === 'reference' ? '來源參照' : '已有精準數據'}`));
  add(right, e('h3', '', 'API 判讀'), summary([['服務', state.connection.configured ? 'OpenAI · 已設定（未驗證）' : '尚未設定'], ['模型', state.connection.model || '未設定'], ['輸出上限', state.connection.max_output_tokens ? `${state.connection.max_output_tokens} Tokens` : '未設定']]));
  const consent = input('', 'checkbox'); consent.id = 'send-consent'; const consentLabel = e('label', 'consent'); consentLabel.htmlFor = consent.id; add(consentLabel, consent, e('span', '', '我同意將以上照片、尺寸、要求與任務名稱送至 OpenAI，並同意本次可能產生的 API 費用。')); right.append(consentLabel); consentLabel.classList.add('details');
  const policy = e('p', 'field-note', '每份資料只嘗試一次。store:false 不等於零保存。'); const policyLink = e('a', '', '資料政策'); policyLink.href = 'https://developers.openai.com/api/docs/guides/your-data'; policyLink.target = '_blank'; policyLink.rel = 'noopener noreferrer'; add(policy, document.createTextNode(' '), policyLink); right.append(policy);
  const connectionId = state.connection.id;
  const send = button('確認資料並送出判讀', async () => { if (!canAnalyze(state.task, state.connection, consent.checked, state.dirty)) throw new Error('請先設定連線，並確認本次傳送。'); notice('正在判讀，請保留此頁。'); try { await mutate('/analyze', { prepared_id: prepared.id, sha256: prepared.sha256, expected_connection_id: connectionId, consent: true }); state.active = 'review'; render(); notice('已收到 AI 建議，請核對。'); } catch (error) { state.consent = false; try { setTask(await api(endpoint())); state.connection = await api('/api/connection'); renderConnection(); render(); } catch { prepared.attempted = true; render(); } throw error; } }, 'primary'); send.disabled = true;
  consent.addEventListener('change', () => { state.consent = consent.checked; send.disabled = !canAnalyze(state.task, state.connection, consent.checked, state.dirty); }); const actions = e('div', 'actions'); add(actions, send); if (!state.connection.configured) actions.append(button('設定連線', () => openConnection(), 'secondary')); right.append(actions); if (prepared.attempted) { consent.disabled = true; right.append(e('p', 'inline-note', '此份資料已使用一次 API 嘗試，沒有自動重試。可查看記錄或使用資料包。')); }
  const manual = e('details', 'details'); manual.append(e('summary', '', '改用資料包／匯入回覆')); const links = e('div', 'actions'); const download = e('a', 'button secondary small', '下載判讀資料包'); download.href = endpoint('/package'); download.download = ''; const template = e('a', 'button secondary small', '回覆格式'); template.href = endpoint('/response-template'); template.download = ''; add(links, download, template); manual.append(links);
  const file = input('', 'file'); file.className = 'file-input'; file.id = 'reply-file'; file.accept = '.json,application/json'; const importButton = button('匯入 JSON 回覆', () => file.click(), 'secondary small'); file.addEventListener('change', () => run(async () => { const item = file.files?.[0]; if (!item) return; if (item.size > 1024 * 1024) throw new Error('回覆檔案須小於 1 MiB。'); let reply; try { reply = JSON.parse(await item.text()); } catch { throw new Error('請選擇有效的 JSON 回覆。'); } await mutate('/import', { prepared_id: prepared.id, reply }); state.active = 'review'; render(); notice('回覆已匯入，尚未核對。'); })); manual.append(add(e('div', 'actions'), importButton, file)); right.append(manual);
  const detail = e('details', 'details'); add(detail, e('summary', '', '版本詳細資料'), e('pre', '', JSON.stringify({ id: prepared.id, sha256: prepared.sha256, revision: prepared.task_revision }, null, 2))); right.append(detail); add(grid, left, right); panel.append(grid);
  panel.append(add(e('div', 'actions'), button('← 返回尺寸與要求', () => go('requirements'), 'secondary')));
}

function renderReview(panel) {
  const response = state.task.response; if (!response) { blocked(panel, '尚未收到判讀建議。', 'analysis'); return; }
  const grid = e('div', 'panel-grid'); const left = e('div'); const right = e('div');
  for (const [title, items, numbered] of [['可見觀察', response.observations, false], ['缺少的資訊', response.missing_information, false], ['建議步驟', response.proposed_steps, true]]) {
    const box = e('div', 'response-block'); box.append(e('h3', '', title)); const list = e(numbered ? 'ol' : 'ul');
    if (!items?.length) list.append(e('li', '', '無提供內容。'));
    for (const item of items || []) { const li = e('li', '', typeof item === 'string' ? item : item.text || ''); if (typeof item !== 'string') { const refs = [...(item.photo_ids || []), ...(item.dimension_ids || [])]; if (refs.length) li.append(e('span', 'evidence-ref', `依據：${refs.join('、')}`)); } list.append(li); } box.append(list); left.append(box);
  }
  add(right, e('h3', '', '對照照片'), photoGrid(state.task.photos, true)); for (const dim of state.task.dimensions || []) right.append(e('p', 'quiet', `${dim.label}：${dim.value} ${dim.unit} · ${dim.source === 'simulated' ? '模擬數據' : dim.source === 'reference' ? '來源參照' : '已有精準數據'}`)); right.append(summary([['回覆來源', response.origin === 'openai_responses' ? 'OpenAI API' : '手動匯入（來源待核對）'], ['判讀狀態', response.status === 'needs_information' ? '待補資訊' : response.status === 'proposal' ? '建議待核對' : '待核對'], ['閱讀確認', state.task.review ? '已確認讀取' : '尚未確認']]));
  const note = e('p', 'inline-note', '確認已讀不代表工程核准。模型與工程圖仍未接通。'); right.append(note); if (!state.task.review) right.append(add(e('div', 'actions'), button('我已核對這份建議', async () => { await mutate('/review', { response_id: response.id, decision: 'acknowledge' }); render(); notice('已記錄閱讀確認；尚未產生工程模型。'); }, 'primary')));
  const detail = e('details', 'details'); add(detail, e('summary', '', '完整判讀資料'), e('pre', '', JSON.stringify(response, null, 2))); right.append(detail); add(grid, left, right); panel.append(grid); panel.append(add(e('div', 'actions'), button('← 修改尺寸與要求', () => go('requirements'), 'secondary'), button('查看模型步驟 →', () => go('model'), 'secondary')));
}
function blocked(panel, reason, back) { const box = e('div', 'locked-state'); add(box, e('span', 'lock-icon', '◇'), e('h3', '', reason)); if (back) box.append(button(`前往${STEPS.find(item => item.id === back).title} →`, () => go(back), 'secondary')); panel.append(box); }
function renderLocked(panel) { const step = STEPS.find(item => item.id === state.active); const reason = nodeState(state.task, state.active).reason || step.help; const box = add(e('div', 'locked-state'), e('span', 'lock-icon', step.icon), e('h3', '', '尚未接通'), e('p', '', reason)); panel.append(box); panel.append(add(e('div', 'actions'), button('返回核對建議', () => go('review'), 'secondary'))); }

function renderConnection() { const data = state.connection; $('connection-label').textContent = data.configured ? 'AI 已設定（未驗證）' : 'AI 未設定'; $('connection-button').classList.toggle('is-connected', Boolean(data.configured)); }
function openConnection() { $('model').value = state.connection.model || ''; $('output-limit').value = state.connection.max_output_tokens || 2048; $('api-key').value = ''; $('connection-dialog').showModal(); }
$('new-task').addEventListener('click', () => { if (!state.busy) openNew(); });
$('next-button').addEventListener('click', () => { if (state.busy) return; if (!state.task) openNew(); else go(currentStep(state.task)); });
$('connection-button').addEventListener('click', () => { if (!state.busy) openConnection(); });
for (const close of document.querySelectorAll('[data-close]')) close.addEventListener('click', () => $(close.dataset.close).close());
$('connection-dialog').addEventListener('close', () => { $('api-key').value = ''; });
$('task-select').addEventListener('change', event => run(() => loadTask(event.target.value)));
$('new-form').addEventListener('submit', event => { event.preventDefault(); run(async () => { const name = $('new-name').value.trim(); if (!name) throw new Error('請輸入任務名稱。'); setTask(await api('/api/tasks', { name })); state.active = 'photos'; $('new-dialog').close(); $('new-name').value = ''; await refreshTasks(); render(); notice('任務已建立。'); }); });
$('connection-form').addEventListener('submit', event => { event.preventDefault(); run(async () => { try { state.connection = await api('/api/connection', { provider: 'openai', model: $('model').value.trim(), api_key: $('api-key').value, max_output_tokens: Number($('output-limit').value) }); $('connection-dialog').close(); renderConnection(); state.consent = false; if (state.active === 'analysis') render(); notice('連線設定已保存在服務記憶體，尚未呼叫 API。'); } finally { $('api-key').value = ''; } }); });
$('disconnect').addEventListener('click', () => run(async () => { state.connection = await api('/api/disconnect', {}); $('connection-dialog').close(); renderConnection(); if (state.active === 'analysis') render(); notice('連線金鑰已清除。'); }));
window.addEventListener('beforeunload', event => { if (state.dirty || state.busy) { event.preventDefault(); event.returnValue = ''; } });
async function init() { try { const session = await api('/api/session'); state.token = session.token; state.connection = await api('/api/connection'); renderConnection(); await refreshTasks(); render(); } catch (error) { render(); report(error); } }
init();

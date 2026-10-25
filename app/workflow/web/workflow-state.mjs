export const STEPS = Object.freeze([
  { id: 'photos', title: '放入照片', icon: '▧', color: '#2971bc', background: '#eaf3ff', help: '放入同一物件的 1–2 張 JPEG 或 PNG 照片。' },
  { id: 'requirements', title: '尺寸與要求', icon: '↔', color: '#b27617', background: '#fff4df', help: '填寫已有尺寸、修改目標與必須保留的部分。' },
  { id: 'analysis', title: 'AI 判讀', icon: '✦', color: '#7651c5', background: '#f2ecff', help: '核對本次資料，再選擇 API 判讀或匯出資料包。' },
  { id: 'review', title: '核對建議', icon: '✓', color: '#288557', background: '#e9f7ef', help: '對照照片與尺寸，查看 AI 建議和缺少的資訊。' },
  { id: 'model', title: '建立模型', icon: '◇', color: '#a26847', background: '#f8eee7', help: '尚未接通照片到可編輯工程模型；不產生替代模型。' },
  { id: 'drawing', title: '工程圖', icon: '▤', color: '#56677d', background: '#edf1f6', help: '需先完成模型與工程檢查；本版尚不能出圖。' },
]);

export const statusLabels = Object.freeze({ empty: '未開始', ready: '可操作', current: '進行中', pending: '待處理', done: '已完成', complete: '已完成', completed: '已完成', blocked: '尚未接通', locked: '尚未就緒', unavailable: '尚未接通', needs_input: '待補資料', review: '待核對', warning: '待補資料' });

export function safePhotoUrl(value, origin) {
  if (typeof value !== 'string' || !value.startsWith('/api/tasks/')) return null;
  try { const url = new URL(value, origin); return url.origin === origin && url.pathname.startsWith('/api/tasks/') ? `${url.pathname}${url.search}` : null; } catch { return null; }
}

export function currentStep(task) {
  const id = typeof task?.next_step === 'string' ? task.next_step : task?.next_step?.id;
  return STEPS.some(step => step.id === id) ? id : 'photos';
}

export function nodeState(task, id) {
  if (!task) return { status: ['model', 'drawing'].includes(id) ? 'blocked' : id === 'photos' ? 'ready' : 'pending', reason: '' };
  const found = task.nodes?.find(node => node.id === id);
  return found || { status: ['model', 'drawing'].includes(id) ? 'blocked' : 'pending', reason: '' };
}

export function canAnalyze(task, connection, consent, dirty = false) {
  return Boolean(task?.prepared?.id && task.prepared.sha256 && !task.prepared.attempted && connection?.configured && consent === true && !dirty);
}

export function parseDimensions(rows) {
  return rows.map((row, index) => {
    const label = String(row.label || '').trim();
    const value = Number(row.value);
    if (!label || row.value === '' || !Number.isFinite(value) || value <= 0) throw new Error(`請核對第 ${index + 1} 筆尺寸的名稱與正數數值。`);
    if (value < .001 || value > 100000) throw new Error('尺寸數值須介於 0.001 與 100000 之間。');
    if (!['mm', 'cm'].includes(row.unit)) throw new Error('請選擇支援的尺寸單位。');
    return { id: row.id || null, label, value, unit: row.unit, source: row.source };
  });
}

export function checkPhotoFiles(files, existingCount) {
  const list = Array.from(files);
  if (!list.length) throw new Error('請選擇照片。');
  if (existingCount + list.length > 2) throw new Error('每個任務最多 2 張照片，請另建任務放入其他照片。');
  for (const file of list) {
    if (!['image/jpeg', 'image/png'].includes(file.type)) throw new Error('只接受 JPEG 或 PNG 照片。');
    if (file.size > 20 * 1024 * 1024) throw new Error('每張照片須小於 20 MiB。');
    if (file.size <= 0) throw new Error('不能匯入空白檔案。');
  }
  return list;
}

export function revisionConflict(status, body) {
  return status === 409 || ['revision_conflict', 'stale_revision', 'conflict'].includes(body?.code);
}

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { STEPS, safePhotoUrl, currentStep, nodeState, canAnalyze, parseDimensions, checkPhotoFiles, revisionConflict } from '../../app/workflow/web/workflow-state.mjs';

test('六個工具節點維持照片到工程圖順序', () => assert.deepEqual(STEPS.map(s => s.id), ['photos', 'requirements', 'analysis', 'review', 'model', 'drawing']));
test('未建任務由照片開始，模型與出圖明示未接通', () => { assert.equal(nodeState(null, 'photos').status, 'ready'); assert.equal(nodeState(null, 'requirements').status, 'pending'); assert.equal(nodeState(null, 'model').status, 'blocked'); assert.equal(nodeState(null, 'drawing').status, 'blocked'); });
test('未知下一步不跳到假完成', () => { assert.equal(currentStep({ next_step: 'finished' }), 'photos'); assert.equal(currentStep({ next_step: 'analysis' }), 'analysis'); });
test('後端節點原因原樣保留', () => assert.deepEqual(nodeState({ nodes: [{ id: 'model', status: 'blocked', reason: '尚無照片模型' }] }, 'model'), { id: 'model', status: 'blocked', reason: '尚無照片模型' }));
test('照片不容許外部網址與data執行內容', () => { assert.equal(safePhotoUrl('https://other.test/api/tasks/a/photo', 'http://127.0.0.1:8171'), null); assert.equal(safePhotoUrl('data:image/png;base64,abc', 'http://127.0.0.1:8171'), null); assert.equal(safePhotoUrl('/api/tasks/a/photos/b', 'http://127.0.0.1:8171'), '/api/tasks/a/photos/b'); });
test('判讀需要本次同意、固定版本與連線', () => { const task = { prepared: { id: 'p1', sha256: 'abc' } }; assert.equal(canAnalyze(task, { configured: true }, true), true); for (const params of [[task, { configured: false }, true], [task, { configured: true }, false], [{}, { configured: true }, true], [task, { configured: true }, true, true]]) assert.equal(canAnalyze(...params), false); });
test('尺寸保留ID與來源，不自動估尺寸', () => assert.deepEqual(parseDimensions([{ id: 'd1', label: ' 寬度 ', value: '24.5', unit: 'mm', source: 'simulated' }]), [{ id: 'd1', label: '寬度', value: 24.5, unit: 'mm', source: 'simulated' }]));
test('錯誤尺寸阻擋保存', () => { for (const value of ['', '0', '-1', 'NaN', 'Infinity']) assert.throws(() => parseDimensions([{ label: '長度', value, unit: 'mm' }])); assert.throws(() => parseDimensions([{ label: '', value: 3, unit: 'mm' }])); assert.throws(() => parseDimensions([{ label: 'a', value: 3, unit: 'ft' }])); });
test('照片一次最多兩張且不接受其他類型', () => { const file = { type: 'image/png', size: 1000 }; assert.equal(checkPhotoFiles([file], 1).length, 1); assert.throws(() => checkPhotoFiles([file, file], 1)); assert.throws(() => checkPhotoFiles([{ type: 'image/svg+xml', size: 100 }], 0)); assert.throws(() => checkPhotoFiles([{ type: 'image/png', size: 21 * 1024 * 1024 }], 0)); });
test('衝突需明確重載，不將草稿自動合併', () => { assert.equal(revisionConflict(409, {}), true); assert.equal(revisionConflict(400, { code: 'revision_conflict' }), true); assert.equal(revisionConflict(422, {}), false); });
test('單次API資格使用後不重試', () => assert.equal(canAnalyze({ prepared: { id: 'p1', sha256: 'abc', attempted: true } }, { configured: true }, true), false));
test('新尺寸明確交由後端配ID', () => assert.equal(parseDimensions([{ label: '厚度', value: '4', unit: 'mm', source: 'reference' }])[0].id, null));
test('照片路徑正規化後仍須留在照片API', () => assert.equal(safePhotoUrl('/api/tasks/../../outside', 'http://127.0.0.1:8171'), null));

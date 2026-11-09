import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { claimKey, createViewModel, filterRows, getInputSources } from '../src/analysis.js';

const result = JSON.parse(fs.readFileSync(new URL('../验收导出/核验完整结果.json', import.meta.url), 'utf8').replace(/^\ufeff/, ''));
const input = JSON.parse(fs.readFileSync(new URL('../离线原型/样例/综合模拟样例.json', import.meta.url), 'utf8').replace(/^\ufeff/, ''));

test('展示模型按真实运行结果构造，并保留无输出主张', () => {
  const model = createViewModel(result);
  assert.equal(model.rows.length, 16);
  assert.equal(model.claims.length, 15);
  assert.equal(model.claims.filter(c => c.rows.length === 0)[0].id, 'benchmark-claim-160');
  const row = model.rows.find(r => r.event === 'evt-A05-deposit');
  assert.equal(row.type, 'CONFLICT');
  assert.equal(row.original, result.event_claim_results.find(r => r.explanation.event_business_key === row.event));
  assert.equal(row.conflict.length, 3);
});

test('同一主张的两个编号匹配候选均可展示', () => {
  const model = createViewModel(result);
  const c1 = model.claims.find(c => c.id === 'c1');
  assert.equal(c1.rows.length, 2);
  assert.deepEqual(c1.rows.map(r => r.type), ['EXACT', 'EXACT']);
});

test('案件与主张的组合键不会因分隔符混淆', () => {
  assert.notEqual(claimKey('a|b', 'c'), claimKey('a', 'b|c'));
  const clone = structuredClone(result.event_claim_results[0]);
  const other = structuredClone(clone);
  other.case_id = 'other-case';
  const model = createViewModel({ event_claim_results: [clone, other], claims_without_output: [] });
  assert.equal(model.claims.length, 2);
});

test('类别筛选使用结果中的真实类型', () => {
  const { rows } = createViewModel(result);
  assert.equal(filterRows(rows, 'CONFLICT').length, 3);
  assert.equal(filterRows(rows, 'INSUFFICIENT').length, 1);
  assert.equal(filterRows(rows, 'ALL').length, 16);
});

test('输入对象与来源只取同案件同标识的真实记录', () => {
  const row = createViewModel(result).rows.find(r => r.event === 'evt-A05-deposit');
  const sources = getInputSources(input, row);
  assert.equal(sources.event.id, row.event);
  assert.equal(sources.claim.id, row.claim);
  assert.ok(sources.records.length > 0);
  assert.ok(sources.records.every(r => r.case_id === row.case));
  assert.deepEqual(sources.event, input.events.find(e => e.id === row.event && e.case_id === row.case));
});

import test from 'node:test';
import assert from 'node:assert/strict';
import {
  decisionStatusOf,
  effectiveTierOf,
  groupTiedCandidates,
  identifiabilityLabel,
  identifiabilityNote,
  isConditional,
  isWithheld,
} from '../src/analysis.js';

/* v0.2.1 整改 F-01：界面必须读后端判定，不得只看 tier。
 * 这些用例直接锁住「展示层不得把需并列的候选显示成可定论」。 */

const link = (over = {}) => ({
  link_id: 'LK-001',
  deposit_edge: 'D1',
  withdrawal_edge: 'W1',
  score: 82.5,
  tier: 'HIGH',
  rank: 1,
  ...over,
});

test('生效档位优先取后端判定，缺失时退回 tier（兼容旧结果）', () => {
  assert.equal(effectiveTierOf(link({ effective_tier: 'WITHHELD' })), 'WITHHELD');
  assert.equal(effectiveTierOf(link({ effective_tier: 'MEDIUM' })), 'MEDIUM');
  assert.equal(effectiveTierOf(link()), 'HIGH');
  assert.equal(effectiveTierOf(null), 'LOW');
});

test('判定为需并列时，标识与档位不得显示成可定论', () => {
  const l = link({
    tier: 'HIGH',
    effective_tier: 'HIGH',
    identifiability: { decision_status: 'CONDITIONAL', top_class_size: 3 },
  });
  assert.equal(decisionStatusOf(l), 'CONDITIONAL');
  assert.equal(isConditional(l), true);
  assert.equal(identifiabilityLabel(l), '需并列比对');
  assert.match(identifiabilityNote(l), /另外 2 个候选/);
});

test('可唯一定位的说明必须点明「不等于已确认」', () => {
  const l = link({ identifiability: { decision_status: 'ALLOWED', top_class_size: 1 } });
  assert.equal(identifiabilityLabel(l), '可唯一定位');
  assert.match(identifiabilityNote(l), /不等于资金关系已确认/);
});

test('不予判定时档位本身变成 WITHHELD', () => {
  const l = link({
    effective_tier: 'WITHHELD',
    identifiability: { decision_status: 'WITHHELD', reason: '未通过质量门槛' },
  });
  assert.equal(isWithheld(l), true);
  assert.equal(identifiabilityLabel(l), '不予判定');
  assert.match(identifiabilityNote(l), /未通过质量门槛/);
});

test('旧结果（无 identifiability 字段）保持原样，不误报', () => {
  const l = link();
  assert.equal(decisionStatusOf(l), 'UNKNOWN');
  assert.equal(isConditional(l), false);
  assert.equal(identifiabilityLabel(l), '未标注');
  assert.equal(identifiabilityNote(l), '');
});

test('同一入金下多条需并列的候选被聚成一组', () => {
  const links = [
    link({ link_id: 'LK-1', withdrawal_edge: 'W1', identifiability: { decision_status: 'CONDITIONAL', top_class_size: 3 } }),
    link({ link_id: 'LK-2', withdrawal_edge: 'W2', identifiability: { decision_status: 'CONDITIONAL', top_class_size: 3 } }),
    link({ link_id: 'LK-3', withdrawal_edge: 'W3', identifiability: { decision_status: 'CONDITIONAL', top_class_size: 3 } }),
    link({ link_id: 'LK-4', deposit_edge: 'D2', withdrawal_edge: 'W9', identifiability: { decision_status: 'ALLOWED', top_class_size: 1 } }),
  ];
  const { groups, singles } = groupTiedCandidates(links);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].deposit, 'D1');
  assert.equal(groups[0].links.length, 3);
  assert.ok(singles.some(l => l.link_id === 'LK-4'));
});

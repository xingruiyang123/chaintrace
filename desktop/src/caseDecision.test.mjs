// V0.2.2 前端数据契约自测（node 直接运行，不依赖构建）
import assert from 'node:assert';
import {
  identifiabilityStateOf, identifiabilityStateLabel,
  caseDecisionOf, caseDecisionLabel, decisionLayersOf,
  withheldDepositsOf, resolvedByEvidenceOf, ignoredEvidenceOf,
} from './caseDecision.js';

// 1. 六态直读
assert.equal(identifiabilityStateOf({ identifiability: { identifiability_status: 'UNIDENTIFIABLE' } }).status,
  'UNIDENTIFIABLE');
// 2. 旧结果映射，并标记 mapped
const legacy = identifiabilityStateOf({ identifiability: { decision_status: 'ALLOWED' } });
assert.equal(legacy.status, 'SUPPORTED');
assert.equal(legacy.mapped, true);
// 3. 什么都没有 → UNKNOWN，不猜
assert.equal(identifiabilityStateOf({}).status, 'UNKNOWN');

// 4. 措辞不得把「并列」说成「已确认」
assert.ok(identifiabilityStateLabel({ identifiability: { identifiability_status: 'CONDITIONAL' } })
  .includes('并列'));
// 5. 冲突与缺失分开表述
assert.ok(identifiabilityStateLabel({ identifiability: { identifiability_status: 'CONFLICTED' } })
  .includes('冲突'));
assert.ok(!identifiabilityStateLabel({ identifiability: { identifiability_status: 'CONFLICTED' } })
  .includes('缺失'));

// 6. 案件级四态
const yes = caseDecisionLabel({ veto: { case_decision: { decision_status: 'YES' } } });
assert.equal(yes.label, '满足执行条件');
assert.ok(yes.note.includes('不代表'));
// 7. CONDITIONAL 不能说成「可以办」
const cond = caseDecisionLabel({
  veto: { case_decision: { decision_status: 'CONDITIONAL', unresolved_conditions: ['逐笔未定'] } },
});
assert.equal(cond.label, '有条件可评估');
assert.ok(cond.note.includes('逐笔未定'));
// 8. UNDETERMINED 不能说成「不能办」
const und = caseDecisionLabel({
  veto: { case_decision: { decision_status: 'UNDETERMINED', unresolved_conditions: ['资产未知'] } },
});
assert.equal(und.label, '尚不能判断');

// 9. 旧结果没有 case_decision → null，不假装有
assert.equal(caseDecisionOf({ veto: { case_level_executable: 'YES' } }), null);
assert.ok(caseDecisionLabel({ veto: { case_level_executable: 'YES' } }).label.includes('旧结果'));

// 10. 三层
const layers = decisionLayersOf({
  veto: { case_decision: { layers: { link: { status: 'SUPPORTED' }, account: { status: 'UNKNOWN' } } } },
});
assert.equal(layers.link.status, 'SUPPORTED');
assert.equal(layers.account.status, 'UNKNOWN');

// 11. withheld_deposits 汇总跨案件
const held = withheldDepositsOf({
  cases: [
    { case_id: 'A', withheld_deposits: [{ identifiability_status: 'WITHHELD' }] },
    { case_id: 'B', withheld_deposits: [{ identifiability_status: 'WITHHELD' }] },
    { case_id: 'C' },
  ],
});
assert.equal(held.length, 2);
assert.equal(held[0].caseId, 'A');

// 12. 证据溯源
assert.deepEqual(resolvedByEvidenceOf({ identifiability: { resolved_by_evidence: ['EV-1'] } }), ['EV-1']);
assert.equal(ignoredEvidenceOf({}).length, 0);

console.log('caseDecision.js 12 项断言全部通过');

// ————————————————————————————————————————————————
// V0.2.2-F1：六态展示层
// ————————————————————————————————————————————————
import {
  IDENTIFIABILITY_CN, STATE_TONE, STATE_ORDER,
  stateInfoOf, identifiabilityCounts, REASON_CODE_CN, missingFieldsOf,
} from './caseDecision.js';

// 13. 六个状态都有中文标签，且不能出现"已确认"这类会被读成结论的词
for (const s of ['SUPPORTED', 'CONDITIONAL', 'WITHHELD', 'CONFLICTED', 'INSUFFICIENT', 'UNIDENTIFIABLE']) {
  assert.ok(IDENTIFIABILITY_CN[s], `${s} 缺中文标签`);
}
assert.ok(!IDENTIFIABILITY_CN.UNIDENTIFIABLE.includes('已确认'));
assert.ok(!IDENTIFIABILITY_CN.WITHHELD.includes('已确认'));

// 14. 视觉分组：不可判定的三态必须压成同一组（中性），不能与 SUPPORTED 同色
assert.equal(STATE_TONE.SUPPORTED, 'supported');
assert.equal(STATE_TONE.CONDITIONAL, 'conditional');
assert.equal(STATE_TONE.CONFLICTED, 'conflict');
for (const s of ['WITHHELD', 'INSUFFICIENT', 'UNIDENTIFIABLE']) {
  assert.equal(STATE_TONE[s], 'blocked', `${s} 应与中性态同组`);
}

// 15. 90 分的 UNIDENTIFIABLE 仍是 blocked —— 分数高不能改变视觉分组
const highButUnusable = { score: 90.9, identifiability: { identifiability_status: 'UNIDENTIFIABLE' } };
assert.equal(stateInfoOf(highButUnusable).tone, 'blocked');
assert.equal(stateInfoOf(highButUnusable).keepScoreTone, false);
const highUsable = { score: 90.9, identifiability: { identifiability_status: 'SUPPORTED' } };
assert.equal(stateInfoOf(highUsable).keepScoreTone, true);

// 16. 汇总计数与逐条明细一致（这是页面上"表头 vs 矩阵"的同一算法）
const sample = [
  { identifiability: { identifiability_status: 'SUPPORTED' } },
  { identifiability: { identifiability_status: 'CONDITIONAL' } },
  { identifiability: { identifiability_status: 'CONDITIONAL' } },
  { identifiability: { identifiability_status: 'UNIDENTIFIABLE' } },
  { identifiability: { decision_status: 'ALLOWED' } },          // 旧结果 → SUPPORTED
];
const counts = identifiabilityCounts(sample);
assert.equal(counts.SUPPORTED, 2);
assert.equal(counts.CONDITIONAL, 2);
assert.equal(counts.UNIDENTIFIABLE, 1);
assert.equal(identifiabilityCounts([]).SUPPORTED, 0);
const total = STATE_ORDER.reduce((n, k) => n + counts[k], 0);
assert.equal(total, sample.length, '汇总合计必须等于明细条数');

// 17. 原因码有中文解释，未知码不崩
assert.ok(REASON_CODE_CN.AMOUNT_TIE.includes('并列'));
assert.ok(REASON_CODE_CN.CRITICAL_FIELD_MISSING.includes('缺失'));
assert.equal(REASON_CODE_CN.NOT_A_REAL_CODE, undefined);

// 18. 缺失字段可读（两处来源都认）
assert.deepEqual(missingFieldsOf({ identifiability: { missing_critical_fields: ['amount'] } }), ['amount']);
assert.deepEqual(missingFieldsOf({ missing_critical_fields: ['time'] }), ['time']);
assert.deepEqual(missingFieldsOf({}), []);

console.log('六态展示层 6 组断言全部通过');

// V0.2.2 前端数据契约（只做读取与措辞，不改任何判定）
//
// 为什么单开一个模块：V0.2.2 后端新增了六态可识别性与案件级四态，
// 但现有页面只认 effective_tier / decision_status。
// 把新字段的读取集中在这里，页面可以逐步接入，不会各写一套解释。

/** 六态可识别性（V0.2.2）。旧结果没有该字段时由 decision_status 映射，不臆造。 */
export const IDENTIFIABILITY_STATES = [
  'SUPPORTED', 'CONDITIONAL', 'WITHHELD', 'CONFLICTED', 'INSUFFICIENT', 'UNIDENTIFIABLE',
];

const LEGACY_TO_STATE = {
  ALLOWED: 'SUPPORTED',
  CONDITIONAL: 'CONDITIONAL',
  WITHHELD: 'WITHHELD',
};

/**
 * 取可识别性六态。
 * 返回 {status, mapped}：mapped=true 表示来自旧字段映射，界面应标注「旧结果」。
 */
export function identifiabilityStateOf(link) {
  const id = link?.identifiability || {};
  if (id.identifiability_status) {
    return { status: id.identifiability_status, mapped: false };
  }
  const legacy = id.decision_status;
  if (legacy && LEGACY_TO_STATE[legacy]) {
    return { status: LEGACY_TO_STATE[legacy], mapped: true };
  }
  return { status: 'UNKNOWN', mapped: true };
}

/** 六态的中文短标签。刻意让「并列」与「不可识别」分开，两者处置不同。 */
export function identifiabilityStateLabel(link) {
  const { status, mapped } = identifiabilityStateOf(link);
  const suffix = mapped ? '（旧结果）' : '';
  switch (status) {
    case 'SUPPORTED': return `可唯一定位${suffix}`;
    case 'CONDITIONAL': return `需并列比对${suffix}`;
    case 'UNIDENTIFIABLE': return `候选结构等价，无法区分${suffix}`;
    case 'WITHHELD': return `不予判定${suffix}`;
    case 'CONFLICTED': return `证据冲突，待解决${suffix}`;
    case 'INSUFFICIENT': return `无可用证据${suffix}`;
    default: return `未标注${suffix}`;
  }
}

/** 为什么不给结论。直接取自后端 reason_code，不在前端重算。 */
export function identifiabilityReasonOf(link) {
  const id = link?.identifiability || {};
  return id.reason || '';
}

/** 这条候选是被哪条外部证据定下来的（空数组表示纯链上结论）。 */
export function resolvedByEvidenceOf(link) {
  return link?.identifiability?.resolved_by_evidence || [];
}

/** 被挡下的证据及原因——让用户看到「为什么这条证据没起作用」。 */
export function ignoredEvidenceOf(link) {
  return link?.identifiability?.evidence_ignored || [];
}

/** 案件级四态（V0.2.2）。旧结果没有该字段时返回 null，不假装有。 */
export function caseDecisionOf(result) {
  const veto = result?.veto;
  if (!veto || !veto.case_decision) return null;
  return veto.case_decision;
}

/**
 * 案件级状态的中文说明。
 * 关键：CONDITIONAL 不能说成「可以办」，UNDETERMINED 不能说成「不能办」。
 */
export function caseDecisionLabel(result) {
  const decision = caseDecisionOf(result);
  if (!decision) {
    const legacy = result?.veto?.case_level_executable;
    if (!legacy) return { label: '未评估', note: '' };
    return { label: `未评估（旧结果：${legacy}）`, note: '旧结果没有案件级裁决，无法判断其条件' };
  }
  switch (decision.decision_status) {
    case 'YES':
      return { label: '满足执行条件', note: '不代表现实中必然追回' };
    case 'NO':
      return { label: '存在明确阻断', note: (decision.blocking_conditions || []).join('；') };
    case 'CONDITIONAL':
      return { label: '有条件可评估', note: (decision.unresolved_conditions || []).join('；') };
    case 'UNDETERMINED':
      return { label: '尚不能判断', note: (decision.unresolved_conditions || []).join('；') };
    default:
      return { label: decision.decision_status, note: '' };
  }
}

/** 三层状态一次性取出，供分层展示。 */
export function decisionLayersOf(result) {
  const decision = caseDecisionOf(result);
  if (!decision) return null;
  const layers = decision.layers || {};
  return {
    link: layers.link || null,
    account: layers.account || null,
    asset: layers.asset || null,
    evidenceBlock: layers.evidence_block || null,
  };
}

/** 缺关键字段、因而没有任何归因分的入金。不给分数，只说明缺了什么。 */
export function withheldDepositsOf(result) {
  const cases = result?.cases || [];
  const out = [];
  for (const item of cases) {
    for (const w of item.withheld_deposits || []) {
      out.push({ caseId: item.case_id, ...w });
    }
  }
  return out;
}

// —————————————————————————————————————————————————————————
// 六态的展示层（V0.2.2-F1）
//
// 与「分数档位 HIGH/MEDIUM/LOW」是两套概念，不得合并：
//   档位 = 这条候选跟自己候选集比，分高不高；
//   六态 = 这条候选能不能当作唯一结论用。
// 一个 91 分的候选完全可能是 UNIDENTIFIABLE —— 分数高，但并列一堆，不能用。
// —————————————————————————————————————————————————————————

/** 六个状态的短标签，用在格子和列表里。措辞刻意区分"能不能用"，而不是"多可信"。 */
export const IDENTIFIABILITY_CN = {
  SUPPORTED: '已有证据支持',
  CONDITIONAL: '有条件支持',
  WITHHELD: '暂不作唯一判断',
  CONFLICTED: '存在证据冲突',
  INSUFFICIENT: '证据不足',
  UNIDENTIFIABLE: '当前信息下无法区分',
  UNKNOWN: '未标注',
};

/** 视觉分组：决定格子底色与标签颜色。 */
export const STATE_TONE = {
  SUPPORTED: 'supported',
  CONDITIONAL: 'conditional',
  WITHHELD: 'blocked',
  INSUFFICIENT: 'blocked',
  UNIDENTIFIABLE: 'blocked',
  CONFLICTED: 'conflict',
  UNKNOWN: 'blocked',
};

export const STATE_ORDER = [
  'SUPPORTED', 'CONDITIONAL', 'UNIDENTIFIABLE', 'WITHHELD', 'CONFLICTED', 'INSUFFICIENT',
];

/** 一条候选的六态 + 视觉分组。 */
export function stateInfoOf(link) {
  const { status, mapped } = identifiabilityStateOf(link);
  return {
    status,
    mapped,
    label: IDENTIFIABILITY_CN[status] || status,
    tone: STATE_TONE[status] || 'blocked',
    // 只有这两种状态允许保留"分数配色"，其余一律压成中性色
    keepScoreTone: status === 'SUPPORTED' || status === 'CONDITIONAL',
  };
}

/** 汇总一批候选的六态计数（键序固定，便于界面稳定）。 */
export function identifiabilityCounts(links) {
  const counts = {};
  for (const key of STATE_ORDER) counts[key] = 0;
  for (const link of links || []) {
    const { status } = stateInfoOf(link);
    counts[status] = (counts[status] || 0) + 1;
  }
  return counts;
}

/** 原因码的中文解释，用于展开详情。 */
export const REASON_CODE_CN = {
  UNIQUE_TOP: '首位候选自成一类，可唯一定位',
  AMOUNT_TIE: '首位与其他候选金额等价，必须并列查看',
  ALL_EQUIVALENT: '全部候选在同一等价类内，结构上无法区分',
  GATE_FAILED: '未通过数据充分性/证据独立性门槛',
  CRITICAL_FIELD_MISSING: '关键字段缺失，无法给出归因分与名次',
  EVIDENCE_CONFLICT: '存在相互矛盾的证据',
  NO_USABLE_EVIDENCE: '没有任何可用证据维度',
  EXTERNAL_EVIDENCE_CONFIRMED: '已有经过核验的逐笔证据直接指向该关联',
};

/** 这条候选缺了哪些关键字段（空数组表示不缺）。 */
export function missingFieldsOf(link) {
  return link?.identifiability?.missing_critical_fields
    || link?.missing_critical_fields
    || [];
}

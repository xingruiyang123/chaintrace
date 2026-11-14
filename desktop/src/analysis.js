export const claimKey = (caseId, claimId) => JSON.stringify([caseId, claimId]);

// The view projects the output returned by the existing engine; it never infers a transfer.
export function createViewModel(result) {
  if (!result) return { rows: [], claims: [] };
  const rows = (result.event_claim_results || []).map(original => {
    const e = original.explanation || {};
    return {
      case: original.case_id, claim: e.claim_business_key, event: e.event_business_key,
      type: e.match_class, asset: e.asset_id || '—', kind: e.event_type || '',
      scores: e.feature_scores || {}, missing: original.missing_items || [],
      conflict: original.conflict_items || [], status: original.review_status,
      score: original.score, original,
    };
  });
  const groups = new Map();
  for (const row of rows) {
    const key = claimKey(row.case, row.claim);
    if (!groups.has(key)) groups.set(key, { key, id: row.claim, caseId: row.case, rows: [] });
    groups.get(key).rows.push(row);
  }
  for (const claim of result.claims_without_output || []) {
    const key = claimKey(claim.case_id, claim.claim_id);
    if (!groups.has(key)) groups.set(key, { key, id: claim.claim_id, caseId: claim.case_id, rows: [] });
  }
  return { rows, claims: [...groups.values()] };
}

export function filterRows(rows, type) {
  if (type === 'ALL') return rows;
  if (type === 'CANDIDATES') return rows.filter(r => ['EXACT', 'CANDIDATE_STRONG', 'CANDIDATE_REVIEW'].includes(r.type));
  return rows.filter(r => r.type === type);
}

export function getInputSources(input, row) {
  if (!input || !row) return { claim: null, event: null, records: [] };
  const claim = input.claims.find(c => c.case_id === row.case && c.id === row.claim) || null;
  const event = input.events.find(e => e.case_id === row.case && e.id === row.event) || null;
  const ids = new Set([...(claim?.source_ids || []), ...(event?.source_ids || [])]);
  const records = input.records.filter(r => r.case_id === row.case && ids.has(r.id));
  return { claim, event, records };
}

/* ------------------------------------------------------------------
 * v0.2.1：可识别性判定接入展示层（整改审查发现 F-01）
 *
 * 背景：后端已给出 identifiability.decision_status 与 effective_tier，
 * 但界面此前只读 link.tier，导致「判定为需并列」的候选仍显示 HIGH。
 * 这里统一收口，任何组件都不得直接读 link.tier 做展示。
 * ------------------------------------------------------------------ */

export const WITHHELD_TIER = 'WITHHELD';

/** 展示用档位：优先取后端判定结果；v0.2.1 之前的结果没有该字段，退回 tier。 */
export function effectiveTierOf(link) {
  if (!link) return 'LOW';
  return link.effective_tier || link.tier || 'LOW';
}

export function decisionStatusOf(link) {
  return link?.identifiability?.decision_status || 'UNKNOWN';
}

export function isWithheld(link) {
  return effectiveTierOf(link) === WITHHELD_TIER;
}

export function isConditional(link) {
  return decisionStatusOf(link) === 'CONDITIONAL';
}

/** 短标签，用在列表与格子上的角标。 */
export function identifiabilityLabel(link) {
  switch (decisionStatusOf(link)) {
    case 'ALLOWED': return '可唯一定位';
    case 'CONDITIONAL': return '需并列比对';
    case 'WITHHELD': return '不予判定';
    default: return '未标注';
  }
}

/** 长说明：解释「为什么」，避免把可定位误读成已确认。 */
export function identifiabilityNote(link) {
  const id = link?.identifiability;
  if (!id) return '';
  if (id.decision_status === 'CONDITIONAL') {
    const others = Math.max(1, Number(id.top_class_size || 1) - 1);
    return `首位与另外 ${others} 个候选在金额上不可区分，必须并列查看，名次先后没有意义`;
  }
  if (id.decision_status === 'WITHHELD') {
    return id.reason || '信息不足，系统不给出强度结论';
  }
  if (id.decision_status === 'ALLOWED') {
    return '首位候选自成一类，可在候选集内唯一定位；这不等于资金关系已确认';
  }
  return '';
}

/**
 * 把同一笔入金下「需并列」的候选聚成组，供界面成组展示。
 * 返回 { groups: [{deposit, links, tied}], singles: [...] }
 */
export function groupTiedCandidates(links) {
  const byDeposit = new Map();
  (links || []).forEach((link) => {
    const key = link.deposit_edge;
    if (!byDeposit.has(key)) byDeposit.set(key, []);
    byDeposit.get(key).push(link);
  });
  const groups = [];
  const singles = [];
  byDeposit.forEach((rows, deposit) => {
    const tied = rows.filter(isConditional);
    const rest = rows.filter(l => !isConditional(l));
    if (tied.length > 1) groups.push({ deposit, links: tied, tied: true });
    else singles.push(...rows);
    singles.push(...rest);
  });
  return { groups, singles };
}


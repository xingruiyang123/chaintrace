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

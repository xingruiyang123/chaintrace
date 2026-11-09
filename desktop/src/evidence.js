export function projectEvidence(input,row){
 const caseId=row.case_id||row.case;
 const claim=(input?.claims||[]).find(x=>x.case_id===caseId&&x.id===(row.claim_id||row.claim));
 const event=(input?.events||[]).find(x=>x.case_id===caseId&&x.id===(row.event_id||row.event));
 const records=input?.records||[];
 const side=(object)=>({object:object||null,sources:(object?.source_ids||[]).map(id=>({id,record:records.find(r=>r.case_id===caseId&&r.id===id)||null}))});
 return {case_id:caseId,claim:side(claim),event:side(event)};
}
const value = v => v === null || v === undefined || v === '' ? '未提供' : String(v);
export function inputFields(claim,event) {
 const c=claim||{}, e=event||{};
 const row=(label,left,right)=>({label,claim:value(left),event:value(right)});
 const reverse=c.predicate==='RECEIVED_FROM', forward=['PAID_TO','TRANSFERRED_TO'].includes(c.predicate);
 const subjectLabel=reverse?'主体 / 转入端':forward?'主体 / 转出端':'主体 / 事件账户';
 const objectLabel=reverse?'对手方 / 转出端':forward?'对手方 / 转入端':'对手方 / 事件账户';
 const subjectAccount=reverse?e.to_account:forward?e.from_account:'方向未确定';
 const objectAccount=reverse?e.from_account:forward?e.to_account:'方向未确定';
 return [row('资产',c.asset_id,e.asset_id),row('网络',c.network_id,e.network_id),row('金额',c.amount,e.amount),row('事件类型',c.expected_event_type,e.event_type),row(subjectLabel,c.subject_id,subjectAccount),row(objectLabel,c.object_id,objectAccount),row('外部编号类型',c.external_id_type,e.external_id_type),row('外部编号',c.external_id,e.external_id),row('时间',`${value(c.time_from)} 至 ${value(c.time_to)}`,e.event_time)];
}

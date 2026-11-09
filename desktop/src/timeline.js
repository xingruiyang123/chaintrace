const canonical = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$/.test(value);
export function hasSortableTime(times, index) { return Array.isArray(times) && canonical(times[index]); }
export function caseEvents(input, caseId, times) {
  return (input?.events || []).map((event,index)=>({event,index,time:hasSortableTime(times,index)?times[index]:null})).filter(x=>x.event.case_id===caseId).sort((a,b)=>{
    if(Boolean(a.time)!==Boolean(b.time)) return a.time?-1:1;
    return (a.time && b.time ? (a.time < b.time ? -1 : a.time > b.time ? 1 : 0) : 0) || a.index-b.index;
  }).map(x=>x.event);
}
export function eventRows(rows,event) { return rows.filter(r=>r.case===event.case_id && r.event===event.id); }

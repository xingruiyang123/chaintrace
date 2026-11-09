import React from 'react';
import { Clock3, ArrowRight, FileText } from 'lucide-react';
import { caseEvents, eventRows, hasSortableTime } from './timeline';
import './timeline.css';
const kinds={ONCHAIN_TRANSFER:'链上转账',EXCHANGE_DEPOSIT:'平台充值',EXCHANGE_WITHDRAWAL:'平台提现',INTERNAL_TRANSFER:'平台内部记账'};
export default function Timeline({input,timelineTimes,current,rows,onChoose}) {
  const events=caseEvents(input,current?.caseId,timelineTimes);
  if(!current)return <div className="timeline-empty">请先选择左侧资金主张。</div>;
  if(!events.length)return <div className="timeline-empty">当前案件没有输入事件。</div>;
  return <div className="timeline-scroll"><div className="timeline-intro"><div><span className="timeline-eyebrow">案件事件记录</span><h2>沿时间查看资金事件</h2><p title={current.caseId}>案件 {current.caseId}</p></div><span className="timeline-count">{events.length} 条事件</span></div><div className="timeline-list">{events.map((event,i)=>{
    const matches=eventRows(rows,event);
    const timeKnown=hasSortableTime(timelineTimes,input.events.indexOf(event));
    return <article className="timeline-item" key={event.id+'-'+i}><div className="timeline-rail"><span><Clock3 size={14}/></span></div><div className="timeline-content"><div className="timeline-time">{event.event_time||'时间缺失'} {!timeKnown&&<strong>（时间无法排序）</strong>} <span>{kinds[event.event_type]||event.event_type||'资金事件'}</span></div><div className="timeline-card"><div className="timeline-card-top"><strong>{event.id}</strong><b>{event.amount??'—'} <small>{event.asset_id||''}</small></b></div><div className="timeline-accounts"><span>{event.from_account||'来源账户缺失'}</span><ArrowRight size={16}/><span>{event.to_account||'目标账户缺失'}</span></div><div className="timeline-links">{matches.length?matches.map((row,n)=><button key={row.claim+'-'+n} onClick={()=>onChoose(row)}><FileText size={14}/>{row.claim}<ArrowRight size={14}/></button>):<span>暂无主张关联结果</span>}</div></div></div></article>})}</div><p className="timeline-note">时间和账户方向来自输入记录；无法排序的事件列在末尾，事件先后不代表资金路径已确认。</p></div>
}

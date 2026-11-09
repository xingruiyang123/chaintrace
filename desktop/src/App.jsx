import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Network, Waypoints, ChartNoAxesCombined, Clock3, FolderOpen, FileText, ClipboardCheck, Database, ChevronDown, ChevronRight, PanelLeftClose, PanelLeftOpen, Search, Play, Download, SlidersHorizontal, List, ScanLine, ShieldCheck, X, Check, TriangleAlert, Minus, Layers, FileJson } from 'lucide-react';
import { Button } from './components/ui/button';
import GraphCanvas from './GraphCanvas';
import Timeline from './Timeline.jsx';
import { projectEvidence, inputFields } from './evidence';
import { claimKey, createViewModel, filterRows } from './analysis';
import logoMark from './assets/logo-mark.png';
import './style.css';

const names={EXACT:'编号规则匹配',CANDIDATE_STRONG:'较强候选',CANDIDATE_REVIEW:'待复核候选',INSUFFICIENT:'信息不足',CONFLICT:'明确冲突'};
const kinds={ONCHAIN_TRANSFER:'链上转账',EXCHANGE_DEPOSIT:'平台充值',EXCHANGE_WITHDRAWAL:'平台提现',INTERNAL_TRANSFER:'平台内部记账'};
const features={amount:'金额',asset_network:'资产与网络',direction_accounts:'方向与账户',external_id:'外部编号',time:'时间'};
const issues={claim_time_unknown:'主张时间缺失',external_identifier_unknown:'外部编号缺失',external_identifier_not_comparable:'编号类型不可直接比较',object_account_unknown:'对手方账户缺失',subject_account_unknown:'主体账户缺失',subject_person_account_not_verified:'主体人员与账户关系未核实',object_person_account_not_verified:'对手方人员与账户关系未核实',ASSET_MISMATCH:'资产不一致',NETWORK_MISMATCH:'网络不一致',EVENT_TYPE_MISMATCH:'事件类型不一致',AMOUNT_OUT_OF_TOLERANCE:'金额超出容差',TIME_OUT_OF_WINDOW:'时间超出窗口',EXTERNAL_ID_MISMATCH:'外部编号不一致',OBJECT_ACCOUNT_CONFLICT:'对手方账户冲突',SUBJECT_ACCOUNT_CONFLICT:'主体账户冲突'};
const tones={EXACT:'exact',CANDIDATE_STRONG:'strong',CANDIDATE_REVIEW:'review',INSUFFICIENT:'missing',CONFLICT:'conflict'};
const label = (value, dictionary) => dictionary[value] || value;
const resultKey = row => JSON.stringify([row.case, row.claim, row.event]);
function Status({type}) { return <span className={'badge '+(tones[type]||'missing')}><i/>{names[type]||type}</span>; }

function ResultTable({rows,selected,onChoose,showClaim=false}) {
  const tableRef=useRef(null);
  useEffect(()=>{
    const list=tableRef.current, item=list?.querySelector('tr[aria-selected="true"]');
    if(!item)return;
    const a=item.getBoundingClientRect(),b=list.getBoundingClientRect();
    if(a.bottom>b.bottom-12)list.scrollTop+=a.bottom-b.bottom+12;
    else if(a.top<b.top+42)list.scrollTop-=b.top-a.top+42;
  },[selected,showClaim]);
  return <div className="result-table" ref={tableRef}><table><thead><tr>{showClaim&&<th>资金主张</th>}<th>资金事件</th><th>核验结果</th><th>规则分</th></tr></thead><tbody>
    {rows.map((r,index)=><tr key={resultKey(r)+'|'+index} onClick={()=>onChoose(r)} tabIndex={0} aria-selected={selected&&resultKey(selected)===resultKey(r)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onChoose(r)}}} className={selected&&resultKey(selected)===resultKey(r)?'selected':''}>
      {showClaim&&<td className="mono">{r.claim}<small title={r.case}>案件 {r.case}</small></td>}<td><span className="mono">{r.event}</span><small>{kinds[r.kind]||r.kind||'资金事件'}</small></td><td><Status type={r.type}/></td><td>{r.score??'—'}</td>
    </tr>)}
  </tbody></table>{!rows.length&&<div className="empty-list">当前筛选没有结果</div>}</div>;
}

function Inspector({row,input,tab,setTab}) {
  const evidence=useMemo(()=>row?projectEvidence(input,row):null,[input,row]);
  const supports=row?Object.entries(row.scores).filter(([,value])=>Number(value)>0):[];
  return <aside className="inspector"><div className="pane-heading"><div className="inspector-title"><ScanLine size={17}/><h2>核验检查器</h2></div><span className="live-dot" title="与当前选择联动"/></div>
    <div className="inspector-tabs"><button className={tab==='explain'?'active':''} onClick={()=>setTab('explain')}>核验依据</button><button className={tab==='record'?'active':''} onClick={()=>setTab('record')}>结构化记录</button></div>
    <div className="inspector-body" key={(row?resultKey(row):'none')+tab}>
      {!row?<div className="inspector-empty"><Network size={30}/><h3>选择一条候选关系</h3><p>点击事件、连线或结果行，查看核验依据。</p></div>:
      tab==='record'?<><h3 className="record-title">当前关系的核验输出</h3><pre>{JSON.stringify(row.original,null,2)}</pre></>:
      <><div className="relation-header"><Status type={row.type}/>{row.score!=null&&<span className="inline-score" title="规则分不代表正确概率">规则分 <b>{row.score}</b></span>}</div>
        <div className="relation-pair"><span className="pair-icon"><FileText size={16}/></span><div><small>资金主张</small><b title={row.claim}>{row.claim}</b></div><span className="pair-link"/><span className="pair-icon event"><Database size={16}/></span><div><small>{kinds[row.kind]||'资金事件'} · {row.asset}</small><b title={row.event}>{row.event}</b></div></div>
        {row.conflict.length>0&&<div className="evidence-section has-conflict priority-evidence"><h3><span><TriangleAlert size={17}/>冲突项</span><b>{row.conflict.length}</b></h3><ul>{row.conflict.map((x,i)=><li key={x+i}>{label(x,issues)}</li>)}</ul></div>}
        {row.missing.length>0&&<div className="evidence-section has-missing priority-evidence"><h3><span><Minus size={17}/>缺失项</span><b>{row.missing.length}</b></h3><ul>{row.missing.map((x,i)=><li key={x+i}>{label(x,issues)}</li>)}</ul></div>}
        <div className="evidence-section"><h3><span title="含部分支持，具体分值可在字段比较中展开"><Check size={17}/>支持依据</span><b>{supports.length}</b></h3><div className="evidence-chips">{supports.map(([k])=><span key={k}>{label(k,features)}</span>)}</div>{!supports.length&&<p className="none-found">未列出支持项</p>}</div>
        <div className="zero-evidence">{row.conflict.length===0&&<span><Check size={13}/>冲突 0</span>}{row.missing.length===0&&<span><Check size={13}/>缺失 0</span>}</div>
        <details className="field-scores input-comparison"><summary>双方输入字段<ChevronDown size={14}/></summary><p>展示原始输入值；账户列按主张方向排列；主体可能是人员或账户，具体对应仍须核验。</p><div className="input-comparison-grid"><div className="comparison-head"><span>字段</span><span>资金主张</span><span>资金事件</span></div>{inputFields(evidence.claim.object,evidence.event.object).map(item=><div className="comparison-row" key={item.label}><span>{item.label}</span><span>{item.claim}</span><span>{item.event}</span></div>)}</div></details>
        <details className="field-scores"><summary>字段比较<ChevronDown size={14}/></summary>{Object.entries(row.scores).map(([k,v])=><div key={k}><span>{label(k,features)}</span><code>{String(v)}</code></div>)}</details>
        <details className="field-scores source-details"><summary>输入对象与引用来源<ChevronDown size={14}/></summary><p>对照输入字段与双方各自引用的来源；具体判断以原始核验输出为准。</p>{['claim','event'].map(side=>{const item=evidence[side];return <section className="input-evidence" key={side}><h4>{side==='claim'?'资金主张':'资金事件'}</h4>{item.object?<pre>{JSON.stringify(item.object,null,2)}</pre>:<p>输入对象未找到</p>}<b>来源引用</b>{!item.sources.length?<p>未提供来源引用</p>:item.sources.map((source,i)=><details key={source.id+'-'+i}><summary>{source.id} · {source.record?'已找到':'未找到'}</summary>{source.record&&<pre>{JSON.stringify(source.record,null,2)}</pre>}</details>)}</section>})}</details>
      </>}
    </div><div className="inspector-foot"><ShieldCheck size={15}/><span>{row?.status==='DISPUTED'?'有争议 · 待人工复核':'待人工复核'}</span></div>
  </aside>;
}

function OtherResults({data,onShowClaim}) {
  return <section className="other-results"><h2>无输出主张 <span>{data.claims_without_output.length}</span></h2>
    {data.claims_without_output.length?<div className="other-claims">{data.claims_without_output.map((c,i)=><button key={claimKey(c.case_id,c.claim_id)+i} onClick={()=>onShowClaim(c)}><FileText size={16}/><b>{c.claim_id}</b><span title={c.case_id}>案件 {c.case_id}</span><ChevronRight size={16}/></button>)}</div>:<p>本次没有无输出主张。</p>}
    <h2>事件间核对 <span>{data.event_reconciliation.length}</span></h2><p>事件间核对结果用于辅助复核，不能直接视为已确认转移关系。</p>
    {data.event_reconciliation.length?data.event_reconciliation.map((row,i)=><details className="other-record" key={i}><summary>{row.explanation?.transfer_id||row.left_node_id} / {row.explanation?.deposit_id||row.right_node_id} <Status type={row.explanation?.match_class}/></summary><pre>{JSON.stringify(row,null,2)}</pre></details>):<p>本次没有事件间核对结果。</p>}
  </section>;
}

export default function App() {
  const [file,setFile]=useState(''),[data,setData]=useState(null),[input,setInput]=useState(null),[timelineTimes,setTimelineTimes]=useState(null),[active,setActive]=useState(null),[selected,setSelected]=useState(null),[filter,setFilter]=useState('ALL'),[mode,setMode]=useState('graph'),[page,setPage]=useState('graph'),[query,setQuery]=useState(''),[collapsed,setCollapsed]=useState(()=>window.innerWidth<=1300),[busy,setBusy]=useState(false),[tab,setTab]=useState('explain'),[status,setStatus]=useState('请选择模拟输入，或加载内置样例。'),[error,setError]=useState(false);
  const claimListRef=useRef(null);
  const {rows,claims}=useMemo(()=>createViewModel(data),[data]);
  const current=claims.find(c=>c.key===active)||null;
  const selectedRow=current?.rows.find(r=>r.event===selected)||null;
  const graphRows=current?filterRows(current.rows,filter):[];
  const tableRows=filterRows(page==='all'?rows:current?.rows||[],filter);
  const shownClaims=claims.filter(c=>c.id.toLowerCase().includes(query.toLowerCase())||c.caseId.toLowerCase().includes(query.toLowerCase()));
  const counts=Object.fromEntries(Object.keys(names).map(type=>[type,rows.filter(r=>r.type===type).length]));
  useEffect(()=>{const mq=window.matchMedia('(max-width:1300px)');const resize=e=>setCollapsed(e.matches);mq.addEventListener('change',resize);return()=>mq.removeEventListener('change',resize)},[]);
  useEffect(()=>{const list=claimListRef.current;const item=list?.querySelector('[aria-current="true"]');if(!item)return;const a=item.getBoundingClientRect(),b=list.getBoundingClientRect();if(a.bottom>b.bottom-8)list.scrollTop+=a.bottom-b.bottom+8;else if(a.top<b.top+8)list.scrollTop-=b.top-a.top+8},[active,query,page]);
  async function chooseInput(sample=false) {
    try {const path=await(sample?window.chaintrace.sample():window.chaintrace.chooseInput());if(!path)return;setFile(path);setData(null);setInput(null);setTimelineTimes(null);setActive(null);setSelected(null);setError(false);setStatus('已选择输入，执行核验时将进行格式检查。');}
    catch(e){setError(true);setStatus('选择输入失败：'+e.message)}
  }
  async function run() {
    setBusy(true);setError(false);setData(null);setInput(null);setTimelineTimes(null);setActive(null);setSelected(null);setStatus('正在执行核验…');
    try {const response=await window.chaintrace.run(file);const result=response.result;const model=createViewModel(result);const first=model.rows.find(r=>r.type==='CONFLICT')||model.rows[0];setData(result);setInput(response.input);setTimelineTimes(response.timelineTimes);setActive(first?claimKey(first.case,first.claim):model.claims[0]?.key||null);setSelected(first?.event||null);setFilter('ALL');setPage('graph');setMode('graph');setTab('explain');setStatus(`核验完成：事件—主张 ${result.event_claim_results.length} 条；事件—事件 ${result.event_reconciliation.length} 条；无输出主张 ${result.claims_without_output.length} 条。`);}
    catch(e){setError(true);setStatus('核验失败：'+e.message)}finally{setBusy(false)}
  }
  async function save() {try{const log=await window.chaintrace.export();if(log){setError(false);setStatus(`已导出 ${log.event_claim_rows} 条事件—主张结果及运行记录。`)}}catch(e){setError(true);setStatus('导出失败：'+e.message)}}
  function chooseClaim(c){setActive(c.key);setFilter('ALL');setSelected((c.rows.find(r=>r.type==='CONFLICT')||c.rows[0])?.event||null);setTab('explain')}
  function chooseRow(r){setActive(claimKey(r.case,r.claim));setSelected(r.event);setTab('explain')}
  function changeFilter(f){setFilter(f);const available=filterRows(page==='all'?rows:current?.rows||[],f);if(!available.some(r=>selectedRow&&resultKey(r)===resultKey(selectedRow))){const next=available[0];if(next)chooseRow(next);else setSelected(null)}}
  function switchPage(p){setPage(p);setFilter('ALL');setTab('explain')}
  const navs=[{label:'分析总览',icon:ChartNoAxesCombined},{label:'关联图谱',icon:Network,key:'graph'},{label:'资金流向',icon:Waypoints},{label:'时间轴',icon:Clock3,key:'timeline'},{label:'全部结果',icon:List,key:'all'},{label:'其他核验',icon:ClipboardCheck,key:'other'}];
  return <><div className="windowbar"><img src={logoMark} alt=""/><span>链迹核验</span></div><div className={'app '+(collapsed?'collapsed':'')}>
    <aside className="sidebar"><div className="brand"><div className="brand-mark"><img src={logoMark} alt="链迹核验标识"/></div><div className="brand-word"><strong>链迹核验</strong><small>资金关联分析工作台</small></div></div><div className="nav-section">分析空间</div><nav aria-label="分析导航">{navs.map(({label:navLabel,icon:Icon,key})=><button key={navLabel} className={'nav-item '+(page===key?'active':'')} disabled={!key} aria-current={page===key?'page':undefined} onClick={()=>switchPage(key)} title={key?navLabel:navLabel+' · 待接入'}><Icon size={18}/><span>{navLabel}</span>{page===key&&<i/>}</button>)}</nav><div className="side-bottom"><div className="offline"><span className="status-light"/><span>本地分析空间</span></div><button className="sidebar-toggle" aria-label={collapsed?'展开导航':'收起导航'} onClick={()=>setCollapsed(!collapsed)}>{collapsed?<PanelLeftOpen size={18}/>:<><PanelLeftClose size={18}/><span>收起导航</span></>}</button></div></aside>
    <div className="workspace"><header className="topbar"><div className="breadcrumb"><FolderOpen size={17}/><b title={file}>{file?file.split(/[\\/]/).pop():'未选择输入'}</b><span className="simulation">模拟数据</span></div><div className="top-actions"><Button variant="outline" className="tool-button" disabled={busy} onClick={()=>chooseInput(false)}><FolderOpen size={16}/>选择输入</Button><Button variant="outline" className="tool-button" disabled={busy} onClick={()=>chooseInput(true)}><FileJson size={16}/>内置样例</Button><Button className="tool-button run-button" disabled={!file||busy} onClick={run}><Play size={15}/>{busy?'核验中…':'执行核验'}</Button><Button variant="outline" className="tool-button" disabled={!data||busy} onClick={save}><Download size={16}/>导出</Button></div></header>
      <section className="pagehead"><div className="title-line"><h1>{page==='graph'?'关联关系图谱':page==='all'?'全部核验结果':page==='timeline'?'事件时间轴':'其他核验结果'}</h1><span className="preview-badge"><span/>离线分析原型</span></div>{data&&<div className="summary"><span><b>{data.input_counts.claims}</b>输入主张</span><span><b>{data.input_counts.events}</b>输入事件</span><span><b>{rows.length}</b>核验结果</span><span className="summary-conflict"><b>{counts.CONFLICT}</b>条冲突结果</span><div className="summary-distribution" aria-label="结果类型分布">{Object.entries(counts).map(([t,n])=><i key={t} className={tones[t]} style={{flex:n}} title={names[t]+' '+n+' 条'}/>)}</div></div>}</section>
      {!data?<main className="initial-empty"><div className="initial-icon"><Network size={30}/></div><h2>开始一次关联核验</h2><p>选择模拟输入或加载内置样例，然后执行核验。</p><Button onClick={()=>chooseInput(true)} disabled={busy} className="initial-button"><FileJson size={16}/>加载内置样例</Button></main>:
       page==='other'?<OtherResults data={data} onShowClaim={c=>{const group=claims.find(x=>x.key===claimKey(c.case_id,c.claim_id));if(group){chooseClaim(group);switchPage('graph')}}}/>:
       <main className="analysis-layout"><section className="object-list" aria-label="资金主张列表"><div className="pane-heading"><h2>资金主张</h2><span className="count">{claims.length}</span></div><div className="searchbox"><Search size={16}/><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索编号或案件" aria-label="搜索编号或案件"/>{query&&<button aria-label="清除搜索" onClick={()=>setQuery('')}><X size={14}/></button>}</div><div className="list-caption"><span>主张编号</span><span>结果</span></div><div className="claim-list" ref={claimListRef}>{shownClaims.map(c=><button className={'claim-row '+(c.key===active?'selected':'')} key={c.key} aria-current={c.key===active?'true':undefined} onClick={()=>chooseClaim(c)} title={c.id+' · 案件 '+c.caseId}><span className={'claim-icon '+(c.rows.some(r=>r.type==='CONFLICT')?'has-conflict':'')}><FileText size={17}/></span><span className="claim-id">{c.id}</span><span className="row-count">{c.rows.length}</span></button>)}{!shownClaims.length&&<div className="empty-list">未找到匹配主张</div>}</div><div className="list-foot"><Layers size={14}/><span>按案件隔离候选关系</span></div></section>
         <section className="canvas-pane"><div className="canvas-toolbar"><div className="current-object"><FileText size={16}/><b title={current?.id}>{page==='all'?'全部事件—主张结果':page==='timeline'?'当前案件事件':current?.id||'选择资金主张'}</b><span>{page==='timeline'?'按输入事件时间排序':`${page==='all'?tableRows.length:graphRows.length} 条结果`}</span></div>{page==='graph'&&<div className="segmented" aria-label="视图切换"><span className={'segment-track '+(mode==='table'?'right':'')}/><button aria-pressed={mode==='graph'} onClick={()=>setMode('graph')}><Network size={15}/>图谱</button><button aria-pressed={mode==='table'} onClick={()=>setMode('table')}><List size={16}/>表格</button></div>}</div>
         {page!=='timeline'&&<div className="graph-filter"><label><SlidersHorizontal size={14}/><select value={filter} onChange={e=>changeFilter(e.target.value)} aria-label="核验结果类型"><option value="ALL">全部结果类型</option><option value="CANDIDATES">候选结果</option>{Object.keys(names).map(x=><option value={x} key={x}>{names[x]}</option>)}</select><ChevronDown size={13}/></label></div>}
         <div className={'graph-container '+((page==='all'||mode==='table')?'table-mode':'')}>{page==='timeline'?<Timeline input={input} timelineTimes={timelineTimes} current={current} rows={rows} onChoose={r=>{chooseRow(r);switchPage('graph')}}/>:page==='all'||mode==='table'?<ResultTable rows={tableRows} selected={selectedRow} onChoose={chooseRow} showClaim={page==='all'}/>:current?.rows.length===0?<div className="gc-empty"><FileText size={30}/><strong>该主张未产生候选结果</strong><span>可在“其他核验”查看本次无输出主张。</span></div>:<GraphCanvas rows={graphRows} selectedId={selected} onSelect={id=>{setSelected(id);setTab('explain')}} claimKey={active}/>}</div>
         {page!=='timeline'&&<div className="graph-legend">{Object.entries(names).map(([key,name])=><span key={key}><i className={'legend-line '+tones[key]}/>{name}</span>)}</div>}</section><Inspector row={selectedRow} input={input} tab={tab} setTab={setTab}/></main>}
      <footer className={'statusbar '+(error?'status-error':'')}><span><i/>模拟数据</span><span role="status" className="run-status" title={status}>{status}</span>{page==='graph'&&<span className="status-boundary">候选关系不表示资金转移</span>}</footer>
    </div></div></>;
}

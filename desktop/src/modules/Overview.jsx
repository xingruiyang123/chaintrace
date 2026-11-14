import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  LayoutDashboard, ShieldCheck, Waypoints, Link2Off, ArrowRight, ChevronDown,
} from 'lucide-react';
import './modules.css';
import CaseDecisionPanel from './CaseDecisionPanel.jsx';
import './Overview.css';

/**
 * 分析总览 —— 核验链路 + 追缴链路。
 * props: { result, dataset, caseId, onPick, verification, rows, counts }
 * 契约：docs/接口契约_混币法币识别_v0_1.md ｜ 样式：docs/新增可视化模块界面基线_v0_1.md
 *
 * 【关键元素索引，供材料引用】
 * · `.ov-strip`：某条链路未加载时的单行提示条（`padding:11px 14px`，图标 + 一句引导，实测高 42px），
 *   替代此前占半屏的空卡片，把高度让给 `.ov-table`；两条都有数据时不渲染。
 * · `.ov-group[data-state="empty"]`：KPI 组空态（斜纹底 + 「未加载」徽标），数字一律显示 `—`，不拿 0 冒充。
 * · `.ov-table`：案件对照表，行 `tabIndex=0` + Enter/Space 下钻 `onPick({type:'case'})`；
 *   1600×1000 下两行均在第 1 屏（行底 849/896，页脚顶 931）。
 * · `.ov-more-bar`：兜底滚动胶囊，`scrollHeight-scrollTop-clientHeight > 24` 时出现，滚到底自隐。
 */

// 结果类型顺序与 App.jsx 的 names 一致，五色取自界面基线 §2 的状态色。
const TYPES = [
  { key: 'EXACT', label: '编号规则匹配', color: '#238a78', hint: '编号或字段可直接对上' },
  { key: 'CANDIDATE_STRONG', label: '较强候选', color: '#5576b8', hint: '多数维度支持，仍是候选' },
  { key: 'CANDIDATE_REVIEW', label: '待复核候选', color: '#bd8734', hint: '需要人工判断' },
  { key: 'INSUFFICIENT', label: '信息不足', color: '#8492a3', hint: '缺字段，不能下结论' },
  { key: 'CONFLICT', label: '明确冲突', color: '#c65e58', hint: '存在互斥证据' },
];

const SIGNALS = [
  { key: 'green', label: '优先核查出口', color: '#238a78' },
  { key: 'yellow', label: '持续监控', color: '#bd8734' },
  { key: 'red', label: '暂不执行', color: '#c65e58' },
];

const share = (part, total) => (total > 0 ? Math.round((part / total) * 100) : 0);

const showCount = (value) => (Number.isFinite(value) ? value.toLocaleString('en-US') : '—');

/** summary 缺字段时用出口的可控性等级回推，两种情况都不编造数字。 */
function signalOf(caseItem) {
  const summary = caseItem?.summary || {};
  const hasSummary = Number.isFinite(summary.green) || Number.isFinite(summary.yellow) || Number.isFinite(summary.red);
  if (hasSummary) {
    return { green: Number(summary.green) || 0, yellow: Number(summary.yellow) || 0, red: Number(summary.red) || 0 };
  }
  const tally = { green: 0, yellow: 0, red: 0 };
  for (const offramp of caseItem?.offramps || []) {
    const level = offramp?.controllability?.level;
    if (level === 'GREEN') tally.green += 1;
    else if (level === 'YELLOW') tally.yellow += 1;
    else if (level === 'RED') tally.red += 1;
  }
  return tally;
}

function buildOverviewModel({ result, dataset, caseId, verification, rows, counts }) {
  const verificationData = verification && typeof verification === 'object' ? verification : null;
  const eventClaimResults = Array.isArray(verificationData?.event_claim_results) ? verificationData.event_claim_results : [];
  const reconciliation = Array.isArray(verificationData?.event_reconciliation) ? verificationData.event_reconciliation : [];
  const withoutOutput = Array.isArray(verificationData?.claims_without_output) ? verificationData.claims_without_output : [];
  const verificationReady = Boolean(verificationData)
    && Boolean(verificationData.input_counts || eventClaimResults.length || reconciliation.length || withoutOutput.length);

  const rowList = Array.isArray(rows) ? rows : [];
  const countedByApp = Boolean(counts) && TYPES.some((type) => counts[type.key] != null && Number.isFinite(Number(counts[type.key])));
  const typeRows = TYPES.map((type) => ({
    ...type,
    count: countedByApp
      ? Math.max(0, Math.trunc(Number(counts[type.key]) || 0))
      : rowList.filter((row) => row?.type === type.key).length,
  }));
  const totalResults = typeRows.reduce((sum, type) => sum + type.count, 0);
  const conflictCount = typeRows.find((type) => type.key === 'CONFLICT')?.count || 0;

  const mixerCases = Array.isArray(result?.cases) ? result.cases : [];
  const mixerReady = mixerCases.length > 0;

  const verificationCaseIds = new Set();
  for (const item of eventClaimResults) if (item?.case_id) verificationCaseIds.add(item.case_id);
  for (const item of withoutOutput) if (item?.case_id) verificationCaseIds.add(item.case_id);
  for (const item of rowList) if (item?.case) verificationCaseIds.add(item.case);

  let pools = 0;
  let links = 0;
  let offramps = 0;
  const signals = { green: 0, yellow: 0, red: 0 };
  for (const caseItem of mixerCases) {
    pools += Array.isArray(caseItem?.mixer_pools) ? caseItem.mixer_pools.length : 0;
    links += Array.isArray(caseItem?.links) ? caseItem.links.length : 0;
    offramps += Array.isArray(caseItem?.offramps) ? caseItem.offramps.length : 0;
    const tally = signalOf(caseItem);
    signals.green += tally.green;
    signals.yellow += tally.yellow;
    signals.red += tally.red;
  }

  const signalParams = result?.parameters?.controllability?.signal || {};
  const greenAt = Number.isFinite(Number(signalParams.green)) ? Number(signalParams.green) : 75;
  const yellowAt = Number.isFinite(Number(signalParams.yellow)) ? Number(signalParams.yellow) : 60;

  const datasetCases = Array.isArray(dataset?.cases) ? dataset.cases : [];
  const datasetById = new Map(datasetCases.map((item) => [item.case_id, item]));
  const caseRows = mixerCases.map((caseItem) => {
    const source = datasetById.get(caseItem?.case_id);
    return {
      id: caseItem?.case_id || '（未命名案件）',
      network: caseItem?.network || source?.network || '—',
      title: source?.title || '',
      nodes: Array.isArray(source?.nodes) ? source.nodes.length : (Number.isFinite(caseItem?.summary?.nodes) ? caseItem.summary.nodes : null),
      edges: Array.isArray(source?.edges) ? source.edges.length : (Number.isFinite(caseItem?.summary?.edges) ? caseItem.summary.edges : null),
      pools: Array.isArray(caseItem?.mixer_pools) ? caseItem.mixer_pools.length : 0,
      links: Array.isArray(caseItem?.links) ? caseItem.links.length : 0,
      offramps: Array.isArray(caseItem?.offramps) ? caseItem.offramps.length : 0,
      signal: signalOf(caseItem),
    };
  });

  const activeCase = caseId || caseRows[0]?.id || null;

  return {
    verificationReady,
    mixerReady,
    anyData: verificationReady || mixerReady,
    typeRows,
    totalResults,
    conflictCount,
    verificationCaseIds,
    inputCounts: verificationData?.input_counts || null,
    eventClaimCount: eventClaimResults.length,
    reconciliationCount: reconciliation.length,
    withoutOutputCount: withoutOutput.length,
    caseRows,
    activeCase,
    pools,
    links,
    offramps,
    signals,
    greenAt,
    yellowAt,
  };
}

function KpiGroup({ index, title, icon: Icon, ready, items }) {
  return (
    <article className="ov-group" data-state={ready ? 'ready' : 'empty'}>
      <header className="ov-group-head">
        <span className="ov-group-icon"><Icon size={14} strokeWidth={1.7} /></span>
        <span className="ov-group-index">{index}</span>
        <h2>{title}</h2>
        <span className="ov-state">{ready ? '已加载' : '未加载'}</span>
      </header>
      <div className="ov-kpis">
        {items.map((item) => (
          <div className="ov-kpi" key={item.label}>
            <small>{item.label}</small>
            <b>{ready ? showCount(item.value) : '—'}</b>
            {ready && Number.isFinite(item.value) ? <em>{item.unit}</em> : null}
          </div>
        ))}
      </div>
    </article>
  );
}

export default function Overview({ result, dataset, caseId, onPick, verification, rows, counts }) {
  const model = useMemo(
    () => buildOverviewModel({ result, dataset, caseId, verification, rows, counts }),
    [result, dataset, caseId, verification, rows, counts],
  );
  const pick = (payload) => { if (typeof onPick === 'function') onPick(payload); };

  // 案件对照表常落在首屏下沿，给一个可感知的「下面还有内容」提示，看完自动收起。
  const scrollRef = useRef(null);
  const tableRef = useRef(null);
  const [moreBelow, setMoreBelow] = useState(false);
  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return undefined;
    const update = () => {
      const remaining = element.scrollHeight - element.scrollTop - element.clientHeight;
      setMoreBelow(remaining > 24);
    };
    update();
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(update) : null;
    observer?.observe(element);
    if (tableRef.current) observer?.observe(tableRef.current);
    return () => observer?.disconnect();
  }, [model.anyData, model.mixerReady, model.verificationReady]);

  const scrollMore = () => {
    const element = scrollRef.current;
    if (!element) return;
    if (typeof element.scrollBy === 'function') element.scrollBy({ top: Math.round(element.clientHeight * 0.7), behavior: 'smooth' });
    else element.scrollTop += Math.round(element.clientHeight * 0.7);
  };

  const header = (
    <header className="mod-head">
      <div>
        <span className="mod-eyebrow">总览 · 两条链路</span>
        <h1>两条链路总览</h1>
        <p className="mod-sub">
          两条链路各自加载：核验链路问「关系对不对得上」，追缴链路问「钱出混币池落到哪个出口」；缺一条该组显示「—」，不拿 0 冒充。
        </p>
      </div>
      <div className="mod-meta">
        <span className="sim-chip">虚拟数据 · 仅演示</span>
        {model.activeCase ? <span className="mod-case">案件 {model.activeCase}</span> : null}
      </div>
    </header>
  );

  if (!model.anyData) {
    return (
      <section className="mod">
        {header}
        <div className="mod-empty">
          <LayoutDashboard size={30} strokeWidth={1.3} />
          <strong>还没有分析结果</strong>
          <span>先执行一次核验，或点击顶部「混币识别」载入混币样例。</span>
        </div>
        <footer className="mod-foot"><i />虚拟模拟数据 · 混币—法币的对应关系为算法候选，不表示资金转移已确认。</footer>
      </section>
    );
  }

  const verificationKpis = [
    { label: '案件', value: model.verificationCaseIds.size, unit: '个' },
    { label: '资金主张', value: model.inputCounts?.claims, unit: '条' },
    { label: '资金事件', value: model.inputCounts?.events, unit: '条' },
    { label: '核验结果', value: model.eventClaimCount, unit: '条' },
    { label: '冲突条数', value: model.conflictCount, unit: '条' },
  ];
  const mixerKpis = [
    { label: '虚拟案件', value: model.caseRows.length, unit: '个' },
    { label: '混币池', value: model.pools, unit: '个' },
    { label: '断链归因候选', value: model.links, unit: '条' },
    { label: '法币出口', value: model.offramps, unit: '个' },
    { label: '优先核查出口', value: model.signals.green, unit: '个' },
  ];

  const stackLabel = model.typeRows.map((type) => `${type.label} ${type.count} 条`).join('，');
  const signalTotal = model.signals.green + model.signals.yellow + model.signals.red;

  return (
    <section className="mod">
      {header}
      <div className="mod-body single">
        <div className="mod-main ov-main" ref={scrollRef} onScroll={() => {
          const element = scrollRef.current;
          if (!element) return;
          setMoreBelow(element.scrollHeight - element.scrollTop - element.clientHeight > 24);
        }}>
          <div className="ov-body">
            <CaseDecisionPanel result={result} caseId={caseId} />
            <section className="ov-band" aria-label="两条链路的关键指标">
              <KpiGroup index="01" title="核验链路" icon={ShieldCheck} ready={model.verificationReady} items={verificationKpis} />
              <KpiGroup index="02" title="追缴链路" icon={Waypoints} ready={model.mixerReady} items={mixerKpis} />
            </section>

            {model.verificationReady && !model.mixerReady && (
              <div className="ov-gap" role="note">
                <span className="ov-gap-icon"><Link2Off size={17} strokeWidth={1.6} /></span>
                <div className="ov-gap-text">
                  <strong>追缴链路未加载</strong>
                  <p>
                    核验结果已经在，但混币池、断链归因和法币出口这三个环节还是空的，所以 02 组显示「—」。
                    只有混币识别跑过以后，追缴侧才会有数字和下钻入口。
                  </p>
                </div>
                <button type="button" className="ov-gap-action" onClick={() => pick({ type: 'action', id: 'load-mixer' })}>
                  载入混币样例<ArrowRight size={14} />
                </button>
              </div>
            )}

            {model.mixerReady && !model.verificationReady && (
              <div className="ov-gap" role="note">
                <span className="ov-gap-icon"><Link2Off size={17} strokeWidth={1.6} /></span>
                <div className="ov-gap-text">
                  <strong>核验链路未加载</strong>
                  <p>
                    追缴侧已有识别结果，但本次没有事件—主张结果，所以 01 组显示「—」。
                  </p>
                </div>
                <button type="button" className="ov-gap-action" onClick={() => pick({ type: 'action', id: 'run-verification' })}>
                  执行一次核验<ArrowRight size={14} />
                </button>
              </div>
            )}

            <div className={model.verificationReady && model.mixerReady ? 'ov-grid' : 'ov-grid ov-grid-single'}>
              {model.verificationReady ? (
                <section className="ov-card">
                  <header className="ov-card-head">
                    <h2>结果类型分布</h2>
                    <span>{model.totalResults} 条核验结果</span>
                  </header>
                  <>
                    <div
                      className="ov-stack"
                      data-empty={model.totalResults === 0}
                      role="img"
                      aria-label={`结果类型分布：${stackLabel}`}
                    >
                      {model.totalResults > 0
                        ? model.typeRows.filter((type) => type.count > 0).map((type) => (
                          <span
                            key={type.key}
                            className="ov-seg"
                            style={{ flexGrow: type.count, background: type.color }}
                            title={`${type.label} ${type.count} 条 · ${share(type.count, model.totalResults)}%`}
                          />
                        ))
                        : <span className="ov-stack-empty">本次核验没有产生结果</span>}
                    </div>
                    <ul className="ov-legend">
                      {model.typeRows.map((type) => (
                        <li
                          key={type.key}
                          data-zero={type.count === 0}
                          title={`${type.label} · ${type.hint}｜${type.count} 条，占 ${share(type.count, model.totalResults)}%`}
                        >
                          <i style={{ background: type.color }} />
                          <span className="ov-legend-name">{type.label}</span>
                          <b>{type.count}</b>
                          <em>{share(type.count, model.totalResults)}%</em>
                        </li>
                      ))}
                    </ul>
                    <p className="ov-note">
                      候选和冲突不能合并成「命中」：较强候选、待复核候选只说明值得跟进的方向；明确冲突要先排除矛盾再采信，
                      否则会把互相矛盾的两条关系算成两条成果。
                    </p>
                  </>
                </section>
              ) : null}

              {model.mixerReady ? (
                <section className="ov-card">
                <header className="ov-card-head">
                  <h2>信号灯分布</h2>
                  <span>{signalTotal} 个法币出口</span>
                </header>
                  <>
                    <div
                      className="ov-signal-bar"
                      data-empty={signalTotal === 0}
                      role="img"
                      aria-label={`信号灯分布：绿灯 ${model.signals.green}，黄灯 ${model.signals.yellow}，红灯 ${model.signals.red}`}
                    >
                      {signalTotal > 0
                        ? SIGNALS.filter((signal) => model.signals[signal.key] > 0).map((signal) => (
                          <span
                            key={signal.key}
                            className="ov-seg"
                            style={{ flexGrow: model.signals[signal.key], background: signal.color }}
                            title={`${signal.label} ${model.signals[signal.key]} 个`}
                          />
                        ))
                        : <span className="ov-stack-empty">没有可控性评分</span>}
                    </div>
                    <ul className="ov-signal-list">
                      {SIGNALS.map((signal) => (
                        <li key={signal.key} data-zero={model.signals[signal.key] === 0}>
                          <i style={{ background: signal.color }} />
                          <span className="ov-signal-name">{signal.label}</span>
                          <b>{model.signals[signal.key]}</b>
                        </li>
                      ))}
                    </ul>
                    <p className="ov-signal-threshold">
                      阈值：绿灯 ≥ {model.greenAt} 分 · 黄灯 {model.yellowAt}–{model.greenAt - 1} 分 · 红灯 {model.yellowAt} 分以下
                    </p>
                    <p className="ov-why">
                      <b>为什么重要</b>
                      绿灯出口的可控性最高，是可以立刻申请冻结的那一批；黄灯先监控资金流向再决定，红灯证据不够，
                      暂不执行，免得把合规账户一起冻掉。
                    </p>
                  </>
                </section>
              ) : null}
            </div>

            <section className="ov-card">
              <header className="ov-card-head">
                <h2>案件对照</h2>
                <span>{model.mixerReady ? `${model.caseRows.length} 个虚拟案件 · 点击行查看该案件` : '追缴链路未加载'}</span>
              </header>
              {model.mixerReady ? (
                <div className="ov-table-wrap" ref={tableRef}>
                  <table className="ov-table">
                    <thead>
                      <tr>
                        <th scope="col">案件</th>
                        <th scope="col">网络</th>
                        <th scope="col" className="ov-col-num">节点</th>
                        <th scope="col" className="ov-col-num">边</th>
                        <th scope="col" className="ov-col-num">混币池</th>
                        <th scope="col" className="ov-col-num">归因候选</th>
                        <th scope="col" className="ov-col-num">出口</th>
                        <th scope="col">绿 / 黄 / 红</th>
                      </tr>
                    </thead>
                    <tbody>
                      {model.caseRows.map((row) => {
                        const active = row.id === model.activeCase;
                        const rowSignals = SIGNALS.filter((signal) => row.signal[signal.key] > 0);
                        return (
                          <tr
                            key={row.id}
                            tabIndex={0}
                            role="button"
                            aria-pressed={active}
                            aria-label={`案件 ${row.id}，${row.network}，${row.pools} 个混币池，${row.links} 条归因候选`}
                            onClick={() => pick({ type: 'case', id: row.id })}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter' || event.key === ' ') {
                                event.preventDefault();
                                pick({ type: 'case', id: row.id });
                              }
                            }}
                          >
                            <td>
                              <span className="ov-case-id">{row.id}</span>
                              {row.title ? <span className="ov-case-title" title={row.title}>{row.title}</span> : null}
                            </td>
                            <td><span className="ov-net">{row.network}</span></td>
                            <td className="ov-col-num">{showCount(row.nodes)}</td>
                            <td className="ov-col-num">{showCount(row.edges)}</td>
                            <td className="ov-col-num">{showCount(row.pools)}</td>
                            <td className="ov-col-num">{showCount(row.links)}</td>
                            <td className="ov-col-num">{showCount(row.offramps)}</td>
                            <td>
                              <span className="ov-sig">
                                <span className="ov-sig-bar" aria-hidden="true">
                                  {rowSignals.map((signal) => (
                                    <i
                                      key={signal.key}
                                      style={{ flexGrow: row.signal[signal.key], background: signal.color }}
                                    />
                                  ))}
                                </span>
                                <span className="ov-sig-nums">
                                  <b className="ov-tone-exact" title={`绿灯 ${row.signal.green}`}>{row.signal.green}</b>
                                  <b className="ov-tone-review" title={`黄灯 ${row.signal.yellow}`}>{row.signal.yellow}</b>
                                  <b className="ov-tone-conflict" title={`红灯 ${row.signal.red}`}>{row.signal.red}</b>
                                </span>
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="ov-card-empty">
                  追缴链路未加载，没有可对照的虚拟案件。载入混币样例后，这里按案件列出节点、边、混币池、归因候选与出口。
                </p>
              )}
            </section>
          </div>
          {moreBelow && (
            <div className="ov-more-bar">
              <button type="button" className="ov-more" onClick={scrollMore}>
                <ChevronDown size={14} />向下滚动查看案件对照表
              </button>
            </div>
          )}
        </div>
      </div>
      <footer className="mod-foot"><i />虚拟模拟数据 · 混币—法币的对应关系为算法候选，不表示资金转移已确认。</footer>
    </section>
  );
}

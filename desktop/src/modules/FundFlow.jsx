import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Waypoints, Wallet, Shuffle, Landmark, Banknote, Building2, TriangleAlert,
  Layers, GitFork, ArrowRight, MousePointer2, Link2Off,
} from 'lucide-react';
import { effectiveTierOf, identifiabilityLabel, identifiabilityNote, isConditional } from '../analysis.js';
import './modules.css';
import './FundFlow.css';

/**
 * 资金流向 —— 手写 SVG 泳道分层图。
 * props: { result, dataset, caseId, onPick, verification, focus }
 * 契约：docs/接口契约_混币法币识别_v0_1.md ｜ 样式：docs/新增可视化模块界面基线_v0_1.md
 *
 * 【跨页下钻】focus={type,id} 由 App 在其它页 onPick 后传入：type 为 node/pool 且 id 在当前图里时，
 * 选中该节点并 scrollIntoView({block:'center', behavior:'smooth'})；focus 为 null、类型不符或 id 找不到时什么都不做。
 *
 * 【关键元素索引，供材料引用】
 * · `.ff-break-tip`：断口浮层（HTML 叠加层，定位 `left: breakX-6 / top: 40`，宽 `breakCaptionW = gutter-24 = 128px`）。
 *   齿轮符号 `BreakGear` + 虚线断口竖线 `breakX`（池列右侧专用列，`gutter` 152px / 窄屏 <1000px 收到 92px），全图只挂一处文案。
 * · `.ff-node`：节点卡片，宽 `cardW = clamp(132, 200, laneW-8)`；label 去括号后缀，全称进 title，实测零截断。
 * · `.ff-side`：右栏，`overflow-y:auto` + `scrollbar-gutter:stable`（实测轨道 11px 常驻可见，无横向溢出）。
 * · 边宽 `widthOf()`：log10(金额) 线性映射到 1.2–9 px，不是面积比例。
 */

const NODE_TYPES = {
  wallet: { label: '普通地址', color: '#728095', icon: Wallet },
  mixer: { label: '混币服务', color: '#b68332', icon: Shuffle },
  exchange: { label: '交易所', color: '#238a78', icon: Landmark, outlet: true },
  otc: { label: '场外承兑', color: '#228a7b', icon: Banknote, outlet: true },
  payment_gateway: { label: '支付网关', color: '#5576b8', icon: Building2, outlet: true },
  underground_bank: { label: '地下钱庄', color: '#c65e58', icon: TriangleAlert, outlet: true },
  bridge: { label: '跨链桥', color: '#8492a3', icon: Waypoints, outlet: true },
  defi: { label: 'DeFi 协议', color: '#8492a3', icon: Layers, outlet: true },
};

const EDGE_STYLE = {
  normal: { label: '普通转移', color: '#8f9db2', dash: null },
  split: { label: '拆分', color: '#8f9db2', dash: null },
  aggregate: { label: '聚合', color: '#8f9db2', dash: null },
  mixer_in: { label: '入混币', color: '#b68332', dash: '6 5' },
  mixer_out: { label: '出混币', color: '#b68332', dash: '6 5' },
  to_fiat: { label: '兑现入金', color: '#238a78', dash: null },
  bridge_out: { label: '跨链转出', color: '#8492a3', dash: '2 4' },
};

const MECHANISM = {
  coinjoin: 'CoinJoin 多输入混合',
  equal_output: '等额输出拆分',
  tornado_like: 'Tornado 式固定面额存提',
  unknown: '未识别机制',
};

const RISK = { high: '高', medium: '中', low: '低' };
const LEVEL = { GREEN: '优先核查出口', YELLOW: '持续监控', RED: '暂不执行' };

const LANES = [
  { key: 'source', title: '涉案源', note: 'SIM_SOURCE' },
  { key: 'pre', title: '中转 · 混币前', note: '拆分 / 归集' },
  { key: 'mixer', title: '混币池', note: '混合服务' },
  { key: 'post', title: '中转 · 混币后', note: '出金中转' },
  { key: 'outlet', title: '法币出口', note: '交易所 / OTC / 网关' },
];
const LANE_INDEX = { source: 0, pre: 1, mixer: 2, post: 3, outlet: 4 };

const MIN_CANVAS_W = 620;
const HEAD_H = 58;
const PAD_X = 14;
const PAD_BOTTOM = 24;

const CARD_H = 64;
const CARD_GAP = 16;
const CARD_MAX_W = 200;

/** 卡片上只留「类型 + 序号」，「（虚拟）」「（干扰）」这类后缀进 title，避免为它牺牲标签宽度。 */
const shortLabel = (node) => String(node.label || node.id).replace(/[（(][^（）()]*[）)]/g, '').trim() || node.id;

const AMOUNT_UNIT = BigInt('100000000');

/** 金额是十进制字符串，用整数缩放求和，不经过浮点、也不做四舍五入。 */
function parseAmount(value) {
  const text = String(value == null ? '' : value).trim();
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const [whole, fraction = ''] = text.split('.');
  const digits = (fraction + '00000000').slice(0, 8);
  return BigInt(whole) * AMOUNT_UNIT + BigInt(digits);
}

function formatScaled(value) {
  const whole = value / AMOUNT_UNIT;
  const fraction = (value % AMOUNT_UNIT).toString().padStart(8, '0');
  return `${whole}.${fraction}`;
}

function sumAmounts(list) {
  let total = BigInt(0);
  for (const item of list) {
    const scaled = parseAmount(item?.amount);
    if (scaled !== null) total += scaled;
  }
  return formatScaled(total);
}

function formatTime(value) {
  const text = String(value == null ? '' : value);
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(text);
  return match ? `${match[1]} ${match[2]}` : (text || '—');
}

function assignLanes(nodes, incoming, outgoing) {
  const laneOf = new Map();
  for (const node of nodes) {
    if (node.type === 'mixer') laneOf.set(node.id, 'mixer');
    else if (NODE_TYPES[node.type]?.outlet) laneOf.set(node.id, 'outlet');
    else if ((node.tags || []).includes('SIM_SOURCE')) laneOf.set(node.id, 'source');
  }
  const post = new Set();
  const forward = [];
  for (const node of nodes) {
    if (node.type !== 'mixer') continue;
    for (const edge of outgoing.get(node.id) || []) {
      if (!laneOf.has(edge.to) && !post.has(edge.to)) { post.add(edge.to); forward.push(edge.to); }
    }
  }
  while (forward.length) {
    const id = forward.shift();
    for (const edge of outgoing.get(id) || []) {
      if (!laneOf.has(edge.to) && !post.has(edge.to)) { post.add(edge.to); forward.push(edge.to); }
    }
  }
  const pre = new Set();
  const backward = [];
  for (const node of nodes) {
    if (node.type !== 'mixer') continue;
    for (const edge of incoming.get(node.id) || []) {
      if (!laneOf.has(edge.from) && !pre.has(edge.from)) { pre.add(edge.from); backward.push(edge.from); }
    }
  }
  while (backward.length) {
    const id = backward.shift();
    for (const edge of incoming.get(id) || []) {
      if (!laneOf.has(edge.from) && !pre.has(edge.from)) { pre.add(edge.from); backward.push(edge.from); }
    }
  }
  for (const node of nodes) {
    if (laneOf.has(node.id)) continue;
    if (post.has(node.id)) laneOf.set(node.id, 'post');
    else if (pre.has(node.id)) laneOf.set(node.id, 'pre');
    else if ((node.tags || []).includes('SIM_POSTMIX')) laneOf.set(node.id, 'post');
    else if (!(incoming.get(node.id) || []).length && (outgoing.get(node.id) || []).length) laneOf.set(node.id, 'source');
    else laneOf.set(node.id, 'pre');
  }
  return laneOf;
}

function computeLayout(nodes, edges, width, height) {
  const outgoing = new Map();
  const incoming = new Map();
  for (const edge of edges) {
    if (!outgoing.has(edge.from)) outgoing.set(edge.from, []);
    outgoing.get(edge.from).push(edge);
    if (!incoming.has(edge.to)) incoming.set(edge.to, []);
    incoming.get(edge.to).push(edge);
  }
  const laneOf = assignLanes(nodes, incoming, outgoing);
  const grouped = { source: [], pre: [], mixer: [], post: [], outlet: [] };
  for (const node of nodes) {
    const key = laneOf.get(node.id) || 'pre';
    grouped[key].push(node);
  }
  const flowScore = (node) => {
    let total = 0;
    for (const edge of incoming.get(node.id) || []) total += Number(edge.amount) || 0;
    for (const edge of outgoing.get(node.id) || []) total += Number(edge.amount) || 0;
    return total;
  };
  for (const key of Object.keys(grouped)) {
    grouped[key].sort((a, b) => flowScore(b) - flowScore(a) || String(a.id).localeCompare(String(b.id)));
  }

  const laneCounts = LANES.map((lane) => grouped[lane.key].length);
  const maxCount = Math.max(1, ...laneCounts);
  const needed = HEAD_H + maxCount * (CARD_H + CARD_GAP) + PAD_BOTTOM;
  const H = Math.max(height, needed);
  // 断口列宽随容器收放：窄屏收到 92px，宁可节点窄一点也不出横向滚动条。
  const narrow = width < 1000;
  const gutter = narrow ? 92 : 152;
  const laneArea = Math.max(narrow ? 420 : 500, width - PAD_X * 2 - gutter);
  const laneW = laneArea / 5;
  const cardW = Math.max(narrow ? 108 : 132, Math.min(CARD_MAX_W, laneW - 8));
  const laneLeft = LANES.map((_, index) => PAD_X + laneW * index + (index >= 3 ? gutter : 0));
  const laneCenter = laneLeft.map((left) => left + laneW / 2);

  const positions = new Map();
  const laneTop = HEAD_H;
  const laneBottom = H - PAD_BOTTOM;
  const available = laneBottom - laneTop;
  LANES.forEach((lane, index) => {
    const list = grouped[lane.key];
    if (!list.length) return;
    const slot = available / list.length;
    list.forEach((node, order) => {
      const y = laneTop + slot * order + (slot - CARD_H) / 2;
      positions.set(node.id, {
        id: node.id, lane: lane.key, laneIndex: index,
        x: laneCenter[index] - cardW / 2, y, w: cardW, h: CARD_H, cy: y + CARD_H / 2,
      });
    });
  });

  const breakX = PAD_X + laneW * 3 + 13;
  const breakCaptionW = Math.max(96, gutter - 24);
  const mixerNodes = grouped.mixer
    .map((node) => ({ node, point: positions.get(node.id) }))
    .filter((item) => item.point)
    .sort((a, b) => a.point.cy - b.point.cy);
  const breakSegments = [];
  let cursor = HEAD_H + 54;
  for (const item of mixerNodes) {
    const top = item.point.cy - 20;
    if (top > cursor + 2) breakSegments.push([cursor, top]);
    cursor = item.point.cy + 20;
  }
  if (H - PAD_BOTTOM > cursor + 2) breakSegments.push([cursor, H - PAD_BOTTOM]);

  const amountNumbers = edges.map((edge) => Number(edge.amount)).filter((value) => Number.isFinite(value) && value > 0);
  const minAmount = amountNumbers.length ? Math.min(...amountNumbers) : 1;
  const maxAmount = amountNumbers.length ? Math.max(...amountNumbers) : 1;
  const lo = Math.log10(minAmount);
  const hi = Math.log10(maxAmount);
  const widthOf = (amount) => {
    const value = Number(amount);
    if (!Number.isFinite(value) || value <= 0) return 1.2;
    if (hi - lo < 1e-6) return 5;
    const ratio = (Math.log10(value) - lo) / (hi - lo);
    return Math.round((1.2 + Math.min(1, Math.max(0, ratio)) * 7.8) * 10) / 10;
  };

  return {
    width, H, laneW, cardW, laneLeft, laneCenter, laneCounts, positions,
    grouped, incoming, outgoing, breakX, breakSegments, mixerNodes, widthOf, breakCaptionW,
  };
}

function edgeGeometry(from, to) {
  if (!from || !to) return null;
  if (from.laneIndex === to.laneIndex) {
    const x = from.x + from.w;
    const bend = 44;
    return `M ${x} ${from.cy} C ${x + bend} ${from.cy}, ${x + bend} ${to.cy}, ${x} ${to.cy}`;
  }
  if (to.laneIndex > from.laneIndex) {
    const x1 = from.x + from.w;
    const y1 = from.cy;
    const x2 = to.x;
    const y2 = to.cy;
    const bend = Math.max(36, (x2 - x1) * 0.45);
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  }
  const x1 = from.x;
  const y1 = from.cy;
  const x2 = to.x + to.w;
  const y2 = to.cy;
  const bend = Math.max(40, (x1 - x2) * 0.3);
  return `M ${x1} ${y1} C ${x1 - bend} ${y1}, ${x2 + bend} ${y2}, ${x2} ${y2}`;
}

function BreakGear({ cx, cy }) {
  const radius = 7.5;
  const teeth = [];
  for (let index = 0; index < 8; index += 1) {
    const angle = (Math.PI * 2 * index) / 8;
    teeth.push(
      <line
        key={index}
        x1={cx + Math.cos(angle) * (radius + 2)}
        y1={cy + Math.sin(angle) * (radius + 2)}
        x2={cx + Math.cos(angle) * (radius + 5)}
        y2={cy + Math.sin(angle) * (radius + 5)}
        stroke="#b68332"
        strokeWidth="1.6"
        strokeLinecap="round"
      />,
    );
  }
  return (
    <g>
      <circle cx={cx} cy={cy} r={radius + 7} fill="#f9fbfd" />
      <circle cx={cx} cy={cy} r={radius} fill="#fff" stroke="#b68332" strokeWidth="1.6" />
      <circle cx={cx} cy={cy} r={radius * 0.32} fill="#b68332" opacity="0.55" />
      {teeth}
    </g>
  );
}

export default function FundFlow({ result, dataset, caseId, onPick, focus }) {
  const cases = Array.isArray(dataset?.cases) ? dataset.cases : [];
  const activeCase = useMemo(() => {
    if (!cases.length) return null;
    return cases.find((item) => item.case_id === caseId) || cases[0];
  }, [cases, caseId]);
  const resultCase = useMemo(() => {
    const list = Array.isArray(result?.cases) ? result.cases : [];
    if (!list.length) return null;
    return list.find((item) => item.case_id === (activeCase?.case_id || caseId)) || list[0];
  }, [result, activeCase, caseId]);

  const nodes = Array.isArray(activeCase?.nodes) ? activeCase.nodes : [];
  const edges = Array.isArray(activeCase?.edges) ? activeCase.edges : [];
  const asset = activeCase?.asset || '';
  const hasGraph = nodes.length > 0;

  const scrollerRef = useRef(null);
  const nodeRefs = useRef(new Map());
  const [box, setBox] = useState({ w: MIN_CANVAS_W, h: 520 });
  const [selected, setSelected] = useState(null);
  const [hovered, setHovered] = useState(null);
  const caseKey = activeCase?.case_id || '';

  useEffect(() => { setSelected(null); setHovered(null); }, [caseKey]);

  useEffect(() => {
    const element = scrollerRef.current;
    if (!element) return undefined;
    const read = () => {
      const width = Math.max(element.clientWidth, MIN_CANVAS_W);
      const height = Math.max(element.clientHeight, 340);
      setBox((prev) => (prev.w === width && prev.h === height ? prev : { w: width, h: height }));
    };
    read();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(read);
    observer.observe(element);
    return () => observer.disconnect();
  }, [caseKey, hasGraph]);

  const layout = useMemo(
    () => (hasGraph ? computeLayout(nodes, edges, box.w, box.h) : null),
    [hasGraph, nodes, edges, box.w, box.h],
  );

  const nodeById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const edgeById = useMemo(() => new Map(edges.map((edge) => [edge.id, edge])), [edges]);
  const links = Array.isArray(resultCase?.links) ? resultCase.links : [];
  const pools = Array.isArray(resultCase?.mixer_pools) ? resultCase.mixer_pools : [];
  const offramps = Array.isArray(resultCase?.offramps) ? resultCase.offramps : [];

  const nodeTotals = useMemo(() => {
    const map = new Map();
    for (const node of nodes) {
      const incoming = edges.filter((edge) => edge.to === node.id);
      const outgoing = edges.filter((edge) => edge.from === node.id);
      map.set(node.id, {
        inSum: sumAmounts(incoming), outSum: sumAmounts(outgoing),
        inCount: incoming.length, outCount: outgoing.length,
      });
    }
    return map;
  }, [nodes, edges]);

  const totals = useMemo(() => {
    if (!hasGraph) return null;
    let all = BigInt(0);
    let fiat = BigInt(0);
    let maxEdge = null;
    let maxValue = -1;
    for (const edge of edges) {
      const scaled = parseAmount(edge.amount);
      if (scaled !== null) all += scaled;
      if (edge.pattern === 'to_fiat' && scaled !== null) fiat += scaled;
      const value = Number(edge.amount);
      if (Number.isFinite(value) && value > maxValue) { maxValue = value; maxEdge = edge; }
    }
    return { total: formatScaled(all), fiat: formatScaled(fiat), maxEdge };
  }, [hasGraph, edges]);

  // 跨页下钻：App 在其它页点「节点 / 混币池」后跳到这里，并带上 focus={type,id}。
  // 只处理 node / pool；id 不在这张图里就什么都不做，保持默认状态。
  useEffect(() => {
    if (!focus || !focus.id || !hasGraph) return undefined;
    if (focus.type !== 'node' && focus.type !== 'pool') return undefined;
    if (!nodeById.has(focus.id)) return undefined;
    setHovered(null);
    setSelected({ kind: 'node', id: focus.id });
    const frame = requestAnimationFrame(() => {
      const element = nodeRefs.current.get(focus.id);
      if (element && typeof element.scrollIntoView === 'function') {
        element.scrollIntoView({ block: 'center', inline: 'center', behavior: 'smooth' });
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [focus, nodeById, hasGraph]);

  const pick = (payload) => { if (typeof onPick === 'function') onPick(payload); };
  const active = hovered || selected;
  const activeEdgeId = active?.kind === 'edge' ? active.id : null;
  const activeNodeId = active?.kind === 'node' ? active.id : null;
  const selectedEdge = active?.kind === 'edge' ? edgeById.get(active.id) : null;
  const selectedNode = active?.kind === 'node' ? nodeById.get(active.id) : null;
  const dimming = Boolean(activeEdgeId || activeNodeId);

  const toggle = (payload) => setSelected((current) => (
    current && current.kind === payload.kind && current.id === payload.id ? null : payload
  ));

  const relatedLinks = selectedEdge
    ? links.filter((link) => link.deposit_edge === selectedEdge.id || link.withdrawal_edge === selectedEdge.id)
    : [];
  const edgePoolId = selectedEdge
    ? (relatedLinks[0]?.pool_id
      || (nodeById.get(selectedEdge.from)?.type === 'mixer' ? selectedEdge.from : null)
      || (nodeById.get(selectedEdge.to)?.type === 'mixer' ? selectedEdge.to : null))
    : null;
  const nodePool = selectedNode ? pools.find((pool) => pool.pool_id === selectedNode.id) : null;
  const nodeOfframp = selectedNode ? offramps.find((item) => item.node_id === selectedNode.id) : null;

  const header = (
    <header className="mod-head">
      <div>
        <span className="mod-eyebrow">追缴空间 · 资金流向</span>
        <h1>资金流向图</h1>
        <p className="mod-sub">
          按 涉案源 → 中转 → 混币池 → 中转 → 法币出口 分栏还原资金走向；混币池右侧标断口，断口之外是算法归因候选。
        </p>
      </div>
      <div className="mod-meta">
        <span className="sim-chip">虚拟数据 · 仅演示</span>
        {activeCase ? (
          <span className="mod-case">案件 {activeCase.case_id} · {activeCase.network || asset}</span>
        ) : null}
        {hasGraph ? <span className="mod-source">{nodes.length} 节点 / {edges.length} 连线</span> : null}
      </div>
    </header>
  );

  if (!hasGraph) {
    return (
      <section className="mod">
        {header}
        <div className="mod-body single">
          <div className="mod-main ff-main">
            <div className="mod-empty">
              <Waypoints size={30} strokeWidth={1.3} />
              <strong>还没有资金流向数据</strong>
              <span>点击顶部「混币识别」载入内置样例后，这里会按涉案源到法币出口分栏画出资金流与金额带宽。</span>
            </div>
          </div>
        </div>
        <footer className="mod-foot"><i />虚拟模拟数据 · 混币—法币的对应关系为算法候选，不表示资金转移已确认。</footer>
      </section>
    );
  }

  const maxLaneCount = Math.max(1, ...layout.laneCounts);

  return (
    <section className="mod">
      {header}
      <div className="mod-body">
        <div className="mod-main ff-main" ref={scrollerRef}>
          <div className="ff-canvas" style={{ width: layout.width, height: layout.H }}>
            <svg
              className="ff-svg"
              width={layout.width}
              height={layout.H}
              viewBox={`0 0 ${layout.width} ${layout.H}`}
              role="img"
              aria-label={`资金流向泳道图，${nodes.length} 个节点，${edges.length} 条连线`}
            >
              <defs>
                <marker id="ff-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                  <path d="M 0 0 L 10 5 L 0 10 z" fill="#238a78" />
                </marker>
              </defs>

              {LANES.map((lane, index) => (
                <g key={lane.key}>
                  <rect
                    x={layout.laneLeft[index]}
                    y={HEAD_H - 12}
                    width={layout.laneW}
                    height={layout.H - HEAD_H + 12 - PAD_BOTTOM + 6}
                    rx="10"
                    fill={index % 2 === 0 ? '#ffffff' : '#fbfcfe'}
                    opacity={index % 2 === 0 ? '0.55' : '0.8'}
                  />
                  <text x={layout.laneCenter[index]} y="23" textAnchor="middle" className="ff-lane-title">{lane.title}</text>
                  <text x={layout.laneCenter[index]} y="39" textAnchor="middle" className="ff-lane-sub">
                    {layout.laneCounts[index]} 个节点 · {lane.note}
                  </text>
                </g>
              ))}

              {edges.map((edge) => {
                const from = layout.positions.get(edge.from);
                const to = layout.positions.get(edge.to);
                if (!from || !to) return null;
                const path = edgeGeometry(from, to);
                const style = EDGE_STYLE[edge.pattern] || EDGE_STYLE.normal;
                const isActive = activeEdgeId === edge.id;
                const connected = Boolean(activeNodeId) && (edge.from === activeNodeId || edge.to === activeNodeId);
                const dim = dimming && !isActive && !connected;
                return (
                  <g
                    key={edge.id}
                    className="ff-edge"
                    tabIndex={0}
                    role="button"
                    data-dim={dim}
                    data-active={isActive || connected}
                    aria-label={`连线 ${edge.txid || edge.id}，金额 ${edge.amount} ${edge.asset || asset}，${style.label}`}
                    onPointerEnter={() => setHovered({ kind: 'edge', id: edge.id })}
                    onPointerLeave={() => setHovered(null)}
                    onFocus={() => setHovered({ kind: 'edge', id: edge.id })}
                    onBlur={() => setHovered(null)}
                    onClick={() => toggle({ kind: 'edge', id: edge.id })}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        toggle({ kind: 'edge', id: edge.id });
                      }
                    }}
                  >
                    <path
                      className="ff-edge-line"
                      d={path}
                      fill="none"
                      stroke={style.color}
                      strokeWidth={layout.widthOf(edge.amount)}
                      strokeDasharray={style.dash || undefined}
                      markerEnd={edge.pattern === 'to_fiat' ? 'url(#ff-arrow)' : undefined}
                    />
                    <path className="ff-edge-hit" d={path} fill="none" />
                  </g>
                );
              })}
              {layout.mixerNodes.length > 0 && (
                <g className="ff-break">
                  {layout.breakSegments.map((segment, index) => (
                    <line
                      key={index}
                      x1={layout.breakX}
                      y1={segment[0]}
                      x2={layout.breakX}
                      y2={segment[1]}
                      stroke="#d9b98b"
                      strokeWidth="1.4"
                      strokeDasharray="4 6"
                    />
                  ))}
                  {layout.mixerNodes.map(({ node, point }) => (
                    <g key={node.id}>
                      <line
                        x1={point.x + point.w + 3}
                        y1={point.cy}
                        x2={layout.breakX - 10}
                        y2={point.cy}
                        stroke="#d9b98b"
                        strokeWidth="1.2"
                        strokeDasharray="2 3"
                      />
                      <line x1={layout.breakX - 5} y1={point.cy - 18} x2={layout.breakX + 5} y2={point.cy - 18} stroke="#b68332" strokeWidth="1.6" strokeLinecap="round" />
                      <line x1={layout.breakX - 5} y1={point.cy + 18} x2={layout.breakX + 5} y2={point.cy + 18} stroke="#b68332" strokeWidth="1.6" strokeLinecap="round" />
                      <BreakGear cx={layout.breakX} cy={point.cy} />
                    </g>
                  ))}
                </g>
              )}
            </svg>

            {nodes.map((node) => {
              const point = layout.positions.get(node.id);
              if (!point) return null;
              const meta = NODE_TYPES[node.type] || NODE_TYPES.wallet;
              const Icon = meta.icon;
              const sums = nodeTotals.get(node.id) || { inSum: '0.00000000', outSum: '0.00000000', inCount: 0, outCount: 0 };
              const outflowOnly = point.lane === 'source' || sums.inSum === '0.00000000';
              const amount = outflowOnly ? sums.outSum : sums.inSum;
              const direction = outflowOnly ? '流出' : '流入';
              const isActive = activeNodeId === node.id;
              const linked = Boolean(activeEdgeId) && (selectedEdge?.from === node.id || selectedEdge?.to === node.id);
              const dim = dimming && !isActive && !linked;
              const pool = pools.find((item) => item.pool_id === node.id);
              return (
                <button
                  key={node.id}
                  type="button"
                  className="ff-node"
                  ref={(element) => {
                    if (element) nodeRefs.current.set(node.id, element);
                    else nodeRefs.current.delete(node.id);
                  }}
                  style={{ left: point.x, top: point.y, width: point.w, height: point.h, '--ff-accent': meta.color }}
                  data-dim={dim}
                  data-active={isActive}
                  data-mixer={node.type === 'mixer'}
                  aria-pressed={selected?.kind === 'node' && selected.id === node.id}
                  aria-label={`${meta.label} ${node.label || node.id}，${direction} ${amount} ${asset || ''}`}
                  onPointerEnter={() => setHovered({ kind: 'node', id: node.id })}
                  onPointerLeave={() => setHovered(null)}
                  onFocus={() => setHovered({ kind: 'node', id: node.id })}
                  onBlur={() => setHovered(null)}
                  onClick={() => toggle({ kind: 'node', id: node.id })}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      toggle({ kind: 'node', id: node.id });
                    }
                  }}
                >
                  <span className="ff-node-head">
                    <span className="ff-node-icon"><Icon size={12} strokeWidth={1.9} /></span>
                    <span className="ff-node-type">{meta.label}</span>
                    {pool ? <span className="ff-node-score" title={`混币判别分 ${pool.detection_score}`}>{pool.detection_score}</span> : null}
                  </span>
                  <span className="ff-node-label" title={node.label || node.id}>{shortLabel(node)}</span>
                  <span className="ff-node-amt">
                    <b title={`${direction} ${amount} ${asset || ''}`}>{amount}</b>
                    {amount.length > 11 ? null : <em>{direction} · {asset || '—'}</em>}
                  </span>
                </button>
              );
            })}

            <div className="ff-break-tip" style={{ left: layout.breakX - 6, top: 40, width: layout.breakCaptionW }}>
              <Link2Off size={11} />
              <span>链上关联在此中断<br /><em>下方为算法归因候选</em></span>
            </div>
            <div className="ff-hint"><MousePointer2 size={12} />点击连线或节点看明细 · Tab 可逐条聚焦</div>
          </div>
        </div>

        <aside className="mod-side ff-side">
          {!active && (
            <>
              <div className="side-block">
                <h4>图例<span className="ff-side-count">{nodes.length} 节点 / {edges.length} 连线</span></h4>
                <ul className="ff-legend">
                  <li><i className="ff-swatch ff-swatch-normal" /><span>普通 / 拆分 / 聚合</span></li>
                  <li><i className="ff-swatch ff-swatch-mix" /><span>入混币 / 出混币（虚线）</span></li>
                  <li><i className="ff-swatch ff-swatch-fiat" /><span>兑现入金（实线 + 箭头）</span></li>
                  <li><i className="ff-swatch ff-swatch-break" /><span>断口：链上关联中断处</span></li>
                </ul>
                <p className="ff-tip">线宽按 log10(金额) 线性映射到 1.2–9 px，只表示量级差异，不代表面积精确比例。</p>
                <p className="ff-break-note">
                  <Link2Off size={12} />
                  齿轮断口表示链上关联在此中断：断口左边可核验，右边（混币后中转与法币出口）来自算法归因，是候选。
                </p>
              </div>

              <div className="side-block">
                <h4>各层节点<span className="ff-side-count">{nodes.length} 个</span></h4>
                <ul className="ff-lane-stats">
                  {LANES.map((lane, index) => (
                    <li key={lane.key}>
                      <span className="ff-lane-name">{lane.title}</span>
                      <b>{layout.laneCounts[index]}</b>
                      <i aria-hidden="true"><span style={{ width: `${layout.laneCounts[index] ? Math.max(10, (layout.laneCounts[index] / maxLaneCount) * 100) : 0}%` }} /></i>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="side-block">
                <h4>流量</h4>
                <dl className="ff-dl">
                  <div><dt>总流量（全部连线）</dt><dd>{totals.total}<em>{asset}</em></dd></div>
                  <div><dt>兑现入金合计</dt><dd>{totals.fiat}<em>{asset}</em></dd></div>
                  <div><dt>最大单笔</dt><dd>{totals.maxEdge ? totals.maxEdge.amount : '—'}<em>{asset}</em></dd></div>
                  <div><dt>最大单笔 txid</dt><dd className="ff-mono">{totals.maxEdge?.txid || '—'}</dd></div>
                </dl>
              </div>
            </>
          )}

          {active && selectedEdge && (
            <>
              <div className="side-block">
                <h4>
                  已选连线
                  <button type="button" className="ff-clear" onClick={() => setSelected(null)}>取消</button>
                </h4>
                <div className="ff-detail-head">
                  <span className="ff-pattern" style={{ '--ff-pattern': (EDGE_STYLE[selectedEdge.pattern] || EDGE_STYLE.normal).color }}>
                    {(EDGE_STYLE[selectedEdge.pattern] || EDGE_STYLE.normal).label}
                  </span>
                  <span className="ff-detail-id ff-mono">{selectedEdge.txid || selectedEdge.id}</span>
                </div>
                <dl className="ff-dl">
                  <div><dt>txid</dt><dd className="ff-mono">{selectedEdge.txid || '—'}</dd></div>
                  <div><dt>金额</dt><dd>{selectedEdge.amount}<em>{selectedEdge.asset || asset}</em></dd></div>
                  <div><dt>时间</dt><dd>{formatTime(selectedEdge.time)}<em>UTC</em></dd></div>
                  <div><dt>pattern</dt><dd className="ff-mono">{selectedEdge.pattern}</dd></div>
                  <div><dt>转出</dt><dd>{nodeById.get(selectedEdge.from)?.label || selectedEdge.from}</dd></div>
                  <div><dt>转入</dt><dd>{nodeById.get(selectedEdge.to)?.label || selectedEdge.to}</dd></div>
                </dl>
              </div>

              <div className="side-block">
                <h4>相关断链归因<span className="ff-side-count">{relatedLinks.length} 条</span></h4>
                {relatedLinks.length ? (
                  <ul className="ff-link-list">
                    {relatedLinks.map((link) => (
                      <li key={link.link_id}>
                        <span className="ff-tier" data-tier={effectiveTierOf(link)} title={identifiabilityNote(link)}>
                          {effectiveTierOf(link) === 'WITHHELD' ? identifiabilityLabel(link) : effectiveTierOf(link)}
                        </span>
                        {isConditional(link) && (
                          <span className="ff-tie-tag" title={identifiabilityNote(link)}>{identifiabilityLabel(link)}</span>
                        )}
                        <span className="ff-link-main">
                          <b>{link.link_id}</b>
                          <small>{link.deposit_edge} ↔ {link.withdrawal_edge}</small>
                        </span>
                        <span className="ff-link-score">{Number.isFinite(Number(link.score)) ? Number(link.score) : '—'}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>这条连线没有进入归因候选：它要么在断口之前，要么是旁路的干扰流量。</p>
                )}
                {edgePoolId ? (
                  <button type="button" className="ff-pool-btn" onClick={() => pick({ type: 'pool', id: edgePoolId })}>
                    <GitFork size={14} />查看该混币池的归因<ArrowRight size={13} />
                  </button>
                ) : null}
              </div>
            </>
          )}

          {active && selectedNode && (
            <>
              <div className="side-block">
                <h4>
                  已选节点
                  <button type="button" className="ff-clear" onClick={() => setSelected(null)}>取消</button>
                </h4>
                <div className="ff-detail-head">
                  <span
                    className="ff-pattern"
                    style={{ '--ff-pattern': (NODE_TYPES[selectedNode.type] || NODE_TYPES.wallet).color }}
                  >
                    {(NODE_TYPES[selectedNode.type] || NODE_TYPES.wallet).label}
                  </span>
                  <span className="ff-detail-id ff-mono">{selectedNode.id}</span>
                </div>
                <p className="ff-node-title">{selectedNode.label || selectedNode.id}</p>
                <dl className="ff-dl">
                  <div><dt>风险</dt><dd>{RISK[selectedNode.risk] || selectedNode.risk || '—'}</dd></div>
                  <div><dt>标签</dt><dd>{(selectedNode.tags || []).join(' / ') || '—'}</dd></div>
                  <div><dt>流入</dt><dd>{(nodeTotals.get(selectedNode.id) || {}).inSum}<em>{asset}</em></dd></div>
                  <div><dt>流出</dt><dd>{(nodeTotals.get(selectedNode.id) || {}).outSum}<em>{asset}</em></dd></div>
                  <div>
                    <dt>笔数（入 / 出）</dt>
                    <dd>{(nodeTotals.get(selectedNode.id) || {}).inCount} / {(nodeTotals.get(selectedNode.id) || {}).outCount}</dd>
                  </div>
                </dl>
              </div>

              {nodePool && (
                <div className="side-block">
                  <h4>混币池判定<span className="ff-side-count">判别分 {nodePool.detection_score}</span></h4>
                  <dl className="ff-dl">
                    <div><dt>机制</dt><dd>{MECHANISM[nodePool.mechanism] || nodePool.mechanism || '—'}</dd></div>
                    <div><dt>回合 / 窗口</dt><dd>{nodePool.rounds} 回合 · {nodePool.window_hours} 小时</dd></div>
                    <div><dt>入金 / 出金</dt><dd>{nodePool.inflow} / {nodePool.outflow}<em>{asset}</em></dd></div>
                    <div><dt>入 / 出边</dt><dd>{(nodePool.deposit_edges || []).length} / {(nodePool.withdrawal_edges || []).length}</dd></div>
                  </dl>
                  <button type="button" className="ff-pool-btn" onClick={() => pick({ type: 'pool', id: nodePool.pool_id })}>
                    <GitFork size={14} />查看该混币池的归因<ArrowRight size={13} />
                  </button>
                </div>
              )}

              {nodeOfframp && (
                <div className="side-block">
                  <h4>出口识别<span className="ff-side-count">{LEVEL[nodeOfframp.controllability?.level] || nodeOfframp.controllability?.level}</span></h4>
                  <dl className="ff-dl">
                    <div><dt>S_off</dt><dd>{nodeOfframp.s_off}</dd></div>
                    <div><dt>S_ctrl</dt><dd>{nodeOfframp.controllability?.s_ctrl ?? '—'}</dd></div>
                    <div><dt>来自混币池</dt><dd>{nodeOfframp.from_mixer}<em>{asset}</em></dd></div>
                    <div><dt>入金笔数</dt><dd>{nodeOfframp.deposit_count}</dd></div>
                  </dl>
                </div>
              )}

              {!nodePool && !nodeOfframp && (
                <div className="side-block">
                  <h4>说明</h4>
                  <p>
                    {selectedNode.type === 'wallet' && assignedLaneLabel(layout, selectedNode.id)}。
                    该节点没有对应的混币池或出口判定结果，明细以上方流入流出为准。
                  </p>
                </div>
              )}
            </>
          )}
        </aside>
      </div>
      <footer className="mod-foot">
        <i />虚拟模拟数据 · 混币—法币的对应关系为算法候选，不表示资金转移已确认。
        {resultCase ? <span className="ff-foot-note"><Link2Off size={11} />断口右侧为算法归因</span> : null}
      </footer>
    </section>
  );
}

function assignedLaneLabel(layout, nodeId) {
  const point = layout?.positions?.get(nodeId);
  if (!point) return '未进入泳道';
  const lane = LANES[point.laneIndex];
  return `位于「${lane?.title || '未知'}」层`;
}

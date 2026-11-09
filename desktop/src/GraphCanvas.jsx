import React, { memo, useEffect, useMemo, useRef } from 'react';
import {
  ReactFlow, ReactFlowProvider, Background, BackgroundVariant,
  Handle, Position, BaseEdge, EdgeLabelRenderer, getBezierPath,
  useNodesState, useReactFlow, useViewport, Panel,
} from '@xyflow/react';
import { FileText, ArrowLeftRight, Landmark, Minus, Plus, Scan, MousePointer2, Network } from 'lucide-react';
import '@xyflow/react/dist/style.css';
import './graph.css';

const TYPES = {
  EXACT: { label: '编号规则匹配', color: '#238a78', tone: 'exact' },
  CANDIDATE_STRONG: { label: '较强候选', color: '#5576b8', tone: 'strong' },
  CANDIDATE_REVIEW: { label: '待复核候选', color: '#bd8734', tone: 'review', dash: '7 5' },
  INSUFFICIENT: { label: '信息不足', color: '#8492a3', tone: 'missing', dash: '3 5' },
  CONFLICT: { label: '明确冲突', color: '#c65e58', tone: 'conflict', dash: '7 5' },
};
const kindLabel = (kind) => ({ ONCHAIN_TRANSFER: '链上转账', EXCHANGE_DEPOSIT: '平台充值', EXCHANGE_WITHDRAWAL: '平台提现', INTERNAL_TRANSFER: '平台内部记账' }[kind] || '资金事件');
const FIT_OPTIONS = { padding: .05, minZoom: .05, maxZoom: 1.05 };
const stopCanvasEvent = (event) => event.stopPropagation();

const ObjectNode = memo(function ObjectNode({ data }) {
  const isClaim = data.kind === 'claim';
  const Icon = isClaim ? FileText : data.row.kind === 'EXCHANGE_DEPOSIT' ? Landmark : ArrowLeftRight;
  const activate = () => data.onActivate(isClaim ? null : data.row.event);
  return (
    <div
      className={`gc-object ${isClaim ? 'gc-claim' : 'gc-event'} ${data.focused ? 'is-focused' : ''} ${data.dimmed ? 'is-dimmed' : ''}`}
      style={{ '--object-accent': isClaim ? '#315d91' : (TYPES[data.row.type]?.color || '#5576b8') }}
      role="button" tabIndex={0}
      aria-label={isClaim ? `资金主张 ${data.label}，查看全部关系` : `资金事件 ${data.row.event}，${TYPES[data.row.type]?.label || data.row.type}`}
      aria-pressed={data.focused}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); activate(); }
      }}
    >
      {!isClaim && <Handle type="target" position={Position.Left} isConnectable={false} />}
      <div className="gc-node-heading"><span className="gc-node-icon"><Icon size={17} strokeWidth={1.8}/></span><span>{isClaim ? '资金主张' : kindLabel(data.row.kind)}</span></div>
      <div className="gc-node-id" title={data.label}>{data.label}</div>
      <div className="gc-node-meta">{isClaim ? <><span className="gc-mini-dot"/>{data.count} 条核验结果</> : data.row.asset}</div>
      {isClaim && <Handle type="source" position={Position.Right} isConnectable={false} />}
    </div>
  );
});

function RelationEdge(props) {
  const [path, labelX, labelY] = getBezierPath(props);
  const { row, active, dimmed, onActivate } = props.data;
  const type = TYPES[row.type] || TYPES.INSUFFICIENT;
  return (
    <>
      <BaseEdge id={props.id} path={path} interactionWidth={24} style={{ stroke: type.color, strokeWidth: active ? 2.5 : 1.8, strokeDasharray: type.dash, opacity: dimmed ? .76 : 1, transition: 'stroke-width 160ms, opacity 160ms' }} />
      <EdgeLabelRenderer>
        <button
          className={`gc-edge-label nodrag nopan ${type.tone} ${active ? 'is-active' : ''} ${dimmed ? 'is-dimmed' : ''}`}
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)` }}
          onClick={(event) => { event.stopPropagation(); onActivate(row.event); }}
          aria-label={`查看 ${row.claim} 与 ${row.event} 的${type.label}依据`}
          aria-pressed={active}
        >{type.label}</button>
      </EdgeLabelRenderer>
    </>
  );
}
const nodeTypes = { object: ObjectNode };
const edgeTypes = { relation: RelationEdge };

function CanvasControls() {
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const { zoom } = useViewport();
  return <Panel position="bottom-left" className="gc-controls nodrag nopan"
    onPointerDown={stopCanvasEvent} onMouseDown={stopCanvasEvent}
    onClick={stopCanvasEvent} onDoubleClick={stopCanvasEvent}
  >
    <button onClick={() => zoomOut({ duration: 180 })} title="缩小" aria-label="缩小图谱"><Minus size={16}/></button>
    <span aria-label="画布缩放比例">{Math.round(zoom * 100)}%</span>
    <button onClick={() => zoomIn({ duration: 180 })} title="放大" aria-label="放大图谱"><Plus size={16}/></button>
    <i/>
    <button onClick={() => fitView({ ...FIT_OPTIONS, duration: 240 })} title="适应画布" aria-label="适应画布"><Scan size={16}/></button>
  </Panel>;
}

function Flow({ rows, selectedId, onSelect, claimKey }) {
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const { fitView } = useReactFlow();
  const containerRef = useRef(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  const activate = useRef((id) => selectRef.current(id)).current;
  const signature = JSON.stringify(rows);

  useEffect(() => {
    if (!rows.length) { setNodes([]); return; }
    const claimId = JSON.stringify(['claim', rows[0].case, rows[0].claim]);
    const gap = 216;
    const centerY = ((rows.length - 1) * gap + 132) / 2;
    setNodes([
      { id: claimId, type: 'object', position: { x: 20, y: centerY - 58 }, data: { kind: 'claim', label: rows[0].claim, count: rows.length, onActivate: activate }, style: { width: 200, height: 116 } },
      ...rows.map((row, index) => ({ id: JSON.stringify(['event', row.case, row.event]), type: 'object', position: { x: 410, y: index * gap }, data: { kind: 'event', row, label: row.event, onActivate: activate }, style: { width: 220, height: 132 } })),
    ]);
    let frameTwo;
    const frameOne = requestAnimationFrame(() => {
      frameTwo = requestAnimationFrame(() => fitView({ ...FIT_OPTIONS, duration: 220 }));
    });
    return () => { cancelAnimationFrame(frameOne); cancelAnimationFrame(frameTwo); };
  }, [signature, claimKey, activate, setNodes, fitView]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    let previousSize;
    let timer;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      const changed = previousSize && (Math.abs(width - previousSize.width) > 1 || Math.abs(height - previousSize.height) > 1);
      previousSize = { width, height };
      if (!changed || width <= 0 || height <= 0) return;
      clearTimeout(timer);
      timer = setTimeout(() => fitView({ ...FIT_OPTIONS, duration: 200 }), 150);
    });
    observer.observe(container);
    return () => { observer.disconnect(); clearTimeout(timer); };
  }, [fitView, !!rows.length]);

  const displayNodes = useMemo(() => nodes.map(node => ({
    ...node,
    data: { ...node.data, focused: node.data.kind === 'claim' ? !selectedId : node.data.row.event === selectedId, dimmed: !!selectedId && node.data.kind === 'event' && node.data.row.event !== selectedId },
  })), [nodes, selectedId]);
  const edges = useMemo(() => rows.map(row => ({
    id: JSON.stringify(['relation', row.case, row.claim, row.event]),
    source: JSON.stringify(['claim', row.case, row.claim]), target: JSON.stringify(['event', row.case, row.event]),
    type: 'relation', selectable: false,
    data: { row, active: row.event === selectedId, dimmed: !!selectedId && row.event !== selectedId, onActivate: activate },
  })), [signature, selectedId, activate]);

  if (!rows.length) return <div className="gc-empty"><Network size={30} strokeWidth={1.25}/><strong>当前筛选没有关系</strong><span>调整结果类型，查看此主张的其他核验结果。</span></div>;
  return <div ref={containerRef} className="gc-flow"><ReactFlow
    nodes={displayNodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
    onNodesChange={onNodesChange}
    onNodeClick={(_event, node) => activate(node.data.kind === 'claim' ? null : node.data.row.event)}
    onEdgeClick={(_event, edge) => activate(edge.data.row.event)}
    onPaneClick={(event) => {
      if (event.target.classList?.contains('react-flow__pane')) activate(null);
    }}
    nodesConnectable={false} edgesReconnectable={false} nodesFocusable={false} edgesFocusable={false}
    deleteKeyCode={null} selectionKeyCode={null} multiSelectionKeyCode={null}
    panOnDrag zoomOnScroll zoomOnPinch minZoom={.05} maxZoom={1.8}
    fitView fitViewOptions={FIT_OPTIONS}
    ariaLabelConfig={{ 'node.a11yDescription.default': '按 Enter 查看对应核验结果', 'controls.zoomIn.ariaLabel': '放大', 'controls.zoomOut.ariaLabel': '缩小' }}
  >
    <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#d6e0eb"/>
    <CanvasControls/>
    <Panel position="bottom-right" className="gc-gesture nodrag nopan" onPointerDown={stopCanvasEvent} onMouseDown={stopCanvasEvent} onClick={stopCanvasEvent} onDoubleClick={stopCanvasEvent}><MousePointer2 size={13}/>点击节点或连线查看依据</Panel>
  </ReactFlow></div>;
}

export default function GraphCanvas(props) {
  return <div className="gc-root"><ReactFlowProvider><Flow {...props}/></ReactFlowProvider></div>;
}

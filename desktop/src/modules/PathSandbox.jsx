import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Route, Clock3, Coins, Flag, TriangleAlert } from 'lucide-react';
import './modules.css';
import './PathSandbox.css';

/**
 * 追缴路径沙盘（.ps-）
 * props: { result, dataset, caseId, onPick }
 * 数据契约：docs/接口契约_混币法币识别_v0_1.md §3.5（paths[]）
 * 视觉基线：docs/新增可视化模块界面基线_v0_1.md（§2 令牌 / §6 路径卡）
 *
 * 主路径 / 备选路径两列并排；best_intercept_node 那一步用强调描边。
 * 风险与被截周期都是估计，措辞用暖色提示，不用红色警报。
 */

const NODE_TYPE_LABEL = {
  wallet: '地址',
  mixer: '混币服务',
  exchange: '交易所',
  otc: '场外承兑',
  payment_gateway: '支付网关',
  underground_bank: '地下钱庄',
  bridge: '跨链桥',
  defi: 'DeFi 协议',
};
const EDGE_PATTERN_LABEL = {
  normal: '普通转移',
  split: '拆分',
  aggregate: '聚合',
  mixer_in: '入混币',
  mixer_out: '混币转出',
  to_fiat: '兑现入金',
  bridge_out: '跨链',
};
const RISK_LABEL = { high: '高', medium: '中', low: '低' };
const COST_RANK = { 低: 1, 中: 2, 高: 3 };

const toNum = (value, fallback = 0) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};
/** 汇总分：1 位小数 */
const score1 = (value) => toNum(value).toFixed(1);
/** 整数型数值（小时等）去掉多余的 .0 */
const tight = (value) => String(Number(toNum(value).toFixed(2)));
const stepNo = (step, index) => String(step.order ?? index + 1);
const timeText = (iso) => {
  if (!iso || typeof iso !== 'string') return '—';
  const found = iso.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  return found ? `${found[1]} ${found[2]} UTC` : iso;
};

function pickCase(result, caseId) {
  const cases = result?.cases || [];
  return cases.find((c) => c.case_id === caseId) || cases[0] || null;
}

function PathCard({ path, role, nodeById, activeKey, flashKey, onStep }) {
  const steps = Array.isArray(path.steps) ? path.steps : [];
  const best = path.best_intercept_node;
  const prerequisites = path.prerequisites || [];
  const risks = path.risks || [];

  return (
    <article className={`ps-card is-${role}`}>
      <header className="ps-card-head">
        <div className="ps-card-title">
          <span className="ps-role">{role === 'main' ? '主路径' : '备选路径'}</span>
          <b>{path.name || (role === 'main' ? '主路径' : '备选路径')}</b>
          <code>{path.path_id}</code>
        </div>
        <div className="ps-card-score">
          <b>{score1(path.score)}</b>
          <small>综合分</small>
        </div>
      </header>

      <div className="ps-card-meta">
        <span className="ps-chip">
          <Clock3 size={13} />
          {tight(path.estimated_hours)} 小时
        </span>
        <span className="ps-chip">
          <Coins size={13} />
          成本 {path.cost_level || '—'}
        </span>
        <span className="ps-chip is-best">
          <Flag size={13} />
          最佳拦截 {best || '—'}
        </span>
      </div>

      <ol className="ps-steps">
        {steps.map((step, index) => {
          const no = stepNo(step, index);
          const isBest = Boolean(best) && step.node_id === best;
          const node = nodeById.get(step.node_id);
          return (
            <li key={`${no}-${step.node_id}`} className={isBest ? 'is-best' : ''}>
              <button
                type="button"
                className={
                  'ps-step' +
                  (activeKey === `${path.path_id}#${no}` ? ' is-active' : '') +
                  (flashKey && flashKey === `${path.path_id}#${no}` ? ' is-flash' : '')
                }
                data-step-key={`${path.path_id}#${no}`}
                aria-pressed={activeKey === `${path.path_id}#${no}`}
                onClick={() => onStep(path, step, no)}
              >
                <span className="ps-step-no">{no}</span>
                <span className="ps-step-body">
                  <b className="ps-step-action">{step.action}</b>
                  <span className="ps-step-meta">
                    <code>{step.node_id}</code>
                    {node?.label ? <em>{node.label}</em> : null}
                    <span className="ps-step-edge">
                      边 <code>{step.edge_id || '—'}</code>
                    </span>
                  </span>
                </span>
                {isBest && <span className="ps-best-tag">最佳拦截点</span>}
              </button>
            </li>
          );
        })}
      </ol>

      <div className="ps-lists">
        <section className="ps-req">
          <h5>关键前提</h5>
          {prerequisites.length > 0 ? (
            <ul>
              {prerequisites.map((text, index) => (
                <li key={`${text}-${index}`}>{text}</li>
              ))}
            </ul>
          ) : (
            <p className="ps-none">未列出前提条件。</p>
          )}
        </section>
        <section className="ps-risk">
          <h5>
            <TriangleAlert size={12} />
            失败风险（估计）
          </h5>
          {risks.length > 0 ? (
            <ul>
              {risks.map((text, index) => (
                <li key={`${text}-${index}`}>{text}</li>
              ))}
            </ul>
          ) : (
            <p className="ps-none">未列出风险项。</p>
          )}
        </section>
      </div>
    </article>
  );
}

export default function PathSandbox({ result, dataset, caseId, onPick, focus }) {
  const [pickedKey, setPickedKey] = useState(null);
  const [flashKey, setFlashKey] = useState(null);
  const rootRef = useRef(null);
  const currentCase = pickCase(result, caseId);
  const paths = useMemo(() => currentCase?.paths || [], [currentCase]);

  const mainPath =
    paths.find((item) => item.fallback_path_id) ||
    paths.find((item) => String(item.name || '').includes('主')) ||
    paths[0] ||
    null;
  const altPath =
    paths.find((item) => item !== mainPath && item.path_id === mainPath?.fallback_path_id) ||
    paths.find((item) => item !== mainPath) ||
    null;

  const datasetCase = useMemo(
    () => (dataset?.cases || []).find((c) => c.case_id === currentCase?.case_id) || null,
    [dataset, currentCase]
  );
  const nodeById = useMemo(
    () => new Map(((datasetCase?.nodes || []).map((node) => [node.id, node]))),
    [datasetCase]
  );
  const edgeById = useMemo(
    () => new Map(((datasetCase?.edges || []).map((edge) => [edge.id, edge]))),
    [datasetCase]
  );

  const defaultKey = (() => {
    const steps = mainPath?.steps || [];
    if (!steps.length) return null;
    const found = steps.findIndex((step) => step.node_id === mainPath?.best_intercept_node);
    const index = found >= 0 ? found : 0;
    return `${mainPath.path_id}#${stepNo(steps[index], index)}`;
  })();
  const activeKey = pickedKey || defaultKey;

  // 跨页下钻：App 传来 focus 且 type==='node' 时，选中该节点所在的那一步。
  // 节点不在任何路径里 / focus 为 null 时，什么都不做（默认选中最佳拦截点不变）。
  const focusNodeId = focus && focus.type === 'node' ? focus.id : null;
  useEffect(() => {
    if (!focusNodeId) return;
    for (const path of paths) {
      const steps = Array.isArray(path.steps) ? path.steps : [];
      const index = steps.findIndex((step) => step.node_id === focusNodeId);
      if (index < 0) continue;
      const key = `${path.path_id}#${stepNo(steps[index], index)}`;
      setPickedKey(key);
      setFlashKey(key);
      return;
    }
  }, [focus, focusNodeId, paths]);

  // 一次性闪烁：滚到可视区，1.4 秒后收掉高亮（prefers-reduced-motion 下动画由全局关掉）
  useEffect(() => {
    if (!flashKey) return undefined;
    const el = rootRef.current ? rootRef.current.querySelector(`[data-step-key="${flashKey}"]`) : null;
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
    const timer = setTimeout(() => setFlashKey(null), 1400);
    return () => clearTimeout(timer);
  }, [flashKey]);

  const activePath =
    paths.find((item) =>
      (item.steps || []).some((step, index) => `${item.path_id}#${stepNo(step, index)}` === activeKey)
    ) || mainPath;
  const activeSteps = activePath?.steps || [];
  const activeStep =
    activeSteps.find((step, index) => `${activePath.path_id}#${stepNo(step, index)}` === activeKey) ||
    activeSteps[0] ||
    null;
  const activeIndex = activeStep
    ? activeSteps.findIndex((step) => step === activeStep)
    : -1;
  const activeNode = activeStep ? nodeById.get(activeStep.node_id) || null : null;
  const activeEdge = activeStep?.edge_id ? edgeById.get(activeStep.edge_id) || null : null;
  const isBestStep = Boolean(activeStep && activePath && activeStep.node_id === activePath.best_intercept_node);

  const maxHours = paths.reduce((acc, item) => Math.max(acc, toNum(item.estimated_hours)), 0) || 1;
  const maxScore = paths.reduce((acc, item) => Math.max(acc, toNum(item.score)), 0);

  function selectStep(path, step, no) {
    setPickedKey(`${path.path_id}#${no}`);
    if (onPick) onPick({ type: 'node', id: step.node_id });
  }

  const ready = paths.length > 0;
  const compareDims = [
    {
      key: 'score',
      label: '综合分',
      hint: '越高越好',
      width: (path) => Math.min(100, Math.max(2, toNum(path.score))),
      text: (path) => score1(path.score),
    },
    {
      key: 'hours',
      label: '预计周期',
      hint: '越短越好',
      width: (path) => Math.max(2, (toNum(path.estimated_hours) / maxHours) * 100),
      text: (path) => `${tight(path.estimated_hours)} 小时`,
    },
    {
      key: 'cost',
      label: '成本',
      hint: '越低越好',
      width: (path) => Math.max(2, ((COST_RANK[path.cost_level] || 0) / 3) * 100),
      text: (path) => path.cost_level || '—',
    },
  ];

  return (
    <section className="mod ps" ref={rootRef}>
      <header className="mod-head">
        <div>
          <span className="mod-eyebrow">追缴空间 · 路径沙盘</span>
          <h1>主路径与备选方案比选</h1>
          <p className="mod-sub">
            主路径与备选路径并排：竖向步骤流标出最佳拦截点，下面三条对比条给出综合分、周期与成本的取舍。
          </p>
        </div>
        <div className="mod-meta">
          <span className="sim-chip">虚拟数据 · 仅演示</span>
          {currentCase && (
            <span className="mod-case">
              案件 {currentCase.case_id} · {currentCase.network}
            </span>
          )}
        </div>
      </header>

      {ready ? (
        <>
          <div className="mod-kpis">
            <div className="kpi">
              <small>路径方案</small>
              <b>{paths.length}</b>
              <span>条</span>
            </div>
            <div className="kpi">
              <small>最高综合分</small>
              <b>{score1(maxScore)}</b>
            </div>
            <div className="kpi">
              <small>最短预计周期</small>
              <b>
                {tight(
                  paths.reduce(
                    (acc, item) => Math.min(acc, toNum(item.estimated_hours, Number.POSITIVE_INFINITY)),
                    Number.POSITIVE_INFINITY
                  )
                )}
              </b>
              <span>小时</span>
            </div>
            <div className="kpi">
              <small>主路径拦截点</small>
              <b className="ps-kpi-text">{mainPath?.best_intercept_node || '—'}</b>
            </div>
          </div>

          <div className="mod-body">
            <div className="mod-main ps-main">
              <div className="ps-cols">
                <PathCard
                  path={mainPath}
                  role="main"
                  nodeById={nodeById}
                  activeKey={activeKey}
                  flashKey={flashKey}
                  onStep={selectStep}
                />
                {altPath ? (
                  <PathCard
                    path={altPath}
                    role="alt"
                    nodeById={nodeById}
                    activeKey={activeKey}
                    flashKey={flashKey}
                    onStep={selectStep}
                  />
                ) : (
                  <div className="ps-card ps-card-blank">
                    <b>本次未给出备选路径</b>
                    <span>
                      契约允许 <code>fallback_path_id</code> 为 null。只有一个方案时，先按主路径评估，并在结论里注明缺少对照。
                    </span>
                  </div>
                )}
              </div>

              {altPath && (
                <div className="ps-compare">
                  <div className="ps-compare-head">
                    <b>两路径对比</b>
                    <span>条形只做相对比较：综合分越长越好，周期越长越慢，成本越高越难</span>
                  </div>
                  <div className="ps-compare-legend">
                    <span className="ps-legend-key is-main" />
                    <em>{mainPath?.name || '主路径'}</em>
                    <span className="ps-legend-key is-alt" />
                    <em>{altPath?.name || '备选路径'}</em>
                  </div>
                  {compareDims.map((dim) => (
                    <div className="ps-cmp-row" key={dim.key}>
                      <span className="ps-cmp-label">
                        {dim.label}
                        <small>{dim.hint}</small>
                      </span>
                      {[mainPath, altPath].map((path) => (
                        <span className="ps-cmp-cell" key={`${dim.key}-${path.path_id}`}>
                          <span className="ps-cmp-track">
                            <i
                              className={`ps-cmp-bar is-${path === mainPath ? 'main' : 'alt'}`}
                              style={{ width: `${dim.width(path)}%` }}
                            />
                          </span>
                          <b>{dim.text(path)}</b>
                        </span>
                      ))}
                    </div>
                  ))}
                </div>
              )}
            </div>

            <aside className="mod-side ps-side">
              {activeStep ? (
                <>
                  <div className="side-block ps-step-block">
                    <h4>
                      选中步骤
                      <em>
                        {activePath?.path_id} · 第 {activeStep.order ?? activeIndex + 1} 步
                      </em>
                    </h4>
                    <p className="ps-step-title">{activeStep.action}</p>
                    {isBestStep && <span className="ps-step-badge">最佳拦截点</span>}
                    <dl>
                      <div>
                        <dt>节点</dt>
                        <dd>
                          {activeNode?.label ? `${activeNode.label} ` : ''}
                          <code>{activeStep.node_id}</code>
                        </dd>
                      </div>
                      <div>
                        <dt>节点类型</dt>
                        <dd>{NODE_TYPE_LABEL[activeNode?.type] || activeNode?.type || '数据集中未找到'}</dd>
                      </div>
                      <div>
                        <dt>风险档位</dt>
                        <dd>{RISK_LABEL[activeNode?.risk] || activeNode?.risk || '—'}</dd>
                      </div>
                      <div>
                        <dt>首次出现</dt>
                        <dd>{timeText(activeNode?.first_seen)}</dd>
                      </div>
                      <div>
                        <dt>转出边</dt>
                        <dd>{activeStep.edge_id ? <code>{activeStep.edge_id}</code> : '无（该方案终点）'}</dd>
                      </div>
                    </dl>
                  </div>

                  {activeEdge ? (
                    <div className="side-block ps-edge-block">
                      <h4>该步资金</h4>
                      <dl>
                        <div>
                          <dt>金额</dt>
                          <dd>
                            {activeEdge.amount} {activeEdge.asset}
                          </dd>
                        </div>
                        <div>
                          <dt>时间</dt>
                          <dd>{timeText(activeEdge.time)}</dd>
                        </div>
                        <div>
                          <dt>模式</dt>
                          <dd>{EDGE_PATTERN_LABEL[activeEdge.pattern] || activeEdge.pattern || '—'}</dd>
                        </div>
                        <div>
                          <dt>交易号</dt>
                          <dd>
                            <code>{activeEdge.txid || '—'}</code>
                          </dd>
                        </div>
                      </dl>
                      <p className="ps-note">金额为数据集原值，未做四舍五入。</p>
                    </div>
                  ) : (
                    <div className="side-block ps-edge-block">
                      <h4>该步资金</h4>
                      <p className="ps-note">
                        {activeStep.edge_id
                          ? '数据集中未找到这条边，可能只存在于结果侧。'
                          : '该步骤是方案终点，没有对应资金边。'}
                      </p>
                    </div>
                  )}

                  <div className="side-block ps-legend">
                    <h4>读图说明</h4>
                    <ul>
                      <li>综合分由成功率、时效、成本加权得出；权重未在结果中给出，页面不代填。</li>
                      <li>周期与风险均为估计，用于比较方案，不构成行动结论。</li>
                      <li>「断链处」指混币池转出那一步，其对应关系仍是算法候选。</li>
                    </ul>
                  </div>
                </>
              ) : (
                <div className="mod-empty">
                  <Route size={26} strokeWidth={1.3} />
                  <strong>该案件没有步骤</strong>
                  <span>路径存在但没有 steps，无法定位。</span>
                </div>
              )}
            </aside>
          </div>
        </>
      ) : (
        <div className="mod-empty">
          <Route size={30} strokeWidth={1.3} />
          <strong>还没有路径方案</strong>
          <span>还没有混币识别结果。点击顶部「混币识别」后，这里生成主/备选两条追缴路径。</span>
        </div>
      )}

      <footer className="mod-foot">
        <i />
        本页全部数据为虚构模拟数据，仅用于展示原型能力；混币—法币对应关系为算法候选，不表示资金转移已确认。
      </footer>
    </section>
  );
}

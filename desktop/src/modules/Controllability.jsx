import React, { useEffect, useMemo, useState } from 'react';
import { Gauge, TriangleAlert } from 'lucide-react';
import './modules.css';
import './Controllability.css';

/**
 * 资产可控性评分（.ct-）
 * props: { result, dataset, caseId, onPick }
 * 数据契约：docs/接口契约_混币法币识别_v0_1.md §3.4（offramps[].controllability）、§1（公式口径）
 * 视觉基线：docs/新增可视化模块界面基线_v0_1.md（§2 状态色 / §6 雷达图、窗口条）
 *
 * 公式：S_ctrl = clamp(0,100, α·S_profile + β·S_off + γ·c − P)
 * α β γ 与 P 的档位表全部取自 result.parameters.controllability，页面不硬编码权重与阈值。
 * 冻结窗口为估计值，凡出现小时数处均带「估计」。
 */

const TYPE_LABEL = {
  exchange: '交易所',
  otc: '场外承兑',
  payment_gateway: '支付网关',
  underground_bank: '地下钱庄',
  dex: '去中心化交易所',
  bridge: '跨链桥',
};

/** 五维画像轴：键名固定取自契约 profile */
const AXES = [
  { key: 'rule_friendly', label: '规则友好' },
  { key: 'execution_efficiency', label: '执行效率' },
  { key: 'cost', label: '成本' },
  { key: 'response_hours', label: '响应时效' },
  { key: 'evidence', label: '证据充分' },
];

/** 状态色沿用基线：GREEN=exact 绿 / YELLOW=strong 蓝 / RED=conflict 暖红 */
const LEVEL_META = {
  GREEN: { name: '优先核查出口', color: '#238a78' },
  YELLOW: { name: '持续监控', color: '#5576b8' },
  RED: { name: '暂不执行', color: '#c65e58' },
};

const toNum = (value, fallback = 0) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};
/** 汇总分：1 位小数 */
const score1 = (value) => toNum(value).toFixed(1);
/** 算式用精确值：去掉浮点噪声与多余 .0 */
const tight = (value) => String(Number(toNum(value).toFixed(2)));
/** 两位小数，供逐项乘积核对 */
const r2 = (value) => String(Number(toNum(value).toFixed(2)));

function pickCase(result, caseId) {
  const cases = result?.cases || [];
  return cases.find((c) => c.case_id === caseId) || cases[0] || null;
}

function Radar({ values, color }) {
  const w = 336;
  const h = 276;
  const cx = 164;
  const cy = 136;
  const r = 92;
  const count = AXES.length;
  const ringRatios = [0.2, 0.4, 0.6, 0.8, 1];
  const angleOf = (i) => ((-90 + (360 / count) * i) * Math.PI) / 180;
  const pointOf = (i, ratio) => [
    cx + Math.cos(angleOf(i)) * r * ratio,
    cy + Math.sin(angleOf(i)) * r * ratio,
  ];
  const polygonOf = (ratio) =>
    AXES.map((_, i) => pointOf(i, ratio))
      .map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`)
      .join(' ');
  const valueAt = (axis) => Math.max(0, Math.min(100, toNum(values?.[axis.key])));
  const valuePolygon = AXES.map((axis, i) => pointOf(i, valueAt(axis) / 100))
    .map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`)
    .join(' ');

  return (
    <svg className="ct-radar" viewBox={`0 0 ${w} ${h}`} role="img" aria-label="五维画像雷达图，0 到 100 分，五环网格，每环 20 分">
      {ringRatios.map((ratio) => (
        <polygon key={`ring-${ratio}`} points={polygonOf(ratio)} fill="none" stroke="#e4eaf3" strokeWidth="1" />
      ))}
      {AXES.map((axis, i) => {
        const [x, y] = pointOf(i, 1);
        return (
          <line
            key={`axis-${axis.key}`}
            x1={cx}
            y1={cy}
            x2={x.toFixed(2)}
            y2={y.toFixed(2)}
            stroke="#eef2f8"
            strokeWidth="1"
          />
        );
      })}
      <polygon
        points={valuePolygon}
        fill={color}
        fillOpacity="0.14"
        stroke={color}
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      {AXES.map((axis, i) => {
        const [x, y] = pointOf(i, valueAt(axis) / 100);
        return <circle key={`dot-${axis.key}`} cx={x.toFixed(2)} cy={y.toFixed(2)} r="3" fill="#fff" stroke={color} strokeWidth="1.6" />;
      })}
      {AXES.map((axis, i) => {
        const a = angleOf(i);
        const cos = Math.cos(a);
        const sin = Math.sin(a);
        const lx = cx + cos * (r + 20);
        const ly = cy + sin * (r + 18);
        const anchor = Math.abs(cos) < 0.25 ? 'middle' : cos > 0 ? 'start' : 'end';
        return (
          <g key={`label-${axis.key}`}>
            <text className="ct-radar-label" x={lx.toFixed(1)} y={ly.toFixed(1)} textAnchor={anchor}>
              {axis.label}
            </text>
            <text className="ct-radar-value" x={lx.toFixed(1)} y={(ly + 13).toFixed(1)} textAnchor={anchor}>
              {tight(valueAt(axis))}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

export default function Controllability({ result, dataset, caseId, onPick, focus }) {
  const [pickedId, setPickedId] = useState(null);
  const currentCase = pickCase(result, caseId);
  const params = result?.parameters?.controllability || {};
  const alpha = Number(params.alpha);
  const beta = Number(params.beta);
  const gamma = Number(params.gamma);
  const hasWeights = [alpha, beta, gamma].every((v) => Number.isFinite(v));
  const greenTh = toNum(params.signal?.green, 75);
  const yellowTh = toNum(params.signal?.yellow, 60);
  const penalty = params.penalty || {};
  // S_off 口径与「法币出口」页保持一致：min(cap, Σ hits.score)，cap 读 parameters.offramp.cap
  const cap = toNum(result?.parameters?.offramp?.cap, 100);

  const offs = useMemo(() => {
    const list = (currentCase?.offramps || []).map((item) => {
      const ctrl = item.controllability || {};
      const hits = Array.isArray(item.hits) ? item.hits : [];
      const sumHits = hits.reduce((acc, hit) => acc + Math.max(0, toNum(hit.score)), 0);
      const sOffCalc = hits.length > 0 ? Math.min(cap, sumHits) : toNum(ctrl.s_off);
      const sCtrl = toNum(ctrl.s_ctrl);
      const level =
        ctrl.level ||
        (sCtrl >= greenTh ? 'GREEN' : sCtrl >= yellowTh ? 'YELLOW' : 'RED');
      return { ...item, ctrl, hits, sOffCalc, sOffField: toNum(ctrl.s_off), sCtrl, level };
    });
    return list.sort((a, b) => b.sCtrl - a.sCtrl);
  }, [currentCase, greenTh, yellowTh, cap]);

  const active = offs.find((item) => item.node_id === pickedId) || offs[0] || null;

  // 跨页下钻：focus 指向某个出口时切到它（雷达图、复算面板随之更新）。
  // focus 为 null / type 不是 offramp / id 不在本案件里时什么都不做。
  const focusId = focus && focus.type === 'offramp' ? focus.id : null;
  useEffect(() => {
    if (!focusId) return;
    const hit = offs.find((item) => item.node_id === focusId);
    if (hit) setPickedId(hit.node_id);
  }, [focus, focusId, offs]);

  const datasetCase = useMemo(
    () => (dataset?.cases || []).find((c) => c.case_id === currentCase?.case_id) || null,
    [dataset, currentCase]
  );
  const nodeRisk = useMemo(() => {
    if (!active || !datasetCase) return null;
    return (datasetCase.nodes || []).find((n) => n.id === active.node_id)?.risk || null;
  }, [active, datasetCase]);

  const asset = datasetCase?.asset || currentCase?.network || '';

  const levels = useMemo(() => {
    const groups = { GREEN: [], YELLOW: [], RED: [] };
    offs.forEach((item) => {
      (groups[item.level] || groups.RED).push(item);
    });
    return [
      { key: 'GREEN', name: LEVEL_META.GREEN.name, rule: `≥ ${tight(greenTh)}`, list: groups.GREEN },
      {
        key: 'YELLOW',
        name: LEVEL_META.YELLOW.name,
        rule: `${tight(yellowTh)}–${tight(greenTh - 1)}`,
        list: groups.YELLOW,
      },
      { key: 'RED', name: LEVEL_META.RED.name, rule: `< ${tight(yellowTh)}`, list: groups.RED },
    ];
  }, [offs, greenTh, yellowTh]);

  const maxFreeze = offs.reduce((acc, item) => Math.max(acc, toNum(item.ctrl.freeze_window_hours)), 0);
  const scaleMax = maxFreeze > 0 ? maxFreeze : 1;
  const greenCount = offs.filter((item) => item.level === 'GREEN').length;
  const maxCtrl = offs.reduce((acc, item) => Math.max(acc, item.sCtrl), 0);

  const ctrl = active?.ctrl || {};
  const sProfile = toNum(ctrl.s_profile);
  // 出口分：有 hits 时按 min(cap, Σ hits) 现算（与出口页同口径）；无 hits 时才回落到结果字段
  const hasHits = (active?.hits || []).length > 0;
  const sOff = hasHits ? active.sOffCalc : toNum(ctrl.s_off);
  const sOffFromCalc = hasHits;
  const sOffConflict = hasHits && Math.abs(active.sOffCalc - active.sOffField) > 0.05;
  const conf = toNum(ctrl.c);
  // 风险惩罚 P 优先按参数表取（parameters.controllability.penalty[risk]），无风险档位时才回落到结果字段 p
  const penFromParams =
    nodeRisk && Number.isFinite(Number(penalty[nodeRisk])) ? Number(penalty[nodeRisk]) : null;
  const pen = penFromParams === null ? toNum(ctrl.p) : penFromParams;
  const penSource =
    penFromParams === null ? '结果字段 controllability.p' : `parameters.controllability.penalty.${nodeRisk}`;
  const penConflict = penFromParams !== null && Math.abs(penFromParams - toNum(ctrl.p)) > 0.001;
  const sCtrl = active ? active.sCtrl : 0;
  const computed = hasWeights && active ? alpha * sProfile + beta * sOff + gamma * conf - pen : 0;
  const clamped = Math.min(100, Math.max(0, computed));
  const mismatch = hasWeights && active && Math.abs(clamped - sCtrl) > 0.05;
  const priorityBonus = toNum(ctrl.profile?.priority_bonus, 0);
  const levelColor = active ? (LEVEL_META[active.level] || LEVEL_META.RED).color : LEVEL_META.RED.color;

  function select(item) {
    setPickedId(item.node_id);
    if (onPick) onPick({ type: 'offramp', id: item.node_id });
  }

  const ready = offs.length > 0;

  return (
    <section className="mod ct">
      <header className="mod-head">
        <div>
          <span className="mod-eyebrow">追缴空间 · 可控性</span>
          <h1>信号灯分档与冻结窗口</h1>
          <p className="mod-sub">
            五维画像 + 出入口分 + 数据置信度 − 风险惩罚，落成信号灯与冻结窗口；右侧把算式逐项代入，可手算核对。
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
              <small>参与评分出口</small>
              <b>{offs.length}</b>
              <span>个</span>
            </div>
            <div className="kpi">
              <small>优先核查出口</small>
              <b>{greenCount}</b>
              <span>个（GREEN）</span>
            </div>
            <div className="kpi">
              <small>最高 S_ctrl</small>
              <b>{score1(maxCtrl)}</b>
            </div>
            <div className="kpi">
              <small>最长冻结窗口（估计）</small>
              <b>{tight(maxFreeze)}</b>
              <span>小时</span>
            </div>
          </div>

          <div className="mod-body">
            <div className="mod-main ct-main">
              <div className="ct-lights">
                {levels.map((group) => (
                  <div key={group.key} className={`ct-light is-${group.key.toLowerCase()}`}>
                    <div className="ct-light-head">
                      <i className="ct-light-dot" />
                      <b className="ct-light-name">{group.name}</b>
                      <span className="ct-light-key">{group.key}</span>
                      <em className="ct-light-rule">{group.rule}</em>
                      <span className="ct-light-count">{group.list.length}</span>
                    </div>
                    {group.list.length > 0 ? (
                      <ul className="ct-light-list">
                        {group.list.map((item) => (
                          <li key={item.node_id}>
                            <button
                              type="button"
                              className="ct-light-item"
                              aria-pressed={active && active.node_id === item.node_id}
                              onClick={() => select(item)}
                              title={`${item.label || item.node_id} · S_ctrl ${score1(item.sCtrl)}`}
                            >
                              <span className="ct-item-name">{item.label || item.node_id}</span>
                              <span className="ct-item-meta">
                                <em className="ct-type">{TYPE_LABEL[item.offramp_type] || item.offramp_type || '未标注'}</em>
                                <b>{score1(item.sCtrl)}</b>
                              </span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="ct-light-empty">本档没有出口</p>
                    )}
                  </div>
                ))}
              </div>

              <div className="ct-detail">
                <div className="ct-card ct-radar-card">
                  <div className="ct-card-head">
                    <b>五维画像</b>
                    <span>综合分 / S_ctrl：算法给出的排序分，不是拦截成功率</span>
                    {active && <em>{active.label || active.node_id}</em>}
                  </div>
                  <Radar values={ctrl.profile} color={levelColor} />
                  <div className="ct-radar-foot">
                    <span>
                      画像分 S_profile <b>{score1(sProfile)}</b>
                    </span>
                    {priorityBonus > 0 && <span className="ct-bonus">含优先通道加分 +{tight(priorityBonus)}</span>}
                    <span className="ct-sim-note">五维由画像库给出</span>
                  </div>
                </div>

                <div className="ct-card ct-freeze-card">
                  <div className="ct-card-head">
                    <b>冻结窗口</b>
                    <span>横轴 0–{tight(scaleMax)} 小时 · 估计</span>
                  </div>
                  <div className="ct-axis">
                    <span className="ct-axis-gutter" />
                    <span className="ct-axis-track">
                      <i>0</i>
                      <i>{tight(scaleMax / 2)} h</i>
                      <i>{tight(scaleMax)} h</i>
                    </span>
                    <span className="ct-axis-gutter" />
                  </div>
                  <ul className="ct-freeze-list">
                    {offs.map((item) => {
                      const hours = toNum(item.ctrl.freeze_window_hours);
                      const pct = (hours / scaleMax) * 100;
                      const color = (LEVEL_META[item.level] || LEVEL_META.RED).color;
                      const wide = pct >= 34;
                      return (
                        <li key={item.node_id}>
                          <button
                            type="button"
                            className="ct-freeze-row"
                            aria-pressed={active && active.node_id === item.node_id}
                            onClick={() => select(item)}
                          >
                            <span className="ct-freeze-name">{item.label || item.node_id}</span>
                            <span className="ct-track">
                              <i
                                className="ct-bar"
                                style={{ width: `${Math.max(pct, 2)}%`, background: color }}
                              >
                                {wide && <em className="ct-bar-est">估计</em>}
                              </i>
                            </span>
                            <span className="ct-freeze-val">
                              {tight(hours)} h{wide ? '' : ' · 估计'}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                  <div className="ct-constraints">
                    <h5>主要约束</h5>
                    {(active?.ctrl.main_constraints || []).length > 0 ? (
                      <ul>
                        {(active.ctrl.main_constraints || []).map((text, index) => (
                          <li key={`${text}-${index}`}>{text}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="ct-note">该出口未列出主要约束。</p>
                    )}
                    <p className="ct-note">
                      冻结窗口为估计值，实际以协作方反馈与文书送达时间为准。
                    </p>
                  </div>
                </div>
              </div>
            </div>

            <aside className="mod-side ct-side">
              {active ? (
                <>
                  <div className="side-block ct-picked">
                    <h4>
                      选中出口
                      <span className="ct-level-chip" style={{ color: levelColor, borderColor: `${levelColor}33` }}>
                        <i style={{ background: levelColor }} />
                        {active.level} · {(LEVEL_META[active.level] || LEVEL_META.RED).name}
                      </span>
                    </h4>
                    <div className="ct-picked-name">{active.label || active.node_id}</div>
                    <code className="ct-picked-id">{active.node_id}</code>
                    <div className="ct-picked-score">
                      <b>{score1(sCtrl)}</b>
                      <small>S_ctrl · 截断到 [0,100]</small>
                    </div>
                  </div>

                  <div className="side-block ct-eqblock">
                    <h4>
                      <span>评分复算</span>
                      <em>可手算核对</em>
                    </h4>
                    {hasWeights ? (
                      <>
                        <div className="ct-eq">
                          <span>
                            {alpha.toFixed(2)}×{tight(sProfile)}
                          </span>
                          <i>+</i>
                          <span>
                            {beta.toFixed(2)}×{tight(sOff)}
                          </span>
                          <i>+</i>
                          <span>
                            {gamma.toFixed(2)}×{tight(conf)}
                          </span>
                          <i>−</i>
                          <span>{tight(pen)}</span>
                          <i>=</i>
                          <b>{score1(clamped)}</b>
                        </div>
                        <p className="ct-src">
                          α β γ 来自 <code>result.parameters.controllability</code>
                          <br />
                          P 来自 <code>{penSource}</code>
                          {penConflict ? '（结果字段 p 与参数表不一致，此处按参数表计算）' : ''}
                          <br />
                          S_off 按 <code>min({tight(cap)}, Σ 命中)</code>
                          {sOffFromCalc ? ' 现算（与「法币出口」页同口径）' : ' 取结果字段'}
                          {sOffConflict ? '；结果字段 ctrl.s_off 与现算值不一致，此处按现算值' : ''}
                        </p>
                        <ul className="ct-terms">
                          <li>
                            <span>α · 画像分</span>
                            <em>
                              {alpha.toFixed(2)} × {tight(sProfile)}
                            </em>
                            <b>{r2(alpha * sProfile)}</b>
                          </li>
                          <li>
                            <span>β · 出口分</span>
                            <em>
                              {beta.toFixed(2)} × {tight(sOff)}
                            </em>
                            <b>{r2(beta * sOff)}</b>
                          </li>
                          <li>
                            <span>γ · 置信度 c</span>
                            <em>
                              {gamma.toFixed(2)} × {tight(conf)}
                            </em>
                            <b>{r2(gamma * conf)}</b>
                          </li>
                          <li className="is-minus">
                            <span>风险惩罚 P</span>
                            <em>− {tight(pen)}</em>
                            <b>−{r2(pen)}</b>
                          </li>
                          <li className="is-total">
                            <span>合计并截断</span>
                            <em>clamp(0, 100)</em>
                            <b>{score1(clamped)}</b>
                          </li>
                        </ul>
                        {mismatch && (
                          <p className="ct-warn">
                            <TriangleAlert size={13} />
                            复算 {score1(clamped)} 与结果字段 s_ctrl {score1(sCtrl)} 不一致
                          </p>
                        )}
                      </>
                    ) : (
                      <p className="ct-note">
                        结果未提供 α/β/γ（parameters.controllability），无法复算；仅显示结果字段 s_ctrl{' '}
                        {score1(sCtrl)}。
                      </p>
                    )}
                  </div>

                  <div className="side-block ct-caliber">
                    <h4>口径</h4>
                    <ul>
                      <li>c（数据置信度）：画像命中取 100，未命中取 55。</li>
                      <li>
                        P（风险惩罚）按风险档位：high {tight(penalty.high)} / medium {tight(penalty.medium)} / low{' '}
                        {tight(penalty.low)}
                        {nodeRisk ? `；本出口档位 ${nodeRisk}` : ''}。
                      </li>
                      <li>
                        信号灯阈值：GREEN ≥ {tight(greenTh)}，YELLOW {tight(yellowTh)}–{tight(greenTh - 1)}，RED &lt;{' '}
                        {tight(yellowTh)}。
                      </li>
                      {!datasetCase && <li>未找到对应数据集，风险档位仅取自结果字段。</li>}
                    </ul>
                  </div>

                  <div className="side-block ct-facts">
                    <h4>出口资金</h4>
                    <dl>
                      <div>
                        <dt>总入金</dt>
                        <dd>
                          {active.inflow_total ?? '—'} {asset}
                        </dd>
                      </div>
                      <div>
                        <dt>来自混币</dt>
                        <dd>
                          {active.from_mixer ?? '—'} {asset}
                        </dd>
                      </div>
                      <div>
                        <dt>入金笔数</dt>
                        <dd>{Math.round(toNum(active.deposit_count))} 笔</dd>
                      </div>
                      <div>
                        <dt>最高归因分</dt>
                        <dd>{score1(active.best_link_score)}</dd>
                      </div>
                    </dl>
                  </div>
                </>
              ) : (
                <div className="mod-empty">
                  <Gauge size={26} strokeWidth={1.3} />
                  <strong>该案件没有可控性评分</strong>
                  <span>换一个虚拟案件，或重新执行混币识别。</span>
                </div>
              )}
            </aside>
          </div>
        </>
      ) : (
        <div className="mod-empty">
          <Gauge size={30} strokeWidth={1.3} />
          <strong>还没有可控性评分</strong>
          <span>还没有混币识别结果。点击顶部「混币识别」后，这里显示评分拆解与信号灯。</span>
        </div>
      )}

      <footer className="mod-foot">
        <i />
        本页全部数据为虚构模拟数据，仅用于展示原型能力；混币—法币对应关系为算法候选，不表示资金转移已确认。
      </footer>
    </section>
  );
}

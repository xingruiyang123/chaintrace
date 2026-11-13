import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Landmark, TriangleAlert, Layers } from 'lucide-react';
import './modules.css';
import './OfframpBoard.css';

/**
 * 法币出口识别（.ob-）
 * props: { result, dataset, caseId, onPick }
 * 数据契约：docs/接口契约_混币法币识别_v0_1.md §3.4（offramps[]）
 * 视觉基线：docs/新增可视化模块界面基线_v0_1.md（§2 令牌 / §3 骨架 / §6 堆叠条）
 *
 * 口径：S_off = min(cap, Σ hits[].score)，cap 取自 result.parameters.offramp.cap。
 * 消融视图：对每条命中算「去掉它之后」的 min(cap, Σ其余)，用来看分数是否被单一规则支配。
 * 分值显示：汇总分（s_off / 归因分）保留 1 位小数；规则分本身是整数，按精确值参与算式，不补 .0。
 */

const TYPE_LABEL = {
  exchange: '交易所',
  otc: '场外承兑',
  payment_gateway: '支付网关',
  underground_bank: '地下钱庄',
  dex: '去中心化交易所',
  bridge: '跨链桥',
};
const TYPE_KEYS = ['exchange', 'otc', 'payment_gateway', 'underground_bank', 'dex', 'bridge'];

const toNum = (value, fallback = 0) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};
/** 汇总分：1 位小数 */
const score1 = (value) => toNum(value).toFixed(1);
/** 精确值：去掉浮点噪声与多余的 .0，供算式逐项核对 */
const exact = (value) => String(Number(toNum(value).toFixed(2)));
const countText = (value) => String(Math.round(toNum(value)));
/** 差值：整数就写整数（与「分值保留 1 位小数」区分，便于直接对上规则分）；非整数才留 1 位 */
const deltaText = (value) => {
  const n = Math.round(toNum(value) * 100) / 100;
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
};

/** 段色：按命中顺序从深到浅的灰阶（不同灰阶 / 透明度，不引入新色系） */
const toneAt = (index, total) => {
  const t = total <= 1 ? 0 : index / (total - 1);
  return `hsl(212 15% ${Math.round(30 + t * 44)}%)`;
};

export default function OfframpBoard({ result, dataset, caseId, onPick, focus }) {
  const [pickedId, setPickedId] = useState(null);
  const cap = toNum(result?.parameters?.offramp?.cap, 100);

  const currentCase = useMemo(() => {
    const cases = result?.cases || [];
    return cases.find((c) => c.case_id === caseId) || cases[0] || null;
  }, [result, caseId]);

  const asset = useMemo(() => {
    const found = (dataset?.cases || []).find((c) => c.case_id === currentCase?.case_id);
    return found?.asset || currentCase?.network || '';
  }, [dataset, currentCase]);

  const offs = useMemo(() => {
    const list = (currentCase?.offramps || []).map((item) => {
      const hits = Array.isArray(item.hits) ? item.hits : [];
      const sum = hits.reduce((acc, hit) => acc + Math.max(0, toNum(hit.score)), 0);
      let cursor = 0;
      const segments = hits.map((hit, index) => {
        const score = Math.max(0, toNum(hit.score));
        const room = Math.max(0, cap - cursor);
        const shown = Math.min(score, room);
        cursor += score;
        return { index, hit, score, shown, tone: toneAt(index, hits.length) };
      });
      return {
        ...item,
        hits,
        segments,
        sum,
        truncated: sum > cap + 1e-9,
        recomputed: Math.min(cap, sum),
      };
    });
    return list.sort((a, b) => toNum(b.s_off) - toNum(a.s_off));
  }, [currentCase, cap]);

  const active = offs.find((item) => item.node_id === pickedId) || offs[0] || null;
  const totalHits = offs.reduce((acc, item) => acc + item.hits.length, 0);
  const maxScore = offs.reduce((acc, item) => Math.max(acc, toNum(item.s_off)), 0);
  const cutCount = offs.filter((item) => item.truncated).length;

  // 跨页下钻：App 传来 focus 时把右栏切到被点的那个出口，并把该行滚进可视区。
  // focus 为 null / type 不是 offramp / id 不在本案件里时，什么都不做（默认选中第一项不变）。
  const listRef = useRef(null);
  const focusId = focus && focus.type === 'offramp' ? focus.id : null;
  useEffect(() => {
    if (!focusId) return;
    const hit = offs.find((item) => item.node_id === focusId);
    if (!hit) return;
    setPickedId(hit.node_id);
    const rows = listRef.current ? listRef.current.querySelectorAll('.ob-row') : [];
    const row = Array.from(rows).find((node) => node.getAttribute('data-node-id') === hit.node_id);
    if (row) row.scrollIntoView({ block: 'center' });
  }, [focus, focusId, offs]);

  const dist = useMemo(
    () => TYPE_KEYS.map((key) => ({
      type: key,
      label: TYPE_LABEL[key],
      count: offs.filter((item) => item.offramp_type === key).length,
    })),
    [offs]
  );

  function select(item) {
    setPickedId(item.node_id);
    if (onPick) onPick({ type: 'offramp', id: item.node_id });
  }

  const ready = offs.length > 0;

  // 规则消融：去掉第 k 条后 S_off' = min(cap, Σ 其余命中)，按影响从大到小排（掉得最多的在前）
  const ablation = useMemo(() => {
    if (!active) return [];
    return active.hits
      .map((hit, index) => {
        const score = Math.max(0, toNum(hit.score));
        const without = Math.min(cap, Math.max(0, active.sum - score));
        return {
          index,
          hit,
          score,
          without,
          delta: without - active.recomputed,
          tone: toneAt(index, active.hits.length),
        };
      })
      .sort((a, b) => a.delta - b.delta || b.score - a.score);
  }, [active, cap]);

  const ratioText = (() => {
    if (!active) return '—';
    const total = toNum(active.inflow_total);
    if (total <= 0) return '—';
    return `${((toNum(active.from_mixer) / total) * 100).toFixed(1)}%`;
  })();

  return (
    <section className="mod ob">
      <header className="mod-head">
        <div>
          <span className="mod-eyebrow">追缴空间 · 法币出口</span>
          <h1>出口排序与分值复算</h1>
          <p className="mod-sub">
            每条出口把规则库命中分值累加成 S_off。左边看分值由哪几段拼成，右边看逐条命中依据与复算式。
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
              <small>疑似出口</small>
              <b>{offs.length}</b>
              <span>个节点</span>
            </div>
            <div className="kpi">
              <small>命中规则</small>
              <b>{totalHits}</b>
              <span>条</span>
            </div>
            <div className="kpi">
              <small>最高 S_off</small>
              <b>{score1(maxScore)}</b>
            </div>
            <div
              className="kpi ob-kpi-cut"
              title={`单节点命中规则合计超过 ${exact(cap)} 时按 ${exact(cap)} 截断：S_off = min(cap, Σ)，这是申报书里 S_off 的定义。`}
            >
              <small>S_off 截断</small>
              {cutCount > 0 ? (
                <>
                  <b>{cutCount}</b>
                  <span>个已截断 · 上限 {exact(cap)}</span>
                </>
              ) : (
                <>
                  <b className="ob-kpi-plain">无截断</b>
                  <span>上限 {exact(cap)}</span>
                </>
              )}
            </div>
          </div>

          <div className="mod-body">
            <div className="mod-main ob-main">
              <div className="ob-caption">
                <span>
                  <b>出口排行</b>
                  <em>按 S_off 降序 · 每段 = 一条命中规则</em>
                </span>
                <span
                  className="ob-cap"
                  title={`上限 ${exact(cap)}：单节点命中规则合计超过 ${exact(cap)} 时按 ${exact(cap)} 截断（S_off = min(cap, Σ)）。`}
                >
                  cap = {exact(cap)}
                </span>
              </div>

              <div className="ob-list" ref={listRef}>
                {offs.map((item) => {
                  const isActive = active && active.node_id === item.node_id;
                  return (
                    <button
                      type="button"
                      key={item.node_id}
                      className="ob-row"
                      data-node-id={item.node_id}
                      aria-pressed={isActive}
                      onClick={() => select(item)}
                    >
                      <span className="ob-row-id">
                        <span className="ob-type">{TYPE_LABEL[item.offramp_type] || item.offramp_type || '未标注'}</span>
                        <b>{item.label || item.node_id}</b>
                        <code>{item.node_id}</code>
                      </span>

                      <span className="ob-row-mid">
                        <span className="ob-bar" role="img" aria-label={`${item.label || item.node_id} 的 S_off 构成：${item.hits.length} 条规则`}>
                          {item.segments.map((seg) =>
                            seg.shown > 0 ? (
                              <i
                                key={`${seg.hit.rule_id}-${seg.index}`}
                                style={{ width: `${(seg.shown / cap) * 100}%`, background: seg.tone }}
                                title={`${seg.hit.rule_id} ${seg.hit.label} +${exact(seg.score)}`}
                              />
                            ) : null
                          )}
                        </span>
                        <span className="ob-meta">
                          <span className="ob-money">
                            混币转入 <b>{item.from_mixer ?? '—'}</b> / 总入金 <b>{item.inflow_total ?? '—'}</b>
                            {asset ? <em>{asset}</em> : null}
                          </span>
                          <span>入金 {countText(item.deposit_count)} 笔</span>
                          {item.truncated && (
                            <em className="ob-cut">已截断 +{score1(item.sum - cap)}</em>
                          )}
                        </span>
                      </span>

                      <span className="ob-row-score">
                        <b>{score1(item.s_off)}</b>
                        <small>S_off</small>
                      </span>
                    </button>
                  );
                })}
              </div>

              <div className="ob-dist">
                <div className="ob-dist-head">
                  <b>类型分布</b>
                  <span>按 offramp_type 计数</span>
                </div>
                <div className="ob-dist-bar" aria-hidden="true">
                  {dist
                    .filter((item) => item.count > 0)
                    .map((item) => (
                      <i
                        key={item.type}
                        className={`ob-t-${item.type}`}
                        style={{ flexGrow: item.count }}
                        title={`${item.label} ${item.count} 个`}
                      />
                    ))}
                </div>
                <div className="ob-dist-chips">
                  {dist.map((item) => (
                    <span key={item.type} className={'ob-dist-chip' + (item.count ? '' : ' is-zero')}>
                      <i className={`ob-dot ob-t-${item.type}`} />
                      {item.label}
                      <b>{item.count}</b>
                    </span>
                  ))}
                </div>
              </div>
            </div>

            <aside className="mod-side ob-side">
              {active ? (
                <>
                  <div className="side-block ob-summary">
                    <h4>
                      选中出口
                      <span className="ob-type">{TYPE_LABEL[active.offramp_type] || active.offramp_type || '未标注'}</span>
                    </h4>
                    <div className="ob-sum-name">{active.label || active.node_id}</div>
                    <code className="ob-sum-id">{active.node_id}</code>
                    <div className="ob-sum-score">
                      <b>{score1(active.s_off)}</b>
                      <small>S_off = min(cap, Σ 命中)</small>
                    </div>
                  </div>

                  <div className="side-block ob-hits">
                    <h4>
                      <span>规则命中</span>
                      <em>{active.hits.length} 条</em>
                    </h4>
                    {active.hits.length > 0 ? (
                      <ul>
                        {active.hits.map((hit, index) => (
                          <li key={`${hit.rule_id}-${index}`} className="ob-hit">
                            <i className="ob-hit-tone" style={{ background: toneAt(index, active.hits.length) }} />
                            <div className="ob-hit-body">
                              <div className="ob-hit-top">
                                <code>{hit.rule_id}</code>
                                <b>{exact(hit.score)}</b>
                              </div>
                              <span className="ob-hit-name">{hit.label}</span>
                              <p>{hit.explain}</p>
                            </div>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="ob-note">该出口没有命中规则，无法复算分值。</p>
                    )}
                  </div>

                  <div className="side-block ob-calc">
                    <h4>分值复算</h4>
                    {active.hits.length > 0 ? (
                      <>
                        <p className="ob-eq">
                          Σ 命中 = {active.hits.map((hit) => exact(hit.score)).join(' + ')} = {exact(active.sum)}
                        </p>
                        <p className="ob-eq is-total">
                          S_off = min({exact(cap)}, {exact(active.sum)}) = {score1(active.recomputed)}
                        </p>
                        <span className="ob-bar-legend">
                          {active.segments
                            .filter((seg) => seg.shown > 0)
                            .map((seg) => (
                              <span key={`legend-${seg.hit.rule_id}-${seg.index}`}>
                                <i style={{ background: seg.tone }} />
                                {seg.hit.rule_id} +{exact(seg.score)}
                              </span>
                            ))}
                          {active.truncated && <em>已截断 +{score1(active.sum - cap)}</em>}
                        </span>
                        {Math.abs(active.recomputed - toNum(active.s_off)) > 0.05 && (
                          <p className="ob-warn">
                            <TriangleAlert size={13} />
                            复算 {score1(active.recomputed)} 与结果字段 {score1(active.s_off)} 不一致
                          </p>
                        )}
                      </>
                    ) : (
                      <p className="ob-note">hits 为空，无法复算。</p>
                    )}
                  </div>

                  <div className="side-block ob-ablate">
                    <h4>
                      <span>消融：去掉一条规则会怎样</span>
                      <em>
                        原分 {score1(active.recomputed)} · {ablation.length} 条
                      </em>
                    </h4>
                    {active.hits.length === 0 ? (
                      <p className="ob-note">该出口没有命中规则，无法做消融比较。</p>
                    ) : active.hits.length === 1 ? (
                      <p className="ob-ablate-single">
                        仅命中 1 条规则，无法做消融比较；该出口的证据强度应视为单点。
                      </p>
                    ) : (
                      <>
                        <ul className="ob-ablate-list">
                          {ablation.map((row, order) => {
                            const basePct = (active.recomputed / cap) * 100;
                            const restPct = (row.without / cap) * 100;
                            const top = order === 0 && row.delta < -0.05;
                            return (
                              <li
                                key={`ab-${row.hit.rule_id}-${row.index}`}
                                className={top ? 'is-leading' : ''}
                              >
                                <div className="ob-ablate-top">
                                  <code>{row.hit.rule_id}</code>
                                  <span className="ob-ablate-idx">命中序 #{row.index + 1}</span>
                                  <b className={row.delta < -0.05 ? 'is-drop' : ''}>
                                    {score1(active.recomputed)} → {score1(row.without)}
                                    <em>
                                      （
                                      {row.delta < -0.05
                                        ? `−${deltaText(Math.abs(row.delta))}`
                                        : '不变'}
                                      ）
                                    </em>
                                  </b>
                                </div>
                                <span
                                  className="ob-ablate-track"
                                  role="img"
                                  aria-label={`原分 ${score1(active.recomputed)}，去掉该条后 ${score1(row.without)}`}
                                >
                                  <i
                                    className="ob-ablate-ghost"
                                    style={{ width: `${Math.max(basePct, 1.5)}%` }}
                                  />
                                  <i className="ob-ablate-rest" style={{ width: `${restPct}%` }} />
                                  <i
                                    className="ob-ablate-gap"
                                    style={{
                                      left: `${restPct}%`,
                                      width: `${Math.max(0, basePct - restPct)}%`,
                                    }}
                                  />
                                </span>
                                <span className="ob-ablate-legend">
                                  <span>
                                    <i className="ob-ablate-key is-gap" />
                                    该条撑起 +{deltaText(Math.max(0, active.recomputed - row.without))}
                                  </span>
                                  <span>
                                    <i className="ob-ablate-key is-rest" />
                                    其余 {score1(row.without)}
                                  </span>
                                </span>
                              </li>
                            );
                          })}
                        </ul>
                        <p className="ob-ablate-note">
                          消融用于检验分数是否被单一规则支配；某条规则一去掉分数断崖式下跌，说明该出口的判定证据单薄。
                        </p>
                      </>
                    )}
                  </div>

                  <div className="side-block ob-facts">
                    <h4>资金与归因</h4>
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
                        <dt>混币占比</dt>
                        <dd>{ratioText}</dd>
                      </div>
                      <div>
                        <dt>入金笔数</dt>
                        <dd>{countText(active.deposit_count)} 笔</dd>
                      </div>
                      <div>
                        <dt>最高归因分</dt>
                        <dd>{score1(active.best_link_score)}</dd>
                      </div>
                    </dl>
                    <p className="ob-note">归因分来自断链归因候选，只表示候选强度，不表示资金转移已确认。</p>
                  </div>
                </>
              ) : (
                <div className="mod-empty">
                  <Layers size={26} strokeWidth={1.3} />
                  <strong>该案件没有出口</strong>
                  <span>换一个虚拟案件，或重新执行混币识别。</span>
                </div>
              )}
            </aside>
          </div>
        </>
      ) : (
        <div className="mod-empty">
          <Landmark size={30} strokeWidth={1.3} />
          <strong>还没有出口识别结果</strong>
          <span>还没有混币识别结果。点击顶部「混币识别」后，这里列出疑似法币出口与分值构成。</span>
        </div>
      )}

      <footer className="mod-foot">
        <i />
        本页全部数据为虚构模拟数据，仅用于展示原型能力；混币—法币对应关系为算法候选，不表示资金转移已确认。
      </footer>
    </section>
  );
}

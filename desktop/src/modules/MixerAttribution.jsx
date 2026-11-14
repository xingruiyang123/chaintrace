import React, { useEffect, useMemo, useState } from 'react';
import { Check, ChevronDown, ChevronRight, GitFork, Layers, List, Minus } from 'lucide-react';
import { effectiveTierOf, identifiabilityLabel, identifiabilityNote, isConditional } from '../analysis.js';
import {
  stateInfoOf, identifiabilityCounts, STATE_ORDER, STATE_TONE, IDENTIFIABILITY_CN,
  REASON_CODE_CN, missingFieldsOf,
} from '../caseDecision.js';
import './modules.css';
import './MixerAttribution.css';

/**
 * 混币—法币断链归因（.ma-）
 * props: { result, dataset, caseId, onPick, verification }
 * 数据契约：docs/接口契约_混币法币识别_v0_1.md §3.2 mixer_pools[] / §3.3 links[] / §3.6 metrics
 * 视觉基线：docs/新增可视化模块界面基线_v0_1.md（§2 状态色 / §3 骨架 / §6 配对矩阵）
 *
 * 口径：
 * - 分数是「断链归因候选」的匹配强度，不表示资金转移已确认。
 * - 矩阵里空格 = 该（入金, 出金）组合没有进入候选集，不是 0 分。
 * - 同一行内分数相同（差 < 0.005）的候选互称「同分并列」；并列时 rank 会重复，必须显式标出，
 *   否则三个 #1 会被读成排版错误。KPI 用 summary.distinct_rank1（首位无并列的入金笔数）而不是 HIGH 条数，
 *   因为 HIGH 计数会被并列虚高。
 * - 色阶 5 档与 parameters.attribution.tier 联动：LOW=灰 / MEDIUM=蓝（浅→深）/ HIGH=绿（浅→深），
 *   同一 tier 内只做明度分层，不跨色相（琥珀留给信号灯 YELLOW / 待复核）。
 * - p@1 / p@3 / mrr 只与「合成数据 · 竞争条件」同屏出现，不写成识别准确率。
 */

/** 同分判定容差：score 保留 2 位小数，差小于 0.005 视为同一分数 */
const TIE_EPS = 0.005;

const FOOT_TEXT =
  '本页全部数据为虚构模拟数据，仅用于展示原型能力；混币—法币的对应关系为算法候选，不表示资金转移已确认。';

/** 六维特征固定顺序（契约 §3.3 features） */
const FEATURES = [
  { key: 'amount', label: '金额吻合' },
  { key: 'time', label: '时间贴近' },
  { key: 'denomination', label: '面额一致' },
  { key: 'path_rarity', label: '路径稀有' },
  { key: 'cooccurrence', label: '共现关联' },
  { key: 'fee_fit', label: '手续费吻合' },
];

const MECHANISM = {
  coinjoin: 'CoinJoin 协作混合',
  equal_output: '等额输出拆分',
  tornado_like: 'Tornado 式固定面额',
  unknown: '机制未判定',
};

const TIERS = ['HIGH', 'MEDIUM', 'LOW'];

const isNum = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));

/** 汇总分：固定 1 位小数 */
const dec = (value, digits = 1) => (isNum(value) ? Number(value).toFixed(digits) : '—');
/** 精确值：去浮点噪声、去多余的 .0，供算式逐项核对 */
const clean = (value) => (isNum(value) ? String(Number(Number(value).toFixed(3))) : '—');
/** 整数：计数与判别分 */
const whole = (value) => (isNum(value) ? String(Math.round(Number(value))) : '—');

/** 金额只截尾零，不做四舍五入；完整原串仍留在 title 里 */
function trimAmount(value) {
  if (typeof value !== 'string' || !value) return '';
  if (!value.includes('.')) return value;
  const trimmed = value.replace(/0+$/, '').replace(/\.$/, '');
  return trimmed || '0';
}

/** ISO-8601 → 08-05 02:05（分钟精度；完整时间留在 title 里） */
function fmtTime(iso) {
  if (typeof iso !== 'string' || iso.length < 16) return '';
  return iso.slice(5, 16).replace('T', ' ');
}

function fmtDay(iso) {
  if (typeof iso !== 'string' || iso.length < 10) return '';
  return iso.slice(5, 10);
}

/**
 * 色阶边界全部来自 result.parameters.attribution.tier：
 * [LOW] / [MEDIUM 下段] / [MEDIUM 上段] / [HIGH 下段] / [HIGH 上段]
 */
function makeBounds(params) {
  const tier = params?.attribution?.tier || {};
  const medium = isNum(tier.medium) ? Number(tier.medium) : 45;
  const high = isNum(tier.high) ? Number(tier.high) : 70;
  const midMedium = (medium + high) / 2;
  const midHigh = (high + 100) / 2;
  return {
    medium,
    high,
    midMedium,
    midHigh,
    steps: [
      { key: 'b1', label: `< ${clean(medium)}`, hint: `低于 ${clean(medium)}：弱候选，需人工判断` },
      { key: 'b2', label: `${clean(medium)}–${clean(midMedium)}`, hint: `中档下段：${clean(medium)} 以上` },
      { key: 'b3', label: `${clean(midMedium)}–${clean(high)}`, hint: `中档上段：接近 ${clean(high)}` },
      { key: 'b4', label: `${clean(high)}–${clean(midHigh)}`, hint: `高档下段：达到 ${clean(high)}` },
      { key: 'b5', label: `≥ ${clean(midHigh)}`, hint: `高档上段：多维度同时吻合` },
    ],
  };
}

function toneOf(score, bounds) {
  const s = Number(score);
  if (!Number.isFinite(s) || s < bounds.medium) return 'b1';
  if (s < bounds.midMedium) return 'b2';
  if (s < bounds.high) return 'b3';
  if (s < bounds.midHigh) return 'b4';
  return 'b5';
}

function tierOf(score, bounds) {
  const s = Number(score);
  if (!Number.isFinite(s) || s < bounds.medium) return 'LOW';
  return s >= bounds.high ? 'HIGH' : 'MEDIUM';
}

export default function MixerAttribution({ result, dataset, caseId, onPick, verification, focus }) {
  const cases = Array.isArray(result?.cases) ? result.cases : [];
  const current = cases.find((item) => item.case_id === caseId) || cases[0] || null;
  const caseKey = current?.case_id || '';

  const [view, setView] = useState('matrix');
  const [poolsOpen, setPoolsOpen] = useState(true);
  const [selection, setSelection] = useState(null);

  // 切换案件时清空选择，避免右侧栏指向别的案件
  useEffect(() => {
    setSelection(null);
  }, [caseKey]);

  const params = result?.parameters || {};
  const bounds = useMemo(() => makeBounds(params), [params]);
  const pools = Array.isArray(current?.mixer_pools) ? current.mixer_pools : [];
  const links = Array.isArray(current?.links) ? current.links : [];
  const metrics = current?.metrics || {};

  // 跨页下钻落点：App 在别的页面收到 onPick 后会把 { type, id } 通过 focus 传进来。
  // 这里只把 focus 翻译成现有的 selection 状态（池 → 池明细，link → link 明细），不新造状态；
  // focus 为 null（没有跨页跳转）时行为完全不变。找不到对应 id 就忽略，不抛错。
  useEffect(() => {
    if (!focus || !focus.type || !focus.id) return;
    if (focus.type === 'pool' && pools.some((pool) => pool.pool_id === focus.id)) {
      setSelection({ kind: 'pool', id: focus.id });
    } else if (focus.type === 'link' && links.some((link) => link.link_id === focus.id)) {
      setSelection({ kind: 'link', id: focus.id });
    }
  }, [focus, pools, links]);

  const dsCase = useMemo(
    () => (dataset?.cases || []).find((item) => item.case_id === caseKey) || null,
    [dataset, caseKey]
  );
  const edgeMap = useMemo(() => {
    const map = new Map();
    (dsCase?.edges || []).forEach((edge) => {
      if (edge?.id) map.set(edge.id, edge);
    });
    return map;
  }, [dsCase]);
  const nodeMap = useMemo(() => {
    const map = new Map();
    (dsCase?.nodes || []).forEach((node) => {
      if (node?.id) map.set(node.id, node);
    });
    return map;
  }, [dsCase]);

  const asset = dsCase?.asset || current?.network || '';

  const poolById = useMemo(() => {
    const map = new Map();
    pools.forEach((pool) => map.set(pool.pool_id, pool));
    return map;
  }, [pools]);

  const stats = useMemo(() => {
    // v0.2.1：档位以可识别性判定为准，WITHHELD 单列一档（整改 F-01）
    const tiers = { HIGH: [], MEDIUM: [], LOW: [], WITHHELD: [] };
    links.forEach((link) => {
      const tier = effectiveTierOf(link);
      (tiers[tier] || tiers.LOW).push(link);
    });
    const maxScore = links.reduce((acc, link) => Math.max(acc, Number(link.score) || 0), -Infinity);
    const maxDetect = pools.reduce((acc, pool) => Math.max(acc, Number(pool.detection_score) || 0), -Infinity);
    // 入金笔数：全案各池入金边去重后的条数，作为「首位无并列 x / y」的分母
    const depositKeys = new Set();
    pools.forEach((pool) => (pool.deposit_edges || []).forEach((id) => depositKeys.add(`${pool.pool_id}|${id}`)));
    const depositCount = depositKeys.size;
    const declaredDistinct = Number(current?.summary?.distinct_rank1);
    // 最高分并列条数：与最高分差 < TIE_EPS 的候选数（全案口径），用来提示「最高分不是唯一解」
    const topScore = Number.isFinite(maxScore) ? maxScore : null;
    const topTieCount =
      topScore === null ? 0 : links.filter((link) => Math.abs(Number(link.score) - topScore) < TIE_EPS).length;
    return {
      tiers,
      high: tiers.HIGH.length,
      maxScore: Number.isFinite(maxScore) ? maxScore : null,
      maxDetect: Number.isFinite(maxDetect) ? maxDetect : null,
      depositCount,
      distinctRank1: Number.isFinite(declaredDistinct) ? declaredDistinct : null,
      topTieCount,
    };
  }, [links, pools, bounds, current]);

  /**
   * 同一行（同一笔入金）内同分的候选互称并列：判定只用分数，不看 link_id。
   * 返回 Map<deposit_edge, { count, topCount }>，count 为该行候选总数，topCount 为该行最高分的并列数。
   */
  const tieIndex = useMemo(() => {
    const map = new Map();
    pools.forEach((pool) => {
      const poolLinks = links.filter((link) => link.pool_id === pool.pool_id);
      const rows = Array.isArray(pool.deposit_edges) ? pool.deposit_edges : [];
      rows.forEach((row) => {
        const rowLinks = poolLinks.filter((link) => link.deposit_edge === row);
        const best = rowLinks.reduce((acc, link) => Math.max(acc, Number(link.score) || 0), -Infinity);
        map.set(`${pool.pool_id}|${row}`, {
          count: rowLinks.length,
          best: Number.isFinite(best) ? best : null,
          topCount: rowLinks.filter((link) => Number.isFinite(best) && Math.abs(Number(link.score) - best) < TIE_EPS).length,
        });
      });
    });
    return map;
  }, [pools, links]);

  /** 某条 link 在同分意义上的并列条数（含自己） */
  function tieCountOf(link) {
    const rowKey = `${link.pool_id}|${link.deposit_edge}`;
    const poolLinks = links.filter((item) => item.pool_id === link.pool_id && item.deposit_edge === link.deposit_edge);
    const n = poolLinks.filter((item) => Math.abs(Number(item.score) - Number(link.score)) < TIE_EPS).length;
    // 行不存在于池的 deposit_edges 时，退化为按全池同分计
    return tieIndex.has(rowKey) ? n : Math.max(1, n);
  }

  function tieLabel(link) {
    const n = tieCountOf(link);
    return n > 1 ? `并列×${n}` : '';
  }

  function pickLink(link) {
    setSelection({ kind: 'link', id: link.link_id });
    if (typeof onPick === 'function') onPick({ type: 'link', id: link.link_id });
  }

  // 池卡是本页内的动作（展开该池的入/出金边与池内候选），不再抛 onPick：
  // 从池卡「跳到本页」等于自己跳自己，App 那边只会把页面重设成 mixer，没有意义。
  // 跨页进入本页的池选中由上面的 focus effect 负责。
  function pickPool(pool) {
    setSelection({ kind: 'pool', id: pool.pool_id });
    setPoolsOpen(true);
  }

  function edgeTitle(id, edge) {
    if (!edge) return `${id}：输入数据集里没有这条边的明细`;
    const from = nodeMap.get(edge.from)?.label || edge.from;
    const to = nodeMap.get(edge.to)?.label || edge.to;
    return `${id} · ${from} → ${to} · ${edge.amount} ${edge.asset || asset} · ${edge.time} · ${edge.pattern || ''}`;
  }

  const head = (
    <header className="mod-head">
      <div>
        <span className="mod-eyebrow">追缴空间 · 混币归因</span>
        <h1>入金 × 出金配对归因</h1>
        <p className="mod-sub">
          {dsCase?.title || '把混币池的入金与出金重新配对，按六维特征给出归因分，再追到可拦截的法币出口。'}
        </p>
      </div>
      <div className="mod-meta">
        <span className="sim-chip">虚拟数据 · 仅演示</span>
        {current && (
          <span className="mod-case">
            案件 {current.case_id} · {current.network}
          </span>
        )}
        {dsCase?.window && (
          <span className="mod-source">
            观测窗 {fmtDay(dsCase.window.from)} → {fmtDay(dsCase.window.to)}
          </span>
        )}
      </div>
    </header>
  );

  if (!current) {
    return (
      <section className="mod ma">
        {head}
        <div className="mod-empty">
          <GitFork size={30} strokeWidth={1.3} />
          <strong>还没有混币识别结果</strong>
          <span>点击顶部「载入混币样例」后执行识别。</span>
        </div>
        <footer className="mod-foot">
          <i />
          {FOOT_TEXT}
        </footer>
      </section>
    );
  }

  /* ---------------------------------------------------------------- 矩阵视图 */

  function renderPoolMatrix(pool) {
    const rows = Array.isArray(pool.deposit_edges) ? pool.deposit_edges : [];
    const cols = Array.isArray(pool.withdrawal_edges) ? pool.withdrawal_edges : [];
    const poolLinks = links.filter((link) => link.pool_id === pool.pool_id);
    const pairMap = new Map(poolLinks.map((link) => [`${link.deposit_edge}|${link.withdrawal_edge}`, link]));
    const rowBest = new Map();
    rows.forEach((row) => {
      const candidates = poolLinks.filter((link) => link.deposit_edge === row);
      const best = candidates.reduce((acc, link) => (!acc || Number(link.score) > Number(acc.score) ? link : acc), null);
      const tieN = best
        ? candidates.filter((link) => Math.abs(Number(link.score) - Number(best.score)) < TIE_EPS).length
        : 0;
      rowBest.set(row, best ? { ...best, tieN } : null);
    });

    return (
      <section className="ma-poolmatrix" key={pool.pool_id || pool.label}>
        <header className="ma-pm-head">
          <div className="ma-pm-title">
            <b>{pool.label || pool.pool_id}</b>
            <span className="ma-mech">{MECHANISM[pool.mechanism] || pool.mechanism || '机制未判定'}</span>
            <span className="ma-pm-sub">
              {whole(pool.rounds)} 个批次 · 窗口 {dec(pool.window_hours)} 小时 · 候选 {poolLinks.length} 条
            </span>
          </div>
          <div className="ma-pm-score">
            <small>判别分</small>
            <b>{whole(pool.detection_score)}</b>
          </div>
        </header>

        {rows.length && cols.length ? (
          <div className="ma-mx-wrap">
            <div
              className="ma-mx"
              style={{ gridTemplateColumns: `104px repeat(${cols.length}, minmax(56px, 1fr)) 78px` }}
              role="group"
              aria-label={`${pool.label || pool.pool_id} 配对矩阵：行是 ${rows.length} 条入金，列是 ${cols.length} 条出金`}
            >
              <div className="ma-mx-corner">入金 ＼ 出金</div>
              {cols.map((col) => {
                const edge = edgeMap.get(col);
                return (
                  <div className="ma-mx-colhead" key={`c-${col}`} title={edgeTitle(col, edge)}>
                    <b>{col}</b>
                    <small>{edge ? `${trimAmount(edge.amount)} ${edge.asset || asset}` : '明细缺失'}</small>
                  </div>
                );
              })}
              <div className="ma-mx-sumhead">行内最高</div>

              {rows.map((row) => {
                const edge = edgeMap.get(row);
                const best = rowBest.get(row);
                return (
                  <React.Fragment key={`r-${row}`}>
                    <div className="ma-mx-rowhead" title={edgeTitle(row, edge)}>
                      <b>{row}</b>
                      <small>{edge ? `${trimAmount(edge.amount)} ${edge.asset || asset} · ${fmtTime(edge.time)}` : '明细缺失'}</small>
                    </div>

                    {cols.map((col) => {
                      const link = pairMap.get(`${row}|${col}`);
                      if (!link) {
                        return (
                          <div
                            className="ma-cell ma-cell-empty"
                            key={`${row}-${col}`}
                            role="img"
                            aria-label={`入金 ${row} 与出金 ${col} 没有进入候选集`}
                            title="未进入候选集：这一对没有被打分，不是 0 分"
                          />
                        );
                      }
                      const tone = toneOf(link.score, bounds);
                      const tier = effectiveTierOf(link);
                      const state = stateInfoOf(link);
                      const on = selection?.kind === 'link' && selection.id === link.link_id;
                      const tieN = tieCountOf(link);
                      const tied = tieN > 1;
                      return (
                        <button
                          type="button"
                          key={`${row}-${col}`}
                          className={`ma-cell ma-cell-btn ma-tone-${tone}${Number(link.rank) === 1 ? ' ma-cell-best' : ''}${tied ? ' ma-cell-tie' : ''} ma-state-${state.tone}`}
                          aria-pressed={on}
                          aria-label={`入金 ${row} 与出金 ${col}：归因分 ${dec(link.score)}，${tier}，本行第 ${link.rank ?? '—'} 名${tied ? `，与同分候选并列（共 ${tieN} 条）` : ''}`}
                          title={
                            `${link.link_id} · ${row} × ${col} · 归因分 ${dec(link.score)}（${tier}，本行第 ${link.rank ?? '—'} 名）` +
                            (tied ? `\n同分并列 ${tieN} 条：分数相同，排名不分先后，需下游证据收敛` : '')
                          }
                          onClick={() => pickLink(link)}
                        >
                          <b>{dec(link.score)}</b>
                          <small aria-hidden="true">{link.rank != null ? `#${link.rank}` : ''}</small>
                          {tied && <i className="ma-cell-tie-tag">并列×{tieN}</i>}
                          <i
                            className={`ma-cell-tie-tag ma-state-tag ma-state-${state.tone}`}
                            title={`${state.label}｜档位 ${tier}｜${REASON_CODE_CN[link.identifiability?.reason_code] || link.identifiability?.reason || ''}`}
                          >
                            {state.label}
                          </i>
                        </button>
                      );
                    })}

                    <div className="ma-mx-sum" title="同一行（同一笔入金）里归因分最高的出金">
                      {best ? (
                        <>
                          <b>{dec(best.score)}</b>
                          <small>
                            {best.tieN > 1 ? `${best.tieN} 条同分并列` : best.withdrawal_edge}
                          </small>
                        </>
                      ) : (
                        <small>无候选</small>
                      )}
                    </div>
                  </React.Fragment>
                );
              })}
            </div>
          </div>
        ) : (
          <p className="ma-none">该池没有入金/出金边记录，无法两两配对。</p>
        )}

        {!poolLinks.length && rows.length > 0 && cols.length > 0 ? (
          <p className="ma-none ma-none-in">
            本池 {rows.length} 个入金 × {cols.length} 个出金全部没有进入候选集：空白代表「没有可解释的配对」，不是 0 分。
          </p>
        ) : null}
      </section>
    );
  }

  function renderMatrixView() {
    return (
      <div className="ma-matrix-view">
        <p className="ma-hint">
          <span>
            行=进入混币池的资金，列=混币池转出的资金；同一池内两两配对打分，<b>同行比较</b>才有意义。
          </span>
          <em>分数是候选匹配强度，不是资金转移的确认。</em>
        </p>

        <div className="ma-legend" aria-label="归因分色阶与空白说明">
          <span className="ma-lg-t">分数档位</span>
          <span className="ma-lg" title="分数只表示候选匹配强度">
            HIGH ≥ {clean(bounds.high)}
          </span>
          <span className="ma-lg" title="分数只表示候选匹配强度">
            MEDIUM {clean(bounds.medium)}–{clean(bounds.high)}
          </span>
          <span className="ma-lg" title="分数只表示候选匹配强度">
            LOW &lt; {clean(bounds.medium)}
          </span>
          <em className="ma-lg-note">格子颜色表示可识别性，不表示分数高低</em>
          <span className="ma-lg ma-lg-gap">
            <i className="ma-lg-hatch" />
            未进入候选集（不是 0 分）
          </span>
        </div>

        {(() => {
          const counts = identifiabilityCounts(links);
          const total = STATE_ORDER.reduce((n, k) => n + (counts[k] || 0), 0);
          if (!total) return null;
          return (
            <div className="ma-states" aria-label="逐笔可识别性六态汇总">
              <span className="ma-lg-t">可识别性</span>
              {STATE_ORDER.filter((k) => counts[k]).map((k) => (
                <span
                  className={`ma-state-chip ma-state-${STATE_TONE[k]}`}
                  key={k}
                  title={`${IDENTIFIABILITY_CN[k]}：${counts[k]} 条（合计 ${total} 条，与下方矩阵逐格一致）`}
                >
                  {IDENTIFIABILITY_CN[k]} <b>{counts[k]}</b>
                </span>
              ))}
              <em className="ma-states-note">
                六态与分数档位是两件事：分数高不等于能当唯一结论
              </em>
            </div>
          );
        })()}

        {pools.length ? (
          pools.map((pool) => renderPoolMatrix(pool))
        ) : (
          <p className="ma-none">本案件没有识别出混币池，因此没有可配对的入金/出金。</p>
        )}
      </div>
    );
  }

  /* ---------------------------------------------------------------- 列表视图 */

  function renderListView() {
    if (!links.length) {
      return (
        <div className="ma-list-view">
          <p className="ma-none">本案件没有断链归因候选。</p>
        </div>
      );
    }
    return (
      <div className="ma-list-view">
        <div className="ma-table" role="group" aria-label="断链归因候选列表">
          <div className="ma-tr ma-th">
            <span>候选</span>
            <span>入金</span>
            <span>出金</span>
            <span className="ma-num">归因分</span>
            <span>档位</span>
            <span>可识别性</span>
            <span className="ma-num">排名</span>
          </div>
          {links.map((link) => {
            const tone = toneOf(link.score, bounds);
            const tier = link.tier || tierOf(link.score, bounds);
            const on = selection?.kind === 'link' && selection.id === link.link_id;
            const tieN = tieCountOf(link);
            return (
              <button
                type="button"
                key={link.link_id}
                className={`ma-tr ma-tr-btn ma-tone-${tone}${on ? ' is-on' : ''}`}
                aria-pressed={on}
                aria-label={`${link.link_id}：入金 ${link.deposit_edge} 对出金 ${link.withdrawal_edge}，归因分 ${dec(link.score)}，${tier}，第 ${link.rank ?? '—'} 名${tieN > 1 ? `，与 ${tieN - 1} 条候选同分并列` : ''}`}
                title={`${link.link_id} · ${link.deposit_edge} → ${link.withdrawal_edge} · ${dec(link.score)}（${tier}）${tieN > 1 ? ` · 同分并列 ${tieN} 条` : ''}`}
                onClick={() => pickLink(link)}
              >
                <span className="ma-td-id">
                  <b>{link.link_id}</b>
                  <small>{poolById.get(link.pool_id)?.label || link.pool_id}</small>
                </span>
                <span className="ma-td-edge">{link.deposit_edge}</span>
                <span className="ma-td-edge">{link.withdrawal_edge}</span>
                <span className="ma-td-num">{dec(link.score)}</span>
                <span className="ma-td-tier">
                  <span className="ma-badge ma-tier-badge">{tier}</span>
                </span>
                <span className="ma-td-state">
                  <span className={`ma-badge ma-state-${stateInfoOf(link).tone}`}>
                    {stateInfoOf(link).label}
                  </span>
                </span>
                <span className="ma-td-num ma-td-rank">
                  {link.rank != null ? `#${link.rank}` : '—'}
                  {tieN > 1 && <i className="ma-tie-mini">并列×{tieN}</i>}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  /* ---------------------------------------------------------------- 池列表 */

  function renderPoolCard(pool) {
    const signals = Array.isArray(pool.signals) ? pool.signals : [];
    const hits = signals.filter((signal) => signal.hit).length;
    const poolLinks = links.filter((link) => link.pool_id === pool.pool_id);
    const on = selection?.kind === 'pool' && selection.id === pool.pool_id;
    return (
      <article className={`ma-pool${on ? ' is-on' : ''}`} key={pool.pool_id || pool.label}>
        <button
          type="button"
          className="ma-pool-head"
          aria-pressed={on}
          title={`在右侧查看「${pool.label || pool.pool_id}」的组成`}
          onClick={() => pickPool(pool)}
        >
          <span className="ma-pool-name">{pool.label || pool.pool_id}</span>
          <span className="ma-mech">{MECHANISM[pool.mechanism] || pool.mechanism || '机制未判定'}</span>
          <span className="ma-pool-detect">
            <b>{whole(pool.detection_score)}</b>
            <small>判别分</small>
          </span>
          <ChevronRight size={14} className="ma-pool-go" />
        </button>

        <div className="ma-pool-meta">
          <span>
            批次 <b>{whole(pool.rounds)}</b>
          </span>
          <span>
            窗口 <b>{dec(pool.window_hours)}</b> 小时
          </span>
          <span title={`入金合计原值 ${pool.inflow || '—'}`}>
            入金 <b>{trimAmount(pool.inflow) || '—'}</b>
          </span>
          <span title={`出金合计原值 ${pool.outflow || '—'}`}>
            出金 <b>{trimAmount(pool.outflow) || '—'}</b>
          </span>
          <span>
            入金边 <b>{pool.deposit_edges?.length || 0}</b> / 出金边 <b>{pool.withdrawal_edges?.length || 0}</b>
          </span>
          <span>
            归因候选 <b>{poolLinks.length}</b>
          </span>
        </div>

        <ul className="ma-sigs">
          {signals.map((signal) => (
            <li className={`ma-sig ${signal.hit ? 'is-hit' : 'is-miss'}`} key={signal.id || signal.label}>
              <span className="ma-sig-ico" aria-hidden="true">
                {signal.hit ? <Check size={12} /> : <Minus size={12} />}
              </span>
              <span className="ma-sig-name">
                {signal.label}
                {signal.id && <i>{signal.id}</i>}
              </span>
              <span className="ma-sig-state">{signal.hit ? '命中' : '未命中'}</span>
              <span className="ma-sig-val">
                {clean(signal.value)} <em>/ {signal.threshold || '阈值缺失'}</em>
              </span>
              <span className="ma-sig-detail">{signal.detail || '该信号没有给出说明'}</span>
            </li>
          ))}
          {!signals.length && <li className="ma-sig ma-sig-none">该池没有输出判别信号。</li>}
        </ul>

        <p className="ma-sig-sum">
          命中 <b>{hits}</b> / {signals.length} 条信号。未命中项同样列出，用来回答「为什么不是它」。
        </p>
      </article>
    );
  }

  function renderPools() {
    return (
      <section className="ma-pools">
        <button
          type="button"
          className="ma-pools-head"
          aria-expanded={poolsOpen}
          onClick={() => setPoolsOpen((open) => !open)}
        >
          <ChevronDown size={14} className={poolsOpen ? 'ma-chev' : 'ma-chev ma-chev-closed'} />
          <span>混币池明细</span>
          <em>
            {pools.length} 个池 · 判别分来自各信号命中情况，点击池名在右侧看组成
          </em>
        </button>
        {poolsOpen && pools.map((pool) => renderPoolCard(pool))}
      </section>
    );
  }

  /* ---------------------------------------------------------------- 右侧栏 */

  function renderOverviewSide() {
    const attribution = metrics?.attribution;
    // 口径串一律读数据，不硬编码「零竞争条件」：算法侧会随场景改成「含竞争条件」
    const competition = attribution
      ? [attribution.data, attribution.competition].filter(Boolean).join(' · ') || '合成数据'
      : '合成数据';
    const nCount = isNum(attribution?.n) ? Number(attribution.n) : null;
    const avgTied = isNum(attribution?.avg_tied) ? Number(attribution.avg_tied) : null;

    return (
      <div className="ma-side-in">
        <div className="side-block">
          <h4>
            本案件概览
            <span className="ma-h4-note">{current.case_id}</span>
          </h4>
          <div className="ma-ovstats">
            <div className="ma-ovstat">
              <small>混币池</small>
              <b>{pools.length}</b>
            </div>
            <div className="ma-ovstat">
              <small>归因候选</small>
              <b>{links.length}</b>
            </div>
          </div>
          <div className="ma-distbar" aria-hidden="true">
            {TIERS.map((tier) => (
              <i
                key={tier}
                className={`ma-dist-${tier}`}
                style={{ flex: stats.tiers[tier].length || 0 }}
              />
            ))}
          </div>
          <div className="ma-distlegend">
            {TIERS.map((tier) => (
              <span key={tier}>
                <i className={`ma-dist-${tier}`} />
                {tier} <b>{stats.tiers[tier].length}</b>
              </span>
            ))}
          </div>
          <p className="ma-tie-note">
            HIGH {stats.high} 条中包含同分并列，<b>不代表 {stats.high} 份独立证据</b>；全案 {stats.depositCount} 笔入金里只有{' '}
            {stats.distinctRank1 === null ? '—' : stats.distinctRank1} 笔在本行内首位无并列。
          </p>
        </div>

        <div className="side-block">
          <h4>
            合成数据评估
            <span className="ma-simtag">{competition}</span>
          </h4>
          {attribution ? (
            <div className="ma-metrics">
              <div>
                <small>p@1</small>
                <b>{dec(attribution.p_at_1, 3)}</b>
              </div>
              <div>
                <small>p@3</small>
                <b>{dec(attribution.p_at_3, 3)}</b>
              </div>
              <div>
                <small>MRR</small>
                <b>{dec(attribution.mrr, 3)}</b>
              </div>
            </div>
          ) : (
            <p>本案件没有给出合成数据评估指标。</p>
          )}
          {(nCount !== null || avgTied !== null) && (
            <div className="ma-metric-meta">
              {nCount !== null && (
                <span>
                  入金样本 <b>{nCount}</b> 笔
                </span>
              )}
              {avgTied !== null && (
                <span>
                  平均同分 <b>{dec(avgTied, 2)}</b> 条
                </span>
              )}
            </div>
          )}
          <p className="ma-note">
            {metrics?.note
              ? metrics.note
              : `只在${competition}下统计配对一致性；样本量很小，不能当作实战效果。`}
          </p>
          {Array.isArray(metrics?.baselines) && metrics.baselines.length > 0 && (
            <div className="ma-baselines">
              <small>对照基线（同样是合成数据）</small>
              <div className="ma-baseline-head">
                <span />
                <b>p@1</b>
                <b>MRR</b>
              </div>
              {metrics.baselines.map((baseline) => (
                <div className="ma-baseline-row" key={baseline.name}>
                  <span>{baseline.name}</span>
                  <b>{dec(baseline.p_at_1, 3)}</b>
                  <b>{dec(baseline.mrr, 3)}</b>
                </div>
              ))}
              <small className="ma-baselines-note">基线用来说明多特征加权相对单特征的变化。</small>
            </div>
          )}
        </div>

        <div className="side-block">
          <h4>从这里开始</h4>
          <p>先看矩阵里同一行的分数高低，再点开单元格看六维拆解；点池卡可以看池的组成与命中信号。</p>
        </div>
      </div>
    );
  }

  function renderLinkSide(link) {
    const features = link.features || {};
    const pool = poolById.get(link.pool_id);
    const tiedN = tieCountOf(link);

    // —— 先验 / 候选规模 ——
    // 口径：N 取该 link 所在池的 withdrawal_edges 条数，即「一笔入金面对多少个候选出金」的匿名集大小。
    // 不用「同 deposit_edge 的 links 条数」是因为后者只反映算法实际打了多少对，遇到截断/剪枝会把先验说大（更乐观）。
    // 用池级出金边数偏保守：它是一个上界口径，宁可能力被低估，也不把不确定性说小。
    // 混币池真实匿名集可能上万地址，似然比压不过先验 —— 把这句话摆在界面上，比被评审问出来强。
    // 红线：只由当前候选数直接算 1/N，不出现任何「后验 xx%」——没有真实池规模，编一个数就是造假。
    const priorN = Array.isArray(pool?.withdrawal_edges) ? pool.withdrawal_edges.length : 0;
    const scoredN = links.filter((item) => item.pool_id === link.pool_id && item.deposit_edge === link.deposit_edge).length;
    const priorText =
      priorN > 0 ? `${priorN === 1 ? '1/1' : `1/${priorN}`} = ${(100 / priorN).toFixed(priorN > 1 ? 1 : 0)}%` : '—';

    const rows = FEATURES.map((item) => {
      const raw = features[item.key];
      if (!raw) return null;
      const value = Number(raw.value) || 0;
      const weight = Number(raw.weight) || 0;
      return { ...item, value, weight, contrib: (value * weight) / 100, detail: raw.detail || '' };
    }).filter(Boolean);

    const sum = rows.reduce((acc, row) => acc + row.contrib, 0);
    const score = Number(link.score);
    const tone = toneOf(score, bounds);
    const tier = link.tier || tierOf(score, bounds);
    const diff = Math.abs(sum - score);
    const consistent = Number.isFinite(score) && rows.length > 0 && diff <= 0.15;
    const deposit = edgeMap.get(link.deposit_edge);
    const withdrawal = edgeMap.get(link.withdrawal_edge);

    return (
      <div className="ma-side-in">
        <div className="ma-side-top">
          <button type="button" className="ma-back" onClick={() => setSelection(null)}>
            <ChevronRight size={12} />
            返回概览
          </button>
          <div className="ma-kicker">
            断链归因候选 · {link.link_id} · {pool?.label || link.pool_id || '池未知'}
          </div>
          <div className="ma-score-line">
            <b className="ma-score">{dec(link.score)}</b>
            <div className="ma-score-meta">
              <span className="ma-badge ma-tier-badge">{tier}</span>
              <span className="ma-rank">{link.rank == null ? '同行排名 —' : (tiedN > 1 ? `并列第 ${link.rank}` : `同行第 ${link.rank}`)}</span>
              {typeof link.ground_truth_hit === 'boolean' && (
                <span
                  className="ma-gt"
                  title="构造真值由本团队在合成数据里人为设定，只用于计算 p@k；一致与不一致都不构成准确或错误"
                >
                  合成数据 · {link.ground_truth_hit ? '与构造真值一致' : '与构造真值不一致（演示用）'}
                </span>
              )}
            </div>
          </div>
          {typeof link.ground_truth_hit === 'boolean' && (
            <p className="ma-gt-note">构造真值仅用于在合成数据上计算 p@k，不代表真实案件判定。</p>
          )}

          {tieCountOf(link) > 1 && (
            <p className="ma-tie-hint">
              本行有 <b>{tieCountOf(link)}</b> 条候选同分（差 &lt; {String(TIE_EPS)}），排名并列、不分先后；分数本身无法区分它们。
            </p>
          )}

          {(() => {
            const state = stateInfoOf(link);
            const idb = link.identifiability || {};
            const code = idb.reason_code;
            const missing = missingFieldsOf(link);
            const applied = idb.resolved_by_evidence || [];
            const ignored = idb.evidence_ignored || [];
            return (
              <div className={`ma-state-block ma-state-${state.tone}`}>
                <div className="ma-state-head">
                  <span className="ma-state-label">{state.label}</span>
                  <span className="ma-state-split">
                    与档位分开：档位看分数高低，这里看能不能当唯一结论
                  </span>
                </div>
                {code && (
                  <p className="ma-state-why">
                    <b>{code}</b>：{REASON_CODE_CN[code] || idb.reason || ''}
                  </p>
                )}
                {missing.length > 0 && (
                  <p className="ma-state-missing">
                    缺关键字段：{missing.join('、')}（因此不给出归因分与名次）
                  </p>
                )}
                {applied.length > 0 && (
                  <p className="ma-state-ev">由下列已核验证据定下：{applied.join('、')}</p>
                )}
                {ignored.length > 0 && (
                  <ul className="ma-state-ignored">
                    {ignored.slice(0, 4).map((x, i) => (
                      <li key={`ig${i}`}>{x.evidence_id}：{x.reason}</li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })()}

          <div className="ma-pair">
            <div className="ma-pair-side">
              <small>入金</small>
              <b title={edgeTitle(link.deposit_edge, deposit)}>{link.deposit_edge}</b>
              <em>{deposit ? `${trimAmount(deposit.amount)} ${deposit.asset || asset} · ${fmtTime(deposit.time)}` : '明细缺失'}</em>
            </div>
            <ChevronRight size={13} className="ma-pair-arrow" />
            <div className="ma-pair-side">
              <small>出金</small>
              <b title={edgeTitle(link.withdrawal_edge, withdrawal)}>{link.withdrawal_edge}</b>
              <em>
                {withdrawal
                  ? `${trimAmount(withdrawal.amount)} ${withdrawal.asset || asset} · ${fmtTime(withdrawal.time)}`
                  : '明细缺失'}
              </em>
            </div>
          </div>
        </div>

        <div className="side-block ma-prior">
          <h4>
            先验 / 候选规模
            <span className="ma-h4-note">决定这个分数能说明什么</span>
          </h4>
          <div className="ma-prior-row">
            <div className="ma-prior-cell">
              <small>本池候选出金数</small>
              <b>{priorN > 0 ? `N = ${priorN}` : '—'}</b>
            </div>
            <div className="ma-prior-cell">
              <small>无其他证据时任选一对的先验</small>
              <b>{priorText}</b>
            </div>
          </div>
          {priorN > 1 && scoredN > 0 && scoredN !== priorN && (
            <p className="ma-prior-sub">
              本行实际打分了 <b>{scoredN}</b> 对（少于 {priorN}，其余未进入候选集）；先验仍按 <b>1/{priorN}</b> 计，不把未打分当作已排除。
            </p>
          )}
          {tiedN > 1 && (
            <p className="ma-prior-tie">
              本入金存在同分并列（{tiedN} 条），实际先验进一步下降。
            </p>
          )}
          <p className="ma-prior-text">
            归因分是规则命中累加（0–100），<b>不是概率</b>。若池内有 {priorN > 0 ? priorN : 'N'} 个等额候选，仅凭金额与时间无法把某对的确信度提高到 1/{priorN > 0 ? priorN : 'N'} 之上；提级必须靠下游证据（交易所入金行为、KYC 调取、资金走向交叉）。
          </p>
        </div>

        <div className="side-block">
          <h4>
            六维特征
            <span className="ma-h4-note">条形长度 = 维度得分</span>
          </h4>
          <div className="ma-feats">
            {rows.map((row) => (
              <div className="ma-feat" key={row.key}>
                <div className="ma-feat-top">
                  <span className="ma-feat-name">
                    {row.label}
                    <i>{row.key}</i>
                  </span>
                  <span className="ma-feat-contrib" title="维度得分 × 权重 ÷ 100">
                    {row.contrib.toFixed(2)}
                  </span>
                </div>
                <div className="ma-feat-track">
                  <i className="ma-feat-fill" style={{ width: `${Math.max(0, Math.min(100, row.value))}%` }} />
                </div>
                <div className="ma-feat-meta">
                  <span>得分 {dec(row.value)}</span>
                  <span>权重 {clean(row.weight)}</span>
                </div>
                {row.detail && <p className="ma-feat-detail">{row.detail}</p>}
              </div>
            ))}
            {!rows.length && <p>该候选没有给出六维特征。</p>}
          </div>
          <div className="ma-sum">
            <span>
              六维加权合计 <b>{sum.toFixed(2)}</b>
            </span>
            {consistent ? (
              <span className="ma-ok" title="合计与归因分相差不超过 0.15，按四舍五入视为一致">
                与归因分一致
              </span>
            ) : (
              <span className="ma-warn">与归因分相差 {diff.toFixed(2)}</span>
            )}
          </div>
        </div>

        <div className="side-block">
          <h4>
            为什么这样配对
            <span className="ma-h4-note">{Array.isArray(link.explain) ? link.explain.length : 0} 条</span>
          </h4>
          {Array.isArray(link.explain) && link.explain.length ? (
            <ul>
              {link.explain.map((text, index) => (
                <li key={`${link.link_id}-e${index}`}>{text}</li>
              ))}
            </ul>
          ) : (
            <p>该候选没有给出文字解释。</p>
          )}
        </div>
      </div>
    );
  }

  function renderPoolSide(pool) {
    const signals = Array.isArray(pool.signals) ? pool.signals : [];
    const hits = signals.filter((signal) => signal.hit).length;
    const poolLinks = links
      .filter((link) => link.pool_id === pool.pool_id)
      .slice()
      .sort((a, b) => Number(b.score) - Number(a.score));

    return (
      <div className="ma-side-in">
        <div className="ma-side-top">
          <button type="button" className="ma-back" onClick={() => setSelection(null)}>
            <ChevronRight size={12} />
            返回概览
          </button>
          <div className="ma-kicker">混币池候选 · {pool.pool_id}</div>
          <div className="ma-pool-title">{pool.label || pool.pool_id}</div>
          <div className="ma-score-line">
            <b className="ma-score">{whole(pool.detection_score)}</b>
            <div className="ma-score-meta">
              <span className="ma-mech">{MECHANISM[pool.mechanism] || pool.mechanism || '机制未判定'}</span>
              <span className="ma-rank">判别分</span>
            </div>
          </div>
        </div>

        <div className="side-block">
          <h4>组成</h4>
          <ul>
            <li>批次 {whole(pool.rounds)} 个，观测窗口 {dec(pool.window_hours)} 小时</li>
            <li>
              入金合计 {trimAmount(pool.inflow) || '—'} {asset}，出金合计 {trimAmount(pool.outflow) || '—'} {asset}
            </li>
            <li>
              入金边 {pool.deposit_edges?.length || 0} 条，出金边 {pool.withdrawal_edges?.length || 0} 条
            </li>
            <li>
              信号命中 {hits} / {signals.length} 条
            </li>
          </ul>
        </div>

        <div className="side-block">
          <h4>
            入金边
            <span className="ma-h4-note">{pool.deposit_edges?.length || 0} 条</span>
          </h4>
          <ul className="ma-edge-list">
            {(pool.deposit_edges || []).map((id) => (
              <li key={`d-${id}`} title={edgeTitle(id, edgeMap.get(id))}>
                <b>{id}</b>
                <span>{edgeMap.get(id) ? `${trimAmount(edgeMap.get(id).amount)} ${edgeMap.get(id).asset || asset}` : '明细缺失'}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className="side-block">
          <h4>
            出金边
            <span className="ma-h4-note">{pool.withdrawal_edges?.length || 0} 条</span>
          </h4>
          <ul className="ma-edge-list">
            {(pool.withdrawal_edges || []).map((id) => (
              <li key={`w-${id}`} title={edgeTitle(id, edgeMap.get(id))}>
                <b>{id}</b>
                <span>{edgeMap.get(id) ? `${trimAmount(edgeMap.get(id).amount)} ${edgeMap.get(id).asset || asset}` : '明细缺失'}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className="side-block">
          <h4>
            池内候选
            <span className="ma-h4-note">{poolLinks.length} 条</span>
          </h4>
          {poolLinks.length ? (
            <div className="ma-mini-links">
              {poolLinks.map((link) => (
                <button
                  type="button"
                  key={link.link_id}
                  className={`ma-mini${selection?.kind === 'link' && selection.id === link.link_id ? ' is-on' : ''}`}
                  onClick={() => pickLink(link)}
                  title={`${link.link_id} · ${link.deposit_edge} → ${link.withdrawal_edge}`}
                >
                  <span className="ma-mini-id">{link.link_id}</span>
                  <span className="ma-mini-pair">
                    {link.deposit_edge} → {link.withdrawal_edge}
                  </span>
                  <b>{dec(link.score)}</b>
                </button>
              ))}
            </div>
          ) : (
            <p>该池没有进入候选集的配对。</p>
          )}
        </div>
      </div>
    );
  }

  function renderSide() {
    if (selection?.kind === 'link') {
      const link = links.find((item) => item.link_id === selection.id);
      if (link) return renderLinkSide(link);
    }
    if (selection?.kind === 'pool') {
      const pool = pools.find((item) => item.pool_id === selection.id);
      if (pool) return renderPoolSide(pool);
    }
    return renderOverviewSide();
  }

  /* ---------------------------------------------------------------- 页面 */

  return (
    <section className="mod ma">
      {head}

      <div className="mod-kpis">
        <div className="kpi">
          <small>混币池</small>
          <b>{pools.length}</b>
          <span>个</span>
        </div>
        <div className="kpi">
          <small>断链归因候选</small>
          <b>{links.length}</b>
          <span>条</span>
        </div>
        <div className="kpi ma-kpi-tied" title="每个入金在本行候选里排名第 1 且没有并列的笔数；其余入金存在同分并列，需下游证据收敛。首位无并列不等于资金归属唯一确定">
          <small>首位无并列</small>
          <b>
            {stats.distinctRank1 === null ? '—' : stats.distinctRank1} / {stats.depositCount}
          </b>
          <span className="ma-kpi-note">
            其余入金同分并列，需下游证据收敛 · HIGH {stats.high} 条含并列，不代表独立证据
          </span>
        </div>
        <div className="kpi" title="本案件所有候选里的最高归因分；存在同分并列时这只是并列最高，不代表唯一解">
          <small>最高归因分</small>
          <b>{stats.maxScore === null ? '—' : dec(stats.maxScore)}</b>
          <span>分</span>
          {stats.topTieCount > 1 && <span className="ma-kpi-inline">并列×{stats.topTieCount}</span>}
        </div>
        <div className="kpi">
          <small>池判别分最高</small>
          <b>{stats.maxDetect === null ? '—' : whole(stats.maxDetect)}</b>
          <span>分</span>
        </div>
      </div>

      <div className="ma-bar">
        <div className="ma-seg" role="group" aria-label="视图切换">
          <button type="button" aria-pressed={view === 'matrix'} onClick={() => setView('matrix')}>
            <Layers size={13} />
            矩阵
          </button>
          <button type="button" aria-pressed={view === 'list'} onClick={() => setView('list')}>
            <List size={13} />
            列表
          </button>
        </div>
        <span className="ma-bar-note">
          {view === 'matrix'
            ? `${pools.length} 个池 · ${links.length} 条候选 · 点单元格看六维拆解`
            : `${links.length} 条候选 · 点行看六维拆解`}
        </span>
      </div>

      <div className="mod-body">
        <div className="mod-main">
          {view === 'matrix' ? renderMatrixView() : renderListView()}
          {renderPools()}
        </div>
        <aside className="mod-side ma-side">{renderSide()}</aside>
      </div>

      <footer className="mod-foot">
        <i />
        {FOOT_TEXT}
      </footer>
    </section>
  );
}

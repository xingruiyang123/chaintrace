import React from 'react';
import { Scale, Link2, Wallet, Gavel, AlertTriangle, Info } from 'lucide-react';
import './CaseDecisionPanel.css';
import {
  caseDecisionOf, decisionLayersOf, withheldDepositsOf, identifiabilityStateOf,
} from '../caseDecision.js';

/**
 * 案件级裁决面板（V0.2.2）。
 *
 * 只做展示，不做判定：所有状态都直接读后端 `veto.case_decision`。
 * 旧结果没有该字段时显示「未评估（旧结果）」并且不猜结论。
 *
 * 【关键元素索引】
 * · `.cdp-status[data-status]`：四态徽标，颜色区分 YES / NO / CONDITIONAL / UNDETERMINED。
 * · `.cdp-layer`：三层（逐笔 / 账户 / 资产）并排，各自独立说明，不互相替代。
 * · `.cdp-cond`：未满足条件与阻断条件分开列，前者黄、后者红。
 * · `.cdp-held`：关键字段缺失、因而没有归因分的入金；不给分数，只说明缺了什么。
 */
export default function CaseDecisionPanel({ result, caseId }) {
  if (!result) return null;
  const cases = result.cases || [];
  if (!cases.length) return null;

  const current = cases.find((c) => c.case_id === caseId) || cases[0];
  const decision = caseDecisionOf({ veto: current.veto });
  const layers = decisionLayersOf({ veto: current.veto });
  const held = (current.withheld_deposits || []).length
    ? current.withheld_deposits
    : withheldDepositsOf(result).filter((w) => w.caseId === current.case_id);

  const STATUS_TEXT = {
    YES: '满足执行条件',
    NO: '存在明确阻断',
    CONDITIONAL: '有条件可评估',
    UNDETERMINED: '尚不能判断',
  };

  // 后端枚举直译成中文。界面上不出现 SUPPORTED / CONFIRMED_OK 这类词——
  // 它们对使用者没有意义，而且容易和"已确认"混为一谈。
  const CN = {
    SUPPORTED: '已确认',
    CONFIRMED_OK: '已确认',
    CONDITIONAL: '需并列比对',
    PARTIAL: '部分出口已确认',
    MIXED_BLOCKED: '部分出口被阻断',
    UNKNOWN: '未查清',
    NOT_EVALUATED: '未评估',
    DENIED: '已明确否定',
    CONFLICTED: '存在冲突',
    INSUFFICIENT: '无可用证据',
    UNIDENTIFIABLE: '结构等价',
    WITHHELD: '不予判定',
  };
  const cn = (s) => CN[s] || s || '—';

  if (!decision) {
    return (
      <section className="cdp cdp-legacy" aria-label="案件级裁决">
        <span className="cdp-icon"><Info size={16} strokeWidth={1.6} /></span>
        <div>
          <strong>本次结果没有案件级裁决（旧结果）</strong>
          <p>
            这是 V0.2.1 及更早的结果文件，只有 <code>case_level_executable</code>
            {current?.veto?.case_level_executable ? `（${current.veto.case_level_executable}）` : ''}，
            没有逐笔 / 账户 / 资产三层说明，无法判断它的条件。重跑一次才能看到四态裁决。
          </p>
        </div>
      </section>
    );
  }

  const status = decision.decision_status;
  const unresolved = decision.unresolved_conditions || [];
  const blocking = decision.blocking_conditions || [];
  const linkLayer = layers?.link || null;
  const accountLayer = layers?.account || null;
  const assetLayer = layers?.asset || null;

  // 逐笔层：状态 + 各状态条数（用"条"，不重复枚举名）
  const linkCounts = linkLayer?.counts || {};
  const linkDetail = Object.keys(linkCounts).length
    ? Object.entries(linkCounts).map(([k, v]) => `${cn(k)} ${v} 条`).join('、')
    : '无候选';

  return (
    <section className="cdp" aria-label="案件级裁决">
      <header className="cdp-head">
        <span className="cdp-icon"><Scale size={17} strokeWidth={1.6} /></span>
        <div className="cdp-title">
          <h3>案件级裁决</h3>
          <span className="cdp-case">{current.case_id}</span>
        </div>
        <span className="cdp-status" data-status={status}>{STATUS_TEXT[status] || status}</span>
      </header>

      <div className="cdp-layers">
        <div className="cdp-layer" data-state={linkLayer?.status || 'UNKNOWN'}>
          <span className="cdp-layer-icon"><Link2 size={14} strokeWidth={1.7} /></span>
          <div>
            <b>逐笔资金关联</b>
            <p>{linkDetail === '无候选' ? cn(linkLayer?.status) : linkDetail}</p>
          </div>
        </div>
        <div className="cdp-layer" data-state={accountLayer?.status || 'UNKNOWN'}>
          <span className="cdp-layer-icon"><Wallet size={14} strokeWidth={1.7} /></span>
          <div>
            <b>账户级资产关联</b>
            <p>{accountLayer?.reason || cn(accountLayer?.status)}</p>
            {accountLayer?.path_evaluated === false && (
              <p className="cdp-sub">独立路径评估尚未实现</p>
            )}
          </div>
        </div>
        <div className="cdp-layer" data-state={assetLayer?.status || 'UNKNOWN'}>
          <span className="cdp-layer-icon"><Gavel size={14} strokeWidth={1.7} /></span>
          <div>
            <b>资产处置条件</b>
            <p>{cn(assetLayer?.status)}
              {['UNKNOWN', 'PARTIAL', 'NOT_EVALUATED'].includes(assetLayer?.status)
                ? ' · 未知不等于不存在' : ''}</p>
          </div>
        </div>
      </div>

      <ul className="cdp-reasons">
        {(decision.decision_reasons || []).map((r, i) => (
          <li key={`r${i}`}>{r}</li>
        ))}
      </ul>

      {(blocking.length > 0 || unresolved.length > 0) && (
        <div className="cdp-conds">
          {blocking.length > 0 && (
            <div className="cdp-cond" data-kind="blocking">
              <span className="cdp-cond-head"><AlertTriangle size={13} strokeWidth={1.8} />明确阻断</span>
              <ul>{blocking.map((c, i) => <li key={`b${i}`}>{c}</li>)}</ul>
            </div>
          )}
          {unresolved.length > 0 && (
            <div className="cdp-cond" data-kind="unresolved">
              <span className="cdp-cond-head"><Info size={13} strokeWidth={1.8} />尚未满足</span>
              <ul>{unresolved.map((c, i) => <li key={`u${i}`}>{c}</li>)}</ul>
            </div>
          )}
        </div>
      )}

      {held.length > 0 && (
        <div className="cdp-held">
          <span className="cdp-cond-head">
            <AlertTriangle size={13} strokeWidth={1.8} />
            {held.length} 笔入金关键字段缺失，未给出归因分
          </span>
          <ul>
            {held.slice(0, 6).map((w, i) => (
              <li key={`h${i}`}>
                <code>{w.deposit_edge}</code> 缺 {w.missing_critical_fields.join('、')}
                <span className="cdp-held-tag">{w.identifiability_status}</span>
              </li>
            ))}
            {held.length > 6 && <li className="cdp-more">另有 {held.length - 6} 笔…</li>}
          </ul>
        </div>
      )}

      <footer className="cdp-foot">
        裁决版本 {decision.decision_version} · 评估层级 {(decision.evaluated_scopes || []).join(' / ')}
        {status === 'YES' && <span className="cdp-warn"> · 满足执行条件不代表现实中必然追回</span>}
      </footer>
    </section>
  );
}

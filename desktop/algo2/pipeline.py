"""管道：串起识别 → 归因 → 出口 → 可控性 → 路径，输出契约 §3 的完整结构。

同时提供 validate_result()，对输出做一次独立的字段与区间自检。
样例结果文件就是用这个函数校验的——它不是"跑通就算"，而是"逐字段可核对"。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import attribution, controllability, ledger, mixer_detect, offramp, paths
from .graph import (
    MONEY_RE,
    Dataset,
    InputError,
    file_sha256,
    load_dataset,
    load_file,
    sha256,
)

SCHEMA_VERSION = "mixer_offramp/v1"

BOUNDARIES = [
    "本页全部数据为虚构模拟数据，仅用于展示原型能力；混币—法币的对应关系为算法候选，不表示资金转移已确认。",
    "混币池为识别候选（附分数与信号），不等于已确认归属；未命中的信号同样输出，可用于说明「为何不是它」。",
    "归因分与可控性分的全部权重、阈值、罚分都写在 parameters 里，界面与复核者可以逐项复算。",
    "可控性评分中的冻结窗口与响应时效为估计值，不是实测数据。",
    "本结果不构成对任何真实主体的事实认定，也不能替代依法取得的证据。",
]

# v0.2.1 起按定义性结构归类（规格 C6）。新代码只会产出这六个值。
MECHANISMS = (
    "coinjoin",
    "fixed_denomination",
    "custodial",
    "ambiguous",
    "unknown",
    "insufficient_evidence",
)

# 已废弃：v0.2.1 之前的分型命名，新代码不再产出。
# 保留在自检白名单里，只是为了让历史结果文件仍能通过校验；
# 迁移完历史结果后应整体删除（见 INDEPENDENT_REVIEW F-05）。
LEGACY_MECHANISMS = ("equal_output", "tornado_like")
OFFRAMP_TYPE_ENUM = ("exchange", "otc", "payment_gateway", "underground_bank", "dex", "bridge")
TIERS = ("HIGH", "MEDIUM", "LOW")
LEVELS = ("GREEN", "YELLOW", "RED")
COST_LEVELS = ("低", "中", "高")
FEATURE_KEYS = ("amount", "time", "denomination", "path_rarity", "cooccurrence", "fee_fit")


def default_parameters() -> dict:
    return {
        "mixer": mixer_detect.default_params(),
        "attribution": attribution.default_params(),
        "offramp": offramp.default_params(),
        "controllability": controllability.default_params(),
    }


def _attach_evidence(links: list[dict]) -> None:
    """规格 C9：给每条候选的每个维度挂可追溯证据记录。

    只填系统确知的信息：来源类型是「算法推断」，直接性是 inferred，
    核验状态一律 unverified（未经外部核验）。**不填任何人工可靠性概率。**
    """
    for link in links:
        lid = link.get("link_id")
        records = []
        for key in FEATURE_KEYS:
            item = link.get("features", {}).get(key) or {}
            records.append({
                "evidence_id": f"EV-{lid}-{key}",
                "source_type": "algorithm_inferred",
                "source_reference": f"{link.get('deposit_edge')}→{link.get('withdrawal_edge')}#{key}",
                "observed_at": None,
                "directness": "inferred",
                "verification_status": "unverified",
                "reliability_basis": "由本系统规则计算得出，未经链下材料交叉核验",
                "independent_of": [],
                "counter_evidence_ids": [],
                "reproducible": True,
                "history": [],
                "detail": item.get("detail"),
            })
        link["evidence"] = records


def _load_evidence_entries(raw_entries) -> list[dict]:
    """解析输入的 evidence_entries，按 subject_link_id 建索引。

    非法记录直接拒收（规格 §6：source_reference 为空、scope 越界都要挡）。
    """
    if not raw_entries:
        return {}
    if not isinstance(raw_entries, list):
        raise InputError("evidence_entries 必须是列表")
    out: list[dict] = []
    for i, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise InputError(f"evidence_entries[{i}] 必须是对象")
        scope = item.get("scope")
        if scope not in attribution.EVIDENCE_SCOPES:
            raise InputError(f"evidence_entries[{i}].scope 不在枚举内：{scope!r}")
        if not item.get("source_reference"):
            raise InputError(f"evidence_entries[{i}] 必须给出 source_reference")
        if item.get("source_type") == "algorithm_inferred" \
                and item.get("verification_status") != "unverified":
            raise InputError(
                f"evidence_entries[{i}] 是算法推断，不得标记为已核验")
        if scope == "link" and not (item.get("subject_link_id")
                                    or (item.get("subject_deposit_edge")
                                        and item.get("subject_withdrawal_edge"))):
            raise InputError(
                f"evidence_entries[{i}] 的 scope=link 时必须给 subject_link_id，"
                f"或同时给 subject_deposit_edge 与 subject_withdrawal_edge")
        out.append(item)
    return out


def _dedupe_evidence(records: list[dict]) -> list[dict]:
    """同源证据只保留一份（规格 R6）。

    判定：若 A.independent_of 含 B 的 id（或反之），视为同源，
    保留先出现的那条，另一条记入 dropped 供审计。
    """
    ids = [r.get("evidence_id") for r in records]
    keep: list[dict] = []
    dropped: list[dict] = []
    for rec in records:
        rid = rec.get("evidence_id")
        related = set(rec.get("independent_of") or [])
        dup_of = next((o for o in keep
                       if o.get("evidence_id") in related
                       or rid in set(o.get("independent_of") or [])), None)
        if dup_of is not None:
            dropped.append({"evidence_id": rid, "same_source_as": dup_of.get("evidence_id")})
            continue
        keep.append(rec)
    return keep


def account_level(evidence: list[dict], offramps: list[dict] | None = None) -> dict:
    """第二层：账户级资产关联。

    只回答"这些记录能否关联到同一账户/托管主体"，
    **不回答**"其中每一笔资金是否已完成逐笔归因"。
    """
    acct = [r for r in (evidence or [])
            if r.get("scope") in ("account", "service")]
    acct = _dedupe_evidence(acct)
    verified = [r for r in acct
                if r.get("verification_status") == "verified"
                and r.get("directness") == "direct"]

    if not acct:
        status, reason = "UNKNOWN", "没有账户级证据，账户归属未查清"
    elif verified:
        status = "SUPPORTED"
        reason = f"{len(verified)} 条已核验的账户级证据支持账户归属"
    else:
        status = "CONDITIONAL"
        reason = "有账户级证据但未经核验或非直接，只能列为待核"

    return {
        "status": status,
        "reason": reason,
        "evidence_count": len(acct),
        "verified_count": len(verified),
        "evidence_ids": [r.get("evidence_id") for r in verified],
        "scope_note": ("账户级关联成立不等于逐笔资金关系成立；"
                       "本层结论不得向上替换逐笔结论"),
        # 证据是否足以支撑账户归属（这是本层唯一能回答的事）
        "evidence_ready": status == "SUPPORTED",
        # 账户级路径评估是否已经实现——**当前未实现**。
        # 保留下面这个旧字段只为兼容，它只表示"证据就绪"，不表示"能算路径"。
        "may_continue_account_path": status == "SUPPORTED",
        "path_evaluated": False,
        "capability_note": ("账户级**证据**状态已给出；账户级**路径评估**尚未实现，"
                            "本次不据此产生任何处置建议")
    }


def _apply_ledger_entries(case, offramps: list[dict], entries) -> None:
    """把入参里携带的外部录入应用到五维台账（规格 C7）。

    设计取舍：入参是唯一的录入通道，不做额外的 CLI。这样「同一份输入 + 同一份材料」
    能完整复现同一份输出，不需要另一套状态。

    任何非法录入一律抛错，**不静默忽略**——静默忽略会让人以为材料已生效。
    """
    if not entries:
        return
    by_offramp = {row.get("node_id"): row for row in offramps}
    for item in entries:
        if item.get("case_id") not in (None, case.case_id):
            continue
        node_id = item.get("offramp_id")
        row = by_offramp.get(node_id)
        if row is None:
            raise InputError(f"外部录入指向不存在的出口：{node_id}")
        book = row.get("ledger")
        if book is None:
            raise InputError(f"出口 {node_id} 没有台账，无法录入")
        try:
            ledger.apply_external_entry(
                book,
                item["dimension"],
                value=item.get("value"),
                status=item["status"],
                source_type=item["source_type"],
                source_reference=item["source_reference"],
                verified_by=item.get("verified_by", ""),
                reason=item.get("reason", ""),
            )
        except KeyError as exc:
            raise InputError(f"出口 {node_id} 的录入缺少字段 {exc}") from exc
        except ValueError as exc:
            raise InputError(f"出口 {node_id} 的录入不合法：{exc}") from exc


def _veto(case_links: list[dict], offramps: list[dict] | None = None,
          withheld_deposits: list[dict] | None = None,
          account_state: dict | None = None) -> dict:
    """规格 C8：两类阻断分开处理。

    A 资产处置硬阻断：由五维台账的**明确否定**触发（`asset_present=DENIED`
      或 `legal_condition=DENIED`）。UNKNOWN / STALE 一律不触发——没查清不等于不能办。
    B 证据链硬阻断：逐笔归因被判定不可输出强度时触发；**只限制该笔结论**，
       不得推导为整个案件不可执行。
    """
    withheld = [
        l["link_id"] for l in case_links
        if (l.get("identifiability") or {}).get("decision_status") == "WITHHELD"
    ]
    evidence_block = {
        "triggered": bool(withheld),
        "scope": "link",
        "link_ids": withheld,
        "reasons": (
            ["逐笔归因所在信息集下不可唯一确定，不输出强度结论"] if withheld else []
        ),
        "propagates_to_case": False,
    }

    # —— 逐出口算路径状态（V22-01）——
    # 旧实现用 any(CONFIRMED_OK)：只要一个出口确认，整案就 CONFIRMED_OK，
    # 把"另一个出口没查"这件事盖掉了。八案各有 2 个出口，这个 bug 一直在。
    paths = []
    for row in (offramps or []):
        if not row.get("ledger"):
            continue
        block = ledger.disposal_block(row["ledger"])
        if block["triggered"]:
            pstatus = "DENIED"
        elif block["status"] == "CONFIRMED_OK":
            pstatus = "CONFIRMED_OK"
        else:
            pstatus = "UNKNOWN"
        paths.append({
            "offramp_id": row.get("node_id"),
            "status": pstatus,
            "raw_status": block["status"],
            "triggered": bool(block["triggered"]),
            "reasons": list(block.get("reasons") or []),
        })

    statuses = [p["status"] for p in paths]
    if not paths:
        asset_status = "NOT_EVALUATED"
    elif all(s == "DENIED" for s in statuses):
        asset_status = "DENIED"
    elif any(s == "DENIED" for s in statuses):
        asset_status = "MIXED_BLOCKED"      # 有路径被明确阻断，另有路径未定
    elif all(s == "CONFIRMED_OK" for s in statuses):
        asset_status = "CONFIRMED_OK"
    elif any(s == "CONFIRMED_OK" for s in statuses):
        asset_status = "PARTIAL"            # 部分出口确认，其余未查清
    else:
        asset_status = "UNKNOWN"

    triggered = [p for p in paths if p["triggered"]]
    asset_block = {
        "triggered": bool(triggered),
        "status": asset_status,
        "paths": paths,
        "path_aggregation": ("全出口内部一致才给单一口径；否则标 PARTIAL / MIXED_BLOCKED，"
                             "不允许用任一出口的确认代表整案"),
        "reasons": [r for p in triggered for r in p["reasons"]],
        "unknown_is_not_denied": True,
        "note": (
            "后三维未录入时状态为 UNKNOWN，不构成阻断；"
            "只有明确否定（DENIED）才阻断处置建议"
        ),
    }
    if asset_block["triggered"]:
        executable = "NO"
    elif evidence_block["triggered"] or asset_status != "CONFIRMED_OK":
        executable = "UNDETERMINED"
    else:
        executable = "YES"
    decision = _case_decision(case_links, asset_block, evidence_block,
                              withheld_deposits or [], account_state)
    return {
        "asset_block": asset_block,
        "evidence_block": evidence_block,
        # 案件级四态（V0.2.2 新增，权威字段）
        "case_decision": decision,
        # 旧字段：由新结论派生，保持三值口径，旧消费者不受影响
        "case_level_executable": LEGACY_EXECUTABLE[decision["decision_status"]],
        "note": "S_ctrl 为展示性参考指标，不参与本判定；UNKNOWN 不等于 DENIED",
    }


# 案件级四态 → 旧三值。CONDITIONAL 落到 UNDETERMINED：
# 旧口径没有"有条件"这一档，宁可说"还不能定"，也不能说"能办"。
LEGACY_EXECUTABLE = {
    "YES": "YES",
    "NO": "NO",
    "CONDITIONAL": "UNDETERMINED",
    "UNDETERMINED": "UNDETERMINED",
}


def _trail_status(case_links: list[dict], withheld_deposits: list[dict]) -> dict:
    """第一层汇总：逐笔资金关联的整体状态。

    用新的六态；旧结果没有该字段时回落到 decision_status（LEGACY_MAPPED）。
    """
    statuses = []
    for link in case_links:
        idb = link.get("identifiability") or {}
        statuses.append(idb.get("identifiability_status")
                        or LEGACY_TO_IDENTIFIABILITY.get(idb.get("decision_status"), "UNKNOWN"))
    for item in withheld_deposits:
        statuses.append(item.get("identifiability_status") or "WITHHELD")

    counts = {}
    for s in statuses:
        counts[s] = counts.get(s, 0) + 1

    if not statuses:
        trail = "INSUFFICIENT"
    elif "CONFLICTED" in counts:
        trail = "CONFLICTED"
    elif counts.get("SUPPORTED", 0) == len(statuses):
        trail = "SUPPORTED"
    elif counts.get("SUPPORTED", 0) == 0:
        trail = "INSUFFICIENT"
    elif counts.get("SUPPORTED", 0) > 0 and (
            counts.get("WITHHELD", 0) or counts.get("CONDITIONAL", 0)
            or counts.get("UNIDENTIFIABLE", 0)):
        trail = "CONDITIONAL"
    else:
        trail = "CONDITIONAL"
    return {"status": trail, "counts": counts, "total": len(statuses)}


LEGACY_TO_IDENTIFIABILITY = {
    "ALLOWED": "SUPPORTED",
    "CONDITIONAL": "CONDITIONAL",
    "WITHHELD": "WITHHELD",
}


def _case_decision(case_links: list[dict], asset_block: dict, evidence_block: dict,
                   withheld_deposits: list[dict], account_state: dict | None = None) -> dict:
    """第三层：案件级裁决（规格 CASE_LEVEL_DECISION_SPEC）。

    纯逻辑条件，不做加权平均、不产生综合分数。四条规则的顺序即优先级。
    """
    trail = _trail_status(case_links, withheld_deposits)
    asset_status = asset_block.get("status")
    blocking, unresolved, reasons = [], [], []

    if asset_block.get("triggered"):
        # 任务书 §8.2：NO = 存在明确、经过核验的案件级否定条件。
        # 一个出口的 DENIED 就是这种条件（既有测试 test_denied_entry_triggers_asset_block 锁定）。
        # 但必须把每条路径的情况写清楚，不能因为一条被阻断就抹掉其他出口的结论。
        status = "NO"
        blocking = list(asset_block.get("reasons") or [])
        blocked = [p["offramp_id"] for p in (asset_block.get("paths") or []) if p["triggered"]]
        others = [f"{p['offramp_id']}({p['status']})"
                  for p in (asset_block.get("paths") or []) if not p["triggered"]]
        reasons.append(f"出口 {', '.join(map(str, blocked))} 存在明确否定，对应处置路径被阻断")
        if others:
            reasons.append(f"其余出口状态：{', '.join(others)}；"
                           "本次为整案级否定，未逐出口分别放行")
    elif asset_status in ("PARTIAL", "MIXED_BLOCKED"):
        # 部分出口已确认 / 部分被阻断，另一些没查清：
        # 有路径可继续评估，但不能说整案可执行，也不能把已阻断的路径混进来。
        status = "CONDITIONAL"
        blocked = [p["offramp_id"] for p in (asset_block.get("paths") or []) if p["triggered"]]
        open_ = [p["offramp_id"] for p in (asset_block.get("paths") or [])
                 if p["status"] == "CONFIRMED_OK"]
        if blocked:
            unresolved.append(f"出口 {', '.join(map(str, blocked))} 已被明确阻断")
        if open_:
            unresolved.append(f"出口 {', '.join(map(str, open_))} 已确认，可单独评估该路径")
        others = [p["offramp_id"] for p in (asset_block.get("paths") or [])
                  if p["status"] == "UNKNOWN"]
        if others:
            unresolved.append(f"出口 {', '.join(map(str, others))} 尚未查清")
        reasons.append(f"资产层为 {asset_status}，各出口状态不一致，不能用一个出口的结论代表整案")
    elif asset_status != "CONFIRMED_OK":
        # 资产没查清是更根本的缺失：即使逐笔层有冲突，也不能据此说"有条件可办"，
        # 只能说"还判断不了"。顺序上必须先于冲突判定。
        status = "UNDETERMINED"
        unresolved.append(f"资产状态为 {asset_status}，不足以判断能否处置")
        reasons.append("资产/法律/执行三维尚未全部确认")
    elif trail["status"] == "CONFLICTED":
        status = "CONDITIONAL"
        unresolved.append("证据冲突未解决")
        reasons.append("逐笔层存在相互矛盾的证据，须先解决冲突，不得自动取其一")
    elif trail["status"] != "SUPPORTED":
        status = "CONDITIONAL"
        unresolved.append("逐笔资金关联存在未解决的并列或缺失")
        # 不把 Python 字典直接写进面向用户的理由里
        _CN_STATUS = {"SUPPORTED": "可唯一定位", "CONDITIONAL": "需并列比对",
                      "UNIDENTIFIABLE": "结构等价", "WITHHELD": "不予判定",
                      "CONFLICTED": "存在冲突", "INSUFFICIENT": "无可用证据"}
        _parts = "、".join(f"{_CN_STATUS.get(k, k)} {v} 条"
                            for k, v in (trail["counts"] or {}).items())
        reasons.append(
            f"逐笔层{_parts}，案件结论依赖这些尚未确认的资金关系，"
            "因此不能判为可执行。账户级路径评估尚未实现，本次不纳入裁决"
        )
    else:
        status = "YES"
        reasons.append("逐笔关联全部可唯一确定，且资产三维全部确认，无未解决阻断")

    supporting = [l["link_id"] for l in case_links
                  if ((l.get("identifiability") or {}).get("identifiability_status")
                      or LEGACY_TO_IDENTIFIABILITY.get(
                          (l.get("identifiability") or {}).get("decision_status"))) == "SUPPORTED"]

    account = account_state or {"status": "UNKNOWN", "reason": "未评估",
                                "evidence_ids": [], "evidence_ready": False,
                                "may_continue_account_path": False,
                                "path_evaluated": False,
                                "capability_note": "账户级路径评估尚未实现"}
    scopes = ["link", "asset", "account"]
    # 账户级成立时补一句：可以走账户级路径，但这不是逐笔归因成功
    if account["status"] == "SUPPORTED" and status in ("CONDITIONAL", "UNDETERMINED"):
        reasons.append("账户级证据支持账户归属；但账户级路径评估尚未实现，"
                       "且逐笔资金关系尚未唯一确定，不得据此认定逐笔归因成立")

    return {
        "decision_status": status,
        "decision_version": "v0.2.2",
        "decision_reasons": reasons,
        "blocking_conditions": blocking,
        "unresolved_conditions": unresolved,
        "supporting_evidence_ids": supporting[:20],
        "counter_evidence_ids": [],
        "evaluated_scopes": scopes,
        "layers": {
            "link": trail,
            "account": account,
            "asset": {
                "status": asset_status,
                "triggered": bool(asset_block.get("triggered")),
                "unknown_is_not_denied": True,
            },
            "evidence_block": {
                "triggered": bool(evidence_block.get("triggered")),
                "scope": evidence_block.get("scope"),
                "link_count": len(evidence_block.get("link_ids") or []),
            },
        },
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _link_sort_key(case, link: dict):
    return (case.edge(link["deposit_edge"]).at, link["rank"], link["withdrawal_edge"])


def _ranked(pairs: list[tuple], truth: set[str], key) -> dict:
    """对一组候选做排序并算 p@1 / p@3 / MRR。

    并列处理：主键完全相同的候选，命中按 1/并列数 折算——同一池里有 3 笔等额出金时，
    任何方法都不可能只凭这些特征挑出唯一一笔，折算比硬判"命中"或"不中"都更接近事实。
    这条规则对被测方法与两个基线一视同仁。
    """
    if not pairs:
        return {"p_at_1": None, "p_at_3": None, "mrr": None, "n": 0}
    ordered = sorted(pairs, key=lambda item: (key(item), item[0].id))
    best_key = key(ordered[0])
    tied = [item for item in ordered if key(item) == best_key]
    if not truth:
        first_credit = 0.0
    else:
        first_credit = (1.0 / len(tied)) if any(item[0].id in truth for item in tied) else 0.0
    top3 = ordered[:3]
    top3_hit = any(item[0].id in truth for item in top3) if truth else False
    reciprocal = 0.0
    for index, item in enumerate(ordered, start=1):
        if item[0].id in truth:
            reciprocal = 1.0 / index
            break
    return {
        "p_at_1": round(first_credit, 4),
        "p_at_3": 1.0 if top3_hit else 0.0,
        "mrr": round(reciprocal, 4),
        "n": 1,
        "tied": len(tied),
    }


def _aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {"p_at_1": None, "p_at_3": None, "mrr": None, "n": 0}
    count = len(rows)
    return {
        "p_at_1": round(sum(r["p_at_1"] for r in rows) / count, 4),
        "p_at_3": round(sum(r["p_at_3"] for r in rows) / count, 4),
        "mrr": round(sum(r["mrr"] for r in rows) / count, 4),
        "n": count,
        "avg_tied": round(sum(r.get("tied", 1) for r in rows) / count, 2),
    }


def competition_label(case, links: list[dict], tolerance: Decimal) -> str:
    """是否存在"同额输出竞争"：同一入金有 ≥2 个出金互为等额。

    有竞争时，仅凭金额无法唯一确定配对——这正是归因指标要说明的前提。
    """
    by_deposit: dict[str, list[dict]] = {}
    for link in links:
        by_deposit.setdefault(link["deposit_edge"], []).append(link)
    for rows in by_deposit.values():
        amounts = [case.edge(r["withdrawal_edge"]).amount for r in rows]
        for i in range(len(amounts)):
            for j in range(i + 1, len(amounts)):
                a, b = amounts[i], amounts[j]
                if a == b or (a > 0 and abs(a - b) <= a * tolerance):
                    return "含竞争条件"
    return "零竞争条件"


def case_metrics(case, links: list[dict], params: dict) -> dict:
    truth = case.truth_links()
    if not truth or not links:
        return {
            "attribution": None,
            "baselines": [],
            "note": "输入未提供构造真值或没有归因候选，故不计算归因一致性指标。",
        }
    by_deposit: dict[str, list[tuple]] = {}
    for link in links:
        by_deposit.setdefault(link["deposit_edge"], []).append((case.edge(link["withdrawal_edge"]), link))

    method_rows: list[dict] = []
    amount_rows: list[dict] = []
    time_rows: list[dict] = []
    for deposit_id, pairs in by_deposit.items():
        truth_set = {w for (d, w) in truth if d == deposit_id}
        if not truth_set:
            continue
        deposit = case.edge(deposit_id)
        method_pairs = [(edge, link) for edge, link in pairs]
        method_rows.append(
            _ranked(method_pairs, truth_set, key=lambda item: -float(item[1]["score"]))
        )
        amount_rows.append(
            _ranked(
                [(edge, link) for edge, link in pairs],
                truth_set,
                key=lambda item: (abs(item[0].amount - deposit.amount), item[0].at),
            )
        )
        time_rows.append(
            _ranked(
                [(edge, link) for edge, link in pairs],
                truth_set,
                key=lambda item: (abs((item[0].at - deposit.at).total_seconds()), item[0].id),
            )
        )

    tolerance = Decimal(str(params["mixer"]["equal_output_tolerance"]))
    metrics = _aggregate(method_rows)
    metrics["competition"] = competition_label(case, links, tolerance)
    metrics["data"] = "合成数据"
    return {
        "attribution": metrics,
        "baselines": [
            {"name": "仅金额匹配", **_aggregate(amount_rows)},
            {"name": "仅时间最近", **_aggregate(time_rows)},
        ],
        "note": "指标口径为「给定入金，正确出金是否排在第 1/前 3」，只在合成数据上衡量归因一致性。",
    }


def run_dataset(dataset: Dataset, parameters: dict | None = None, source_file_sha256: str | None = None) -> dict:
    params = parameters or default_parameters()
    rules = offramp.load_rules()
    link_evidence = _load_evidence_entries(dataset.raw.get("evidence_entries"))
    counter = 0
    cases_out: list[dict] = []

    for case in dataset.cases:
        # V0.2.2：关键字段缺失的边不进入金额/时间类分析。
        # mixer_detect / offramp / paths 假设 amount、at 一定存在，这里给它们完整视图；
        # attribution 拿到原 Case，由它如实报告"哪条边缺了什么"。
        complete = case.complete_view()
        pools = mixer_detect.detect_case(complete, params["mixer"])
        links = attribution.attribute_case(case, pools, params, link_evidence)
        links.sort(key=lambda row: _link_sort_key(case, row))

        truth = case.truth_links()
        for link in links:
            counter += 1
            link["link_id"] = f"LK-{counter:03d}"
            link["ground_truth_hit"] = bool(
                truth and (link["deposit_edge"], link["withdrawal_edge"]) in truth
            )
            # V0.2.2：link_id 生成后再套外部证据（scope 隔离在 attribution 里判定）
            # 逐条按"是否指向本候选"筛选；不指向的记录不会进入本行
            if link_evidence:
                mine = [r for r in link_evidence
                        if r.get("scope") == "link"
                        and attribution.is_link_resolving(r, link)
                        or (r.get("scope") == "link"
                            and r.get("subject_link_id") == link["link_id"])]
                if mine:
                    link["identifiability"] = attribution.apply_link_evidence(
                        link["identifiability"], mine, link)
                    # 证据改了判定，派生字段必须跟着重算，否则自检会抓到不一致
                    idb = link["identifiability"]
                    link["effective_tier"] = ("WITHHELD"
                                              if idb["decision_status"] == "WITHHELD"
                                              else link["tier"])
                    link["rank_withheld"] = idb["decision_status"] == "WITHHELD"

        ordered_links = [
            {
                "link_id": link["link_id"],
                "pool_id": link["pool_id"],
                "deposit_edge": link["deposit_edge"],
                "withdrawal_edge": link["withdrawal_edge"],
                "score": link["score"],
                "tier": link["tier"],
                "rank": link["rank"],
                "features": link["features"],
                "explain": link["explain"],
                "ground_truth_hit": link["ground_truth_hit"],
                # 规格 C2 / C3 / C11：池假设声明、B 域辅助证据、全局冲突状态
                "conditional_on_pool_hypothesis": link.get("conditional_on_pool_hypothesis"),
                "auxiliary": link.get("auxiliary"),
                # 规格 C11 第二步：关系类型（默认 unresolved）
                "relation_type": link.get("relation_type"),
                "relation_resolved_by": link.get("relation_resolved_by"),
                "relation_basis": link.get("relation_basis"),
                # 规格 C1 / C4：可识别性与质量门槛
                "identifiability": link.get("identifiability"),
                "effective_tier": link.get("effective_tier"),
                "rank_withheld": link.get("rank_withheld"),
            }
            for link in links
        ]
        # 规格 C9：证据记录（每条候选每维一条，可追溯）
        _attach_evidence(ordered_links)
        # V0.2.2：关键字段缺失的入金如实登记（不编造分数）
        held = attribution.withheld_deposits(case, pools)

        # 规格 C11 第一步：冲突检测（不改动原始排序）
        conflicts = attribution.mark_conflicts(ordered_links)
        # 规格 C11 第二步：建立关系类型（默认 unresolved，不做推断）
        attribution.mark_relations(ordered_links)
        # 规格 C2：池判别分在输出阶段附到池假设字段上，供界面并列展示；
        # 归因阶段（attribution.py）不读取它，因此它不可能进入候选分数。
        pool_scores = {pool["pool_id"]: pool["detection_score"] for pool in pools}
        for link in ordered_links:
            hyp = link.get("conditional_on_pool_hypothesis")
            if hyp is not None:
                hyp["detection_score"] = pool_scores.get(link["pool_id"])
            # 规格 C3：出口可达性属 B 域，由 pipeline 附加，不进入归因打分
            withdrawal = case.edge(link["withdrawal_edge"])
            reached = offramp.reach_offramp(complete, withdrawal.dst)
            link["auxiliary"] = {
                "offramp_reach": attribution.offramp_reach_aux(case, link["withdrawal_edge"], reached)
            }

        offramps = offramp.detect_case(complete, pools, ordered_links, params["offramp"], rules)
        # 规格 C9：出口的证据记录（每条命中规则一条），并回灌到台账维度
        for row in offramps:
            records = []
            for hit in row.get("hits", []):
                records.append({
                    "evidence_id": f"EV-{row.get('node_id')}-{hit.get('rule_id')}",
                    "source_type": "algorithm_inferred",
                    "source_reference": f"rule:{hit.get('rule_id')}",
                    "observed_at": None,
                    "directness": "inferred",
                    "verification_status": "unverified",
                    "reliability_basis": "出口识别规则命中，未经外部材料核验",
                    "independent_of": [],
                    "counter_evidence_ids": [],
                    "reproducible": True,
                    "history": [],
                    "detail": hit.get("explain"),
                })
            row["evidence"] = records
        # 规格 C7：每个出口挂五维台账。后三维一律 UNKNOWN，等外部录入——
        # 系统不得自行赋值，也不能用 0 顶替。
        for row in offramps:
            row["ledger"] = ledger.build_ledger(
                {
                    "offramp_id": row.get("node_id"),
                    "score": row.get("s_off"),
                    "node_type": row.get("offramp_type"),
                },
                evidence_ids=[r["evidence_id"] for r in row["evidence"]],
            )
        # 规格 C7：把入参携带的外部录入应用到台账（没有该段则保持全 UNKNOWN）
        # 必须在 veto 之前，否则 A 类阻断读不到刚录入的否定结论
        _apply_ledger_entries(case, offramps, dataset.raw.get("offramp_ledger_entries"))
        # 规格 C8：两类阻断分开判定（依赖台账）
        veto = _veto(ordered_links, offramps, held,
                     account_level(link_evidence, offramps))
        plans = paths.build_case_paths(complete, pools, ordered_links, offramps)
        metrics = case_metrics(case, ordered_links, params)

        summary = {
            "nodes": len(case.nodes),
            "edges": len(case.edges),
            "mixer_pools": len(pools),
            "links": len(ordered_links),
            "offramps": len(offramps),
            "green": sum(1 for row in offramps if row["controllability"]["level"] == "GREEN"),
            "yellow": sum(1 for row in offramps if row["controllability"]["level"] == "YELLOW"),
            "red": sum(1 for row in offramps if row["controllability"]["level"] == "RED"),
            "high_links": sum(1 for row in ordered_links if row["tier"] == "HIGH"),
            "paths": len(plans),
            # 规格 C11：冲突统计（同一出金被多笔入金同时选为首选）
            "contested_withdrawals": conflicts["contested_count"],
            "contested_extra_claims": conflicts["extra_claims"],
            # 同额批次里同一个入金常有多笔同分并列第 1；这个数字才是"能唯一定位"的入金数
            "distinct_rank1": len(
                [
                    deposit
                    for deposit in {row["deposit_edge"] for row in ordered_links}
                    if len([row for row in ordered_links if row["deposit_edge"] == deposit and row["rank"] == 1]) == 1
                ]
            ),
        }

        cases_out.append(
            {
                "case_id": case.case_id,
                "network": case.network,
                "summary": summary,
                "mixer_pools": [mixer_detect.public_pool(pool) for pool in pools],
            "withheld_deposits": held,
                "links": ordered_links,
                "offramps": offramps,
                "paths": plans,
                "metrics": metrics,
                # 规格 C11：冲突清单（保留原始排序，不覆盖名次）
                "conflicts": conflicts,
                # 规格 C8：veto 判定（S_ctrl 只作展示）
                "veto": veto,
            }
        )

    result = {
        "schema_version": SCHEMA_VERSION,
        "is_simulated": True,
        "input_sha256": sha256(dataset.raw),
        "source_file_sha256": source_file_sha256,
        "generated_at": _now(),
        "parameters": params,
        "cases": cases_out,
        "boundaries": list(BOUNDARIES),
    }
    problems = validate_result(result)
    if problems:
        raise InputError("算法输出未通过自检：" + "；".join(problems[:5]))
    return result


def run(raw: dict) -> dict:
    """对已读入的 JSON 对象执行完整管道。

    此时拿不到原始文件字节，source_file_sha256 记为 null —— 只保留对象哈希。
    """
    return run_dataset(load_dataset(raw))


def run_file(path: str | Path) -> tuple[dict, dict]:
    """读文件 → 执行 → 返回（结果, 原始数据集）。

    额外记录源文件的字节哈希 source_file_sha256，用于核对"同一份输入没被改过"；
    input_sha256 仍是对象规范化哈希，两者语义不同，都在结果里。
    """
    digest = file_sha256(path)
    dataset, raw = load_file(path)
    return run_dataset(dataset, source_file_sha256=digest), raw


# —————————————————————— 输出结构自检 ——————————————————————


def _check_money(value: Any, where: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not MONEY_RE.match(value):
        problems.append(f"{where} 必须是 8 位小数的金额字符串，实际为 {value!r}")
        return
    try:
        Decimal(value)
    except InvalidOperation:
        problems.append(f"{where} 不是合法十进制数：{value!r}")


def _check_range(value: Any, where: str, problems: list[str], low: float = 0.0, high: float = 100.0) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        problems.append(f"{where} 必须是数字，实际为 {value!r}")
        return
    if not (low <= float(value) <= high):
        problems.append(f"{where} 超出 [{low:g}, {high:g}]：{value!r}")


def validate_result(result: Any, dataset: Dataset | None = None) -> list[str]:
    """逐字段检查契约 §3。返回问题列表；空列表表示通过。"""
    problems: list[str] = []
    if not isinstance(result, dict):
        return ["结果必须是 JSON 对象"]
    if result.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if result.get("is_simulated") is not True:
        problems.append("is_simulated 必须为 true")
    digest = result.get("input_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        problems.append("input_sha256 必须是 64 位小写十六进制")
    source_digest = result.get("source_file_sha256", "missing")
    if source_digest == "missing":
        problems.append("source_file_sha256 字段缺失（入参为对象时应显式写 null）")
    elif source_digest is not None and not (
        isinstance(source_digest, str) and re.fullmatch(r"[0-9a-f]{64}", source_digest)
    ):
        problems.append("source_file_sha256 必须是 64 位小写十六进制或 null")
    if not isinstance(result.get("generated_at"), str):
        problems.append("generated_at 缺失")

    params = result.get("parameters") or {}
    for key in ("mixer", "attribution", "offramp", "controllability"):
        if not isinstance(params.get(key), dict):
            problems.append(f"parameters.{key} 缺失")
    weights = (params.get("attribution") or {}).get("weights") or {}
    if abs(sum(float(v) for v in weights.values()) - 100.0) > 1e-9:
        problems.append("parameters.attribution.weights 合计必须为 100")
    if set(weights) != set(FEATURE_KEYS):
        problems.append("parameters.attribution.weights 的键必须与六维一致")

    boundaries = result.get("boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        problems.append("boundaries 必须是非空列表")

    cases = result.get("cases")
    if not isinstance(cases, list) or not cases:
        return problems + ["cases 必须是非空列表"]
    truth_cases = {case.case_id: case for case in dataset.cases} if dataset is not None else {}

    tier_params = (params.get("attribution") or {}).get("tier") or {"high": 70, "medium": 45}
    signal_params = (params.get("controllability") or {}).get("signal") or {"green": 75, "yellow": 60}
    alpha = float((params.get("controllability") or {}).get("alpha", 0.65))
    beta = float((params.get("controllability") or {}).get("beta", 0.25))
    gamma = float((params.get("controllability") or {}).get("gamma", 0.10))
    penalty = (params.get("controllability") or {}).get("penalty") or {"high": 15, "medium": 8, "low": 2}
    cap = float((params.get("offramp") or {}).get("cap", 100))

    for case in cases:
        case_id = case.get("case_id", "?")
        summary = case.get("summary") or {}
        for key in ("nodes", "edges", "mixer_pools", "links", "offramps", "green", "yellow", "red", "high_links", "distinct_rank1"):
            if not isinstance(summary.get(key), int):
                problems.append(f"[{case_id}] summary.{key} 必须是整数")
        if summary.get("green", 0) + summary.get("yellow", 0) + summary.get("red", 0) != summary.get("offramps", -1):
            problems.append(f"[{case_id}] 信号灯计数与出口数不一致")

        # v0.2.1 规格 C11：冲突清单与候选标记必须互相自洽（存在才校验）
        conflicts = case.get("conflicts")
        if conflicts is not None:
            listed = {
                item.get("withdrawal_edge")
                for item in (conflicts.get("contested_withdrawals") or [])
            }
            for item in conflicts.get("contested_withdrawals") or []:
                if (item.get("claimant_count") or 0) < 2:
                    problems.append(
                        f"[{case_id}] 冲突 {item.get('withdrawal_edge')} 的指认方少于 2，不应列为冲突"
                    )
                if item.get("resolution") not in ("unresolved", "resolved_manual"):
                    problems.append(
                        f"[{case_id}] 冲突 {item.get('withdrawal_edge')} 的 resolution 不在枚举内"
                    )
            for link in case.get("links") or []:
                if link.get("contested") and link.get("withdrawal_edge") not in listed:
                    problems.append(
                        f"[{case_id}] 归因 {link.get('link_id')} 标为 contested，但冲突清单未列出其出金"
                    )
            if (conflicts.get("contested_count") or 0) != len(listed):
                problems.append(f"[{case_id}] conflicts.contested_count 与清单长度不一致")
            if isinstance(summary.get("contested_withdrawals"), int) and \
                    summary["contested_withdrawals"] != (conflicts.get("contested_count") or 0):
                problems.append(f"[{case_id}] summary.contested_withdrawals 与冲突清单不一致")

        # 规格 C8：veto 的两类阻断语义必须分开，且 B 类不得把案件判成不可执行
        veto = case.get("veto")
        if veto is not None:
            if veto.get("case_level_executable") not in ("YES", "NO", "UNDETERMINED"):
                problems.append(f"[{case_id}] veto.case_level_executable 不在枚举内")
            eb = veto.get("evidence_block") or {}
            ab = veto.get("asset_block") or {}
            if eb.get("triggered") and veto.get("case_level_executable") == "NO":
                problems.append(
                    f"[{case_id}] 证据链阻断（B 类）不得推导为案件级不可执行"
                )
            # V0.2.2：多出口分别裁决后新增 PARTIAL / MIXED_BLOCKED 两个聚合口径
            if ab.get("status") not in ("CONFIRMED_OK", "DENIED", "UNKNOWN",
                                        "NOT_EVALUATED", "PARTIAL", "MIXED_BLOCKED"):
                problems.append(f"[{case_id}] veto.asset_block.status 不在枚举内")

        for pool in case.get("mixer_pools") or []:
            pid = pool.get("pool_id", "?")
            if pool.get("mechanism") not in MECHANISMS + LEGACY_MECHANISMS:
                problems.append(f"[{case_id}] 池 {pid} 的 mechanism 不在枚举内")
            _check_range(pool.get("detection_score"), f"[{case_id}] 池 {pid}.detection_score", problems)
            signals = pool.get("signals") or []
            if len(signals) < 4:
                problems.append(f"[{case_id}] 池 {pid} 的信号少于 4 条")
            for signal in signals:
                for key in ("id", "label", "value", "threshold", "weight", "hit", "detail"):
                    if key not in signal:
                        problems.append(f"[{case_id}] 池 {pid} 的信号 {signal.get('id')} 缺少 {key}")
                if not isinstance(signal.get("hit"), bool):
                    problems.append(f"[{case_id}] 池 {pid} 的信号 {signal.get('id')}.hit 必须是布尔值")
                if not isinstance(signal.get("detail"), str) or not signal.get("detail"):
                    problems.append(f"[{case_id}] 池 {pid} 的信号 {signal.get('id')}.detail 不能为空")
            recomputed = min(100, sum(s["weight"] for s in signals if s.get("hit")))
            if isinstance(pool.get("detection_score"), (int, float)) and abs(recomputed - pool["detection_score"]) > 0.51:
                problems.append(
                    f"[{case_id}] 池 {pid} 的判别分与信号权重之和不一致：{pool['detection_score']} ≠ {recomputed}"
                )
            _check_money(pool.get("inflow"), f"[{case_id}] 池 {pid}.inflow", problems)
            _check_money(pool.get("outflow"), f"[{case_id}] 池 {pid}.outflow", problems)

        link_deposits: dict[str, int] = {}
        for link in case.get("links") or []:
            lid = link.get("link_id", "?")
            for key in ("link_id", "pool_id", "deposit_edge", "withdrawal_edge", "score", "tier", "rank", "features", "explain"):
                if key not in link:
                    problems.append(f"[{case_id}] 归因 {lid} 缺少 {key}")
            _check_range(link.get("score"), f"[{case_id}] 归因 {lid}.score", problems)
            if link.get("tier") not in TIERS:
                problems.append(f"[{case_id}] 归因 {lid}.tier 不在枚举内")
            else:
                expected = attribution.tier_of(float(link.get("score", 0)), tier_params)
                if expected != link["tier"]:
                    problems.append(f"[{case_id}] 归因 {lid}.tier 与分数不符：{link['tier']} ≠ {expected}")
            if not isinstance(link.get("rank"), int) or link.get("rank", 0) < 1:
                problems.append(f"[{case_id}] 归因 {lid}.rank 必须是 ≥1 的整数")
            features = link.get("features") or {}
            if set(features) != set(FEATURE_KEYS):
                problems.append(f"[{case_id}] 归因 {lid}.features 的键必须固定为六维")
            else:
                for key in FEATURE_KEYS:
                    item = features[key]
                    for sub in ("value", "weight", "detail"):
                        if sub not in item:
                            problems.append(f"[{case_id}] 归因 {lid}.features.{key} 缺少 {sub}")
                    _check_range(item.get("value"), f"[{case_id}] 归因 {lid}.features.{key}.value", problems)
                    if not isinstance(item.get("detail"), str) or not item.get("detail"):
                        problems.append(f"[{case_id}] 归因 {lid}.features.{key}.detail 不能为空")
                recomputed = round(
                    sum(float(features[k]["value"]) * float(features[k]["weight"]) for k in FEATURE_KEYS) / 100.0,
                    2,
                )
                if abs(recomputed - float(link.get("score", -1))) > 0.011:
                    problems.append(
                        f"[{case_id}] 归因 {lid} 的分数与六维加权不一致：{link.get('score')} ≠ {recomputed}"
                    )
            explain = link.get("explain")
            if not isinstance(explain, list) or not (2 <= len(explain) <= 5):
                problems.append(f"[{case_id}] 归因 {lid}.explain 必须是 2–5 条")
            if not isinstance(link.get("ground_truth_hit"), bool):
                problems.append(f"[{case_id}] 归因 {lid}.ground_truth_hit 必须是布尔值")
            # v0.2.1 新增字段：存在则校验，不存在不报错（向后兼容旧结果文件）
            hyp = link.get("conditional_on_pool_hypothesis")
            if hyp is not None:
                if not isinstance(hyp, dict):
                    problems.append(f"[{case_id}] 归因 {lid}.conditional_on_pool_hypothesis 必须是对象")
                else:
                    for sub in ("pool_id", "mechanism", "assumption", "pool_score_used_in_scoring"):
                        if sub not in hyp:
                            problems.append(f"[{case_id}] 归因 {lid} 的池假设缺少 {sub}")
                    if hyp.get("pool_score_used_in_scoring") is not False:
                        problems.append(
                            f"[{case_id}] 归因 {lid} 的池假设必须声明判别分不参与打分"
                        )
            aux = (link.get("auxiliary") or {}).get("offramp_reach")
            if aux is not None:
                if aux.get("evidence_domain") != "B":
                    problems.append(f"[{case_id}] 归因 {lid} 的出口可达性必须标为 B 域")
                if aux.get("used_in_scoring") is not False:
                    problems.append(f"[{case_id}] 归因 {lid} 的出口可达性不得参与打分")
            if "contested" in link and not isinstance(link.get("contested"), bool):
                problems.append(f"[{case_id}] 归因 {lid}.contested 必须是布尔值")
            # 规格 C1 / C4：可识别性与质量门槛
            idbl = link.get("identifiability")
            if idbl is not None:
                if idbl.get("decision_status") not in ("ALLOWED", "CONDITIONAL", "WITHHELD"):
                    problems.append(f"[{case_id}] 归因 {lid} 的 decision_status 不在枚举内")
                gate = idbl.get("quality_gate") or {}
                if not isinstance(gate.get("layer1_pass"), bool) or not isinstance(gate.get("layer2_pass"), bool):
                    problems.append(f"[{case_id}] 归因 {lid} 的质量门槛缺少布尔结论")
                # 门槛不过时必须 WITHHELD，不得给出强度结论
                if gate and not (gate.get("layer1_pass") and gate.get("layer2_pass")):
                    if idbl.get("decision_status") != "WITHHELD":
                        problems.append(
                            f"[{case_id}] 归因 {lid} 未通过质量门槛却仍给出 {idbl.get('decision_status')}"
                        )
                if idbl.get("decision_status") == "ALLOWED" and (idbl.get("top_class_size") or 1) != 1:
                    problems.append(f"[{case_id}] 归因 {lid} 声称可唯一定位，但首位等价类大小不为 1")
                expected_eff = "WITHHELD" if idbl.get("decision_status") == "WITHHELD" else link.get("tier")
                if link.get("effective_tier") != expected_eff:
                    problems.append(f"[{case_id}] 归因 {lid} 的 effective_tier 与判定不一致")
                if bool(link.get("rank_withheld")) != (idbl.get("decision_status") == "WITHHELD"):
                    problems.append(f"[{case_id}] 归因 {lid} 的 rank_withheld 与判定不一致")
            # 规格 C9：证据记录不得出现无依据的人工可靠性概率
            ev = link.get("evidence")
            if ev is not None:
                if not isinstance(ev, list) or len(ev) != len(FEATURE_KEYS):
                    problems.append(f"[{case_id}] 归因 {lid} 的证据条数应等于维度数")
                else:
                    for rec in ev:
                        for sub in ("evidence_id", "source_type", "directness",
                                    "verification_status", "reproducible"):
                            if sub not in rec:
                                problems.append(f"[{case_id}] 归因 {lid} 的证据缺少 {sub}")
                        if any(isinstance(v, float) for v in rec.values()):
                            problems.append(f"[{case_id}] 归因 {lid} 的证据出现了数值型可靠性")
            # 规格 C11 第二步：关系类型枚举（存在才校验，兼容旧结果）
            rel = link.get("relation_type")
            if rel is not None:
                if rel not in attribution.RELATION_TYPES:
                    problems.append(f"[{case_id}] 归因 {lid} 的 relation_type 不在枚举内")
                # 未标注的关系只能是 unresolved，不得凭空给出一对一之类
                if rel != "unresolved" and not link.get("relation_resolved_by"):
                    problems.append(
                        f"[{case_id}] 归因 {lid} 给出了 {rel} 却没有人工作出该判断的来源"
                    )
            link_deposits[link.get("deposit_edge")] = link_deposits.get(link.get("deposit_edge"), 0) + 1

        for deposit_id in link_deposits:
            rows = [link for link in case["links"] if link.get("deposit_edge") == deposit_id]
            for row in rows:
                # 标准竞赛排名：名次 = 1 + 分数严格高于本行的人数（同分同名次）
                expected = 1 + len(
                    [other for other in rows if round(float(other["score"]), 2) > round(float(row["score"]), 2)]
                )
                if row.get("rank") != expected:
                    problems.append(
                        f"[{case_id}] 入金 {deposit_id} 的 {row.get('link_id')} 名次应为 {expected}（同分同名次），实际 {row.get('rank')}"
                    )

        for off in case.get("offramps") or []:
            nid = off.get("node_id", "?")
            if off.get("offramp_type") not in OFFRAMP_TYPE_ENUM:
                problems.append(f"[{case_id}] 出口 {nid} 的 offramp_type 不在枚举内")
            _check_range(off.get("s_off"), f"[{case_id}] 出口 {nid}.s_off", problems)
            hits = off.get("hits")
            if not isinstance(hits, list) or not hits:
                problems.append(f"[{case_id}] 出口 {nid}.hits 不能为空")
            else:
                total = sum(float(h.get("score", 0)) for h in hits)
                if abs(min(cap, total) - float(off.get("s_off", -1))) > 0.11:
                    problems.append(f"[{case_id}] 出口 {nid} 的 s_off 与规则分值之和不一致")
                for hit in hits:
                    for key in ("rule_id", "label", "score", "explain"):
                        if key not in hit:
                            problems.append(f"[{case_id}] 出口 {nid} 的命中规则缺少 {key}")
            _check_money(off.get("inflow_total"), f"[{case_id}] 出口 {nid}.inflow_total", problems)
            _check_money(off.get("from_mixer"), f"[{case_id}] 出口 {nid}.from_mixer", problems)
            if not isinstance(off.get("deposit_count"), int):
                problems.append(f"[{case_id}] 出口 {nid}.deposit_count 必须是整数")
            _check_range(off.get("best_link_score"), f"[{case_id}] 出口 {nid}.best_link_score", problems)
            control = off.get("controllability") or {}
            for key in ("s_profile", "s_off", "c", "p", "s_ctrl", "level", "profile", "main_constraints", "freeze_window_hours"):
                if key not in control:
                    problems.append(f"[{case_id}] 出口 {nid}.controllability 缺少 {key}")
            profile = control.get("profile") or {}
            for key in (*controllability.PROFILE_WEIGHTS, "priority_bonus"):
                if key not in profile:
                    problems.append(f"[{case_id}] 出口 {nid}.controllability.profile 缺少 {key}")
            if all(k in control for k in ("s_profile", "c", "p", "s_ctrl")):
                expected = (
                    alpha * float(control["s_profile"])
                    + beta * float(off.get("s_off", 0))
                    + gamma * float(control["c"])
                    - float(control["p"])
                )
                expected = max(0.0, min(100.0, expected))
                if abs(expected - float(control["s_ctrl"])) > 0.11:
                    problems.append(
                        f"[{case_id}] 出口 {nid} 的 s_ctrl 与公式不符：{control['s_ctrl']} ≠ {expected:.1f}"
                    )
                expected_level = controllability.level_of(float(control["s_ctrl"]), signal_params)
                if expected_level != control["level"]:
                    problems.append(
                        f"[{case_id}] 出口 {nid} 的信号灯与阈值不符：{control['level']} ≠ {expected_level}"
                    )
                if float(control["p"]) not in {float(v) for v in penalty.values()}:
                    problems.append(f"[{case_id}] 出口 {nid}.controllability.p 不在罚分表内")

            # 规格 C7：五维台账校验（旧结果文件没有该字段时不报错）
            book = off.get("ledger")
            if book is not None:
                dims = book.get("dimensions") or {}
                if set(dims) != set(ledger.DIMENSIONS):
                    problems.append(f"[{case_id}] 出口 {nid} 的台账维度必须固定为五维")
                else:
                    known = {r.get("evidence_id") for r in (off.get("evidence") or [])}
                    for name, rec in dims.items():
                        for sub in ("value", "status", "source", "observed_at",
                                    "updated_at", "verified_by", "history"):
                            if sub not in rec:
                                problems.append(f"[{case_id}] 台账 {nid}.{name} 缺少 {sub}")
                        if rec.get("status") not in ledger.STATUSES:
                            problems.append(f"[{case_id}] 台账 {nid}.{name}.status 不在枚举内")
                        # null 不得被填成 0
                        if rec.get("status") == "UNKNOWN" and rec.get("value") is not None:
                            problems.append(f"[{case_id}] 台账 {nid}.{name} 为 UNKNOWN 却带了值")
                        # 后三维不得由系统赋值
                        if name in ledger.EXTERNAL_DIMENSIONS:
                            if rec.get("status") == "CONFIRMED" and rec.get("verified_by") in (None, "algorithm"):
                                problems.append(
                                    f"[{case_id}] 台账 {nid}.{name} 属外部维度，不能由系统确认"
                                )
                        if not isinstance(rec.get("history"), list):
                            problems.append(f"[{case_id}] 台账 {nid}.{name}.history 必须是列表")
                        # 规格 C9：台账维度引用的证据必须真实存在
                        for eid in rec.get("evidence_ids") or []:
                            if eid not in known:
                                problems.append(
                                    f"[{case_id}] 台账 {nid}.{name} 引用了不存在的证据 {eid}"
                                )

        for plan in case.get("paths") or []:
            pid = plan.get("path_id", "?")
            steps = plan.get("steps")
            if not isinstance(steps, list) or not steps:
                problems.append(f"[{case_id}] 路径 {pid}.steps 不能为空")
            else:
                for index, step in enumerate(steps, start=1):
                    if step.get("order") != index:
                        problems.append(f"[{case_id}] 路径 {pid} 的步骤序号不连续")
                    for key in ("node_id", "action"):
                        if not step.get(key):
                            problems.append(f"[{case_id}] 路径 {pid} 步骤 {index} 缺少 {key}")
                truth_case = truth_cases.get(case_id)
                if truth_case is not None and truth_case.ground_truth:
                    # 路径只许走本案的边：外部用户噪声边不得出现，起点必须是涉案源
                    noise = {str(x) for x in (truth_case.ground_truth.get("noise_edges") or [])}
                    used = {step.get("edge_id") for step in steps if step.get("edge_id")}
                    leaked = sorted(used & noise)
                    if leaked:
                        problems.append(f"[{case_id}] 路径 {pid} 使用了外部用户噪声边：{leaked}")
                    sources = {n.id for n in truth_case.nodes.values() if paths.SOURCE_TAG in n.tags}
                    if sources and steps[0].get("node_id") not in sources:
                        problems.append(
                            f"[{case_id}] 路径 {pid} 的起点 {steps[0].get('node_id')} 不是涉案源 {sorted(sources)}"
                        )
            if "differentiator" in plan and (
                not isinstance(plan["differentiator"], str) or not plan["differentiator"].strip()
            ):
                problems.append(f"[{case_id}] 路径 {pid}.differentiator 不能为空")
            _check_range(plan.get("score"), f"[{case_id}] 路径 {pid}.score", problems)
            if plan.get("cost_level") not in COST_LEVELS:
                problems.append(f"[{case_id}] 路径 {pid}.cost_level 不在枚举内")
            if not isinstance(plan.get("estimated_hours"), (int, float)):
                problems.append(f"[{case_id}] 路径 {pid}.estimated_hours 必须是数字")
            for key in ("prerequisites", "risks"):
                value = plan.get(key)
                if not isinstance(value, list) or not value:
                    problems.append(f"[{case_id}] 路径 {pid}.{key} 至少要有一条")
            if plan.get("fallback_path_id") is not None and plan.get("fallback_path_id") not in {
                p.get("path_id") for p in case.get("paths") or []
            }:
                problems.append(f"[{case_id}] 路径 {pid}.fallback_path_id 指向不存在的路径")

        metrics = case.get("metrics") or {}
        attribution_metrics = metrics.get("attribution")
        if attribution_metrics is not None:
            if attribution_metrics.get("competition") not in ("零竞争条件", "含竞争条件"):
                problems.append(f"[{case_id}] metrics.competition 必须是「零竞争条件」或「含竞争条件」")
            if attribution_metrics.get("data") != "合成数据":
                problems.append(f"[{case_id}] metrics.data 必须标注「合成数据」")
            for key in ("p_at_1", "p_at_3", "mrr"):
                _check_range(attribution_metrics.get(key), f"[{case_id}] metrics.{key}", problems, 0.0, 1.0)

    if dataset is not None:
        if sha256(dataset.raw) != result.get("input_sha256"):
            problems.append("input_sha256 与输入数据集不一致")
        case_ids = [c.get("case_id") for c in cases]
        if case_ids != [c.case_id for c in dataset.cases]:
            problems.append("结果中的案件与输入不一致（数量或顺序）")
    return problems


def check_result(result: dict, dataset: Dataset | None = None) -> None:
    problems = validate_result(result, dataset)
    if problems:
        raise InputError("结果未通过结构校验：" + "；".join(problems[:8]))


def write_json(obj: Any, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target

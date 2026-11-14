"""断链归因：把同一混币池的一笔入金与一笔出金重新配对。

六维特征各归一到 0–100，按契约给定的权重加权：
    score = Σ(value × weight) / 100      （权重合计 100，故 score ∈ [0,100]）
    tier  = HIGH ≥70 / MEDIUM ≥45 / LOW <45

每个特征都带 detail，且 detail 里的数字与 value 同源，便于人工复核。
入金与出金数量不等时按笛卡尔积全配对（一对多），每条都带 rank 与 tier ——
这是契约要求的形态，也如实反映"混币后本来就没有唯一解"。
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from decimal import Decimal

from .graph import Case, Edge, InputError, ZERO, money_text
from .mixer_detect import cluster_amounts

FEATURES = ("amount", "time", "denomination", "path_rarity", "cooccurrence", "fee_fit")

FEATURE_LABELS = {
    "amount": "金额",
    "time": "时间",
    "denomination": "面额",
    "path_rarity": "链上位置",
    "cooccurrence": "批次共现",
    "fee_fit": "费率",
}

# 服务抽成区间：与 mixer_detect 保持一致（0.01%–3%）
FEE_BAND = (Decimal("0.0001"), Decimal("0.03"))
# 认为"入出金同量级"的容差：3%（超过就不是同一笔资金，而是拆分或找零）
SAME_SCALE = Decimal("0.03")
# 找零判定：出金不足入金的 20%
CHANGE_SHARE = Decimal("0.20")
# 入金可支撑出金的持有上限（小时）：超过视为跨轮次，共现证据弱
HOLD_LIMIT_HOURS = 240.0
# 出金下游继续流向法币出口的观察跳数（仅用于 auxiliary 展示，不参与打分）
DOWNSTREAM_HOPS = 2


def default_params() -> dict:
    return {
        "weights": {
            "amount": 30,
            "time": 20,
            "denomination": 15,
            "path_rarity": 15,
            "cooccurrence": 12,
            "fee_fit": 8,
        },
        "tier": {"high": 70, "medium": 45},
        # 规格 C4 双重门槛。全部标 UNVERIFIED：数值来自评估集观测，非真实标定。
        "gate": {
            "min_coverage": 0.45,
            "min_dims": 3,
            "min_independent": 2,
            "rho_max": 0.9,
            "params_status": "UNVERIFIED",
        },
        # 规格 C1 可识别性：声明用哪些特征、什么容差划等价类。
        "identifiability": {
            "partition_features": ["amount"],
            "tolerance_source": "mixer.equal_output_tolerance",
            "delta_rank": None,          # 仅作描述，不参与判定
            "delta_rank_source": "未指定",
        },
    }


# 已实测高度共线的特征对（规格 C4 第二层）。相关系数见
# IDENTIFIABILITY_THEORY_REVIEW.md 与 04_实施/批次1对比.json。
# 声明为常量而不是每次从数据估，是因为生产环境没有“每个案件都重估相关”的条件；
# 代价是这份知识随数据变化会过期，故标注 UNVERIFIED。
COLLAPSED_FEATURE_PAIRS = (("amount", "fee_fit"),)


def quality_gate(features: dict[str, dict], weights: dict, gate: dict) -> dict:
    """规格 C4：数据充分性 + 证据独立性双重门槛。

    第二层是关键：三维有效数据不等于三份独立证据。
    `independent_evidence = effective_dims − 已共线的对数`，
    再把重复表达的维度按一份计入。
    """
    present = [k for k in FEATURES if features.get(k, {}).get("value") is not None]
    covered = sum(float(weights[k]) for k in present)
    coverage = covered / 100.0
    effective_dims = len(present)

    collapsed = [
        pair for pair in COLLAPSED_FEATURE_PAIRS
        if pair[0] in present and pair[1] in present
    ]
    independent = max(0, effective_dims - len(collapsed))

    layer1 = (
        coverage >= float(gate["min_coverage"])
        and effective_dims >= int(gate["min_dims"])
    )
    layer2 = independent >= int(gate["min_independent"])
    return {
        "effective_dims": effective_dims,
        "coverage": round(coverage, 4),
        "missing_ratio": round((len(FEATURES) - effective_dims) / len(FEATURES), 4),
        "collapsed_pairs": [list(pair) for pair in collapsed],
        "independent_evidence": independent,
        "layer1_pass": bool(layer1),
        "layer2_pass": bool(layer2),
        "params_status": gate.get("params_status", "UNVERIFIED"),
    }


def partition_by_amount(withdrawals: list[Edge], tolerance: Decimal) -> list[list[str]]:
    """按金额等额把候选出金划成等价类（规格 C1）。

    为什么用金额：连续特征下"六维完全相等"几乎不出现，判据会沦为恒真
    （实测 31 笔无一触发）。金额是被量化的一维，"等额即不可辨"有明确物理含义。

    划类**不依赖真值**：只看候选集自身，否则会把答案泄漏进可识别性判断。
    """
    groups: list[list[str]] = []
    used: set[str] = set()
    ordered = sorted(withdrawals, key=lambda e: (e.amount, e.id))
    for edge in ordered:
        if edge.id in used:
            continue
        anchor = abs(edge.amount) if edge.amount != ZERO else Decimal(1)
        group = [
            other.id for other in ordered
            if other.id not in used and abs(other.amount - edge.amount) <= tolerance * anchor
        ]
        for member in group:
            used.add(member)
        groups.append(group)
    return groups


def identifiability_of(
    groups: list[list[str]],
    candidates: list[dict],
    gate: dict,
) -> dict:
    """规格 C1：三层判定。返回每个候选都能用的可识别性块。

    T-06 的解法：判据落在「被报出的那一条所属等价类的大小」上，而不是笼统的 K。
    `K < N` 只说明候选集里还有并列，不代表这次的答案不唯一。
    """
    class_size = {member: len(g) for g in groups for member in g}
    n_candidates = len(candidates)
    top = [c for c in candidates if c["rank"] == 1]
    top_class_sizes = {class_size.get(c["withdrawal_edge"], 1) for c in top}
    n_top = max(top_class_sizes) if top_class_sizes else n_candidates

    layer_pass = bool(gate["layer1_pass"] and gate["layer2_pass"])
    live = [c for c in candidates if c.get("evidence_state") != "REVOKED"]

    # —— 六态判定（规格 DATA_QUALITY_CONTRACT §4）——
    # 顺序即优先级：关键字段缺失 > 证据冲突 > 无可用证据 > 门槛不过 > 全等价 > 唯一/并列
    critical = sorted({f for c in live for f in (c.get("missing_critical_fields") or [])})
    conflicted = [c for c in live if c.get("evidence_conflict_count", 0) > 0]
    if critical:
        istatus, code = "WITHHELD", "CRITICAL_FIELD_MISSING"
        reason = f"关键字段缺失（{', '.join(critical)}），不输出归因分与名次"
    elif conflicted:
        istatus, code = "CONFLICTED", "EVIDENCE_CONFLICT"
        reason = "存在相互矛盾的证据，必须先解决冲突再下结论"
    elif _effective_dimensions(gate) == 0:
        istatus, code = "INSUFFICIENT", "NO_USABLE_EVIDENCE"
        reason = "没有任何可用证据维度"
    elif not layer_pass:
        istatus, code = "WITHHELD", "GATE_FAILED"
        reason = "未通过 C4 双重门槛（数据充分性/证据独立性），不允许给出强度结论"
    elif n_top == 1:
        istatus, code = "SUPPORTED", "UNIQUE_TOP"
        reason = "首位候选在声明的特征子集上自成一类，可唯一定位"
    elif n_top >= max(1, n_candidates):
        istatus, code = "UNIDENTIFIABLE", "ALL_EQUIVALENT"
        reason = "全部候选在同一等价类内，结构上无法区分"
    else:
        istatus, code = "CONDITIONAL", "AMOUNT_TIE"
        reason = f"首位候选与其他 {n_top - 1} 个候选在声明特征上不可区分，必须并列列出"

    # 旧字段由新状态映射而来（规格 EVIDENCE_SCHEMA §x-chaintrace-mapping）
    status = LEGACY_FROM_IDENTIFIABILITY[istatus]

    return {
        "information_set": "I_onchain",
        "partition_features": ["amount"],
        "candidate_count": n_candidates,
        "equivalence_classes": len(groups),
        "top_class_size": n_top,
        "structurally_identifiable": len(groups) == n_candidates,
        "decision_status": status,
        "identifiability_status": istatus,
        "reason_code": code,
        "reason": reason,
        "quality_gate": gate,
    }


def _effective_dimensions(gate: dict) -> int | None:
    """读有效维度数。v0.2.1 的键叫 effective_dims，V0.2.2 新增 effective_dimensions，
    两者都要认；都缺则返回 None（表示"未知"，不当作 0）。"""
    for key in ("effective_dimensions", "effective_dims"):
        if key in gate:
            try:
                return int(gate[key])
            except (TypeError, ValueError):
                return None
    return None


def tier_of(score: float, tier: dict) -> str:
    """分档。边界值归属上档：70 → HIGH，45 → MEDIUM。"""
    if score >= float(tier["high"]):
        return "HIGH"
    if score >= float(tier["medium"]):
        return "MEDIUM"
    return "LOW"


def _hours(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _amount_feature(deposit: Edge, withdrawal: Edge) -> tuple[float, str]:
    if deposit.amount == ZERO:
        return 0.0, "入金为 0，金额维度无法评估"
    ratio = (deposit.amount - withdrawal.amount) / deposit.amount
    pct = float(ratio * 100)
    low, high = FEE_BAND
    if low <= ratio <= high:
        # 抽成区间内：越接近区间下沿越贴合（大额抽成同时意味着更大的金额缺口）
        value = 100.0 - 15.0 * float(ratio / high)
        detail = (
            f"入金 {money_text(deposit.amount)} 与出金 {money_text(withdrawal.amount)} 差额 {pct:.2f}%，"
            "落在混币池抽成区间"
        )
    elif ratio > high:
        # 超出抽成区间后按指数衰减：8% 的缺口已经不是"同一笔钱"，不能用线性斜坡糊过去
        value = 85.0 * math.exp(-float((ratio - high) / Decimal("0.05")))
        detail = (
            f"入金 {money_text(deposit.amount)} 与出金 {money_text(withdrawal.amount)} 差额 {pct:.2f}%，"
            "超出抽成区间，更像拆分或找零"
        )
    else:
        value = _clamp(100.0 + 300.0 * float(ratio))
        detail = (
            f"入金 {money_text(deposit.amount)} 小于出金 {money_text(withdrawal.amount)}（差额 {pct:.2f}%），"
            "金额关系不成立"
        )
    return _clamp(value), detail


def _time_feature(deposit: Edge, withdrawal: Edge) -> tuple[float, str]:
    hours = _hours(withdrawal.at, deposit.at)
    if hours < 0:
        value = _clamp(60.0 + 2.0 * hours)
        detail = f"出金早于入金 {abs(hours):.1f} 小时，时序不成立"
    elif hours <= 72.0:
        value = 100.0 - 15.0 * (hours / 72.0)
        detail = f"入金后 {hours:.1f} 小时转出，处于混币服务常见的延迟区间之内"
    elif hours <= 240.0:
        value = 85.0 - 45.0 * ((hours - 72.0) / 168.0)
        detail = f"入金后 {hours:.1f} 小时转出，延迟偏长但仍在可解释范围内"
    else:
        value = max(5.0, 40.0 - 30.0 * ((hours - 240.0) / 336.0))
        detail = f"入金后 {hours:.1f} 小时转出，间隔过长，时间维度证据弱"
    return _clamp(value), detail


def _denomination_feature(
    deposit: Edge, withdrawal: Edge, clusters: dict[str, int], tolerance: Decimal
) -> tuple[float, str]:
    size = clusters.get(withdrawal.id, 1)
    ratio = (deposit.amount - withdrawal.amount) / deposit.amount if deposit.amount > ZERO else Decimal(1)
    same_scale = abs(ratio) <= SAME_SCALE
    if size >= 2 and same_scale:
        value = 100.0
        detail = f"出金与入金同量级，且属于 {size} 笔一组的等额切片（容差 {float(tolerance) * 100:.2f}%）"
    elif size >= 2:
        value = 70.0
        detail = f"出金属于 {size} 笔一组的等额切片，但与入金金额不同量级"
    elif deposit.amount > ZERO and withdrawal.amount < deposit.amount * CHANGE_SHARE:
        value = 8.0
        detail = f"出金仅为入金的 {float(withdrawal.amount / deposit.amount) * 100:.1f}%，按找零处理"
    else:
        value = 30.0
        detail = "出金为单独面额，不属任何等额切片"
    return _clamp(value), detail


def _downstream_fanout(case: Case, start: str, hops: int = DOWNSTREAM_HOPS) -> int:
    """从 start 出发在 hops 跳内可达的不同地址数（纯拓扑，不含任何标签语义）。"""
    seen = {start}
    frontier = [start]
    for _ in range(hops):
        nxt: list[str] = []
        for node_id in frontier:
            for edge in case.out_edges(node_id):
                if edge.dst not in seen and case.node(edge.dst).type != "mixer":
                    seen.add(edge.dst)
                    nxt.append(edge.dst)
        frontier = nxt
        if not frontier:
            break
    return max(0, len(seen) - 1)


def _path_rarity_feature(case: Case, withdrawal: Edge) -> tuple[float, str]:
    """链上位置维度（v0.2.1 起**只用 A 域纯链上结构证据**）。

    规格 C3 要求：纯链上归因排序不得引用链下出口标签。原实现含
    `+45 if 可达出口` 一项，而出口类型来自节点标签（B 域），构成与出口识别的
    潜在环路，故切断。出口可达性改记在 auxiliary.offramp_reach，不参与打分。

    只用三件事：出金地址入度、是否一次性地址、下游扇出。
    """
    dst = withdrawal.dst
    indegree = len(case.in_edges(dst))
    one_time = indegree == 1
    fanout = _downstream_fanout(case, dst)
    value = 20.0 + (30.0 if one_time else 0.0) + 25.0 / (1 + max(0, indegree - 1))
    value += 25.0 * min(1.0, fanout / 3.0)
    detail = (
        f"出金地址 {dst} 的入度为 {indegree}"
        f"{'（一次性地址）' if one_time else ''}，"
        f"{DOWNSTREAM_HOPS} 跳内可继续到达 {fanout} 个地址；"
        "仅计入链上结构，不含出口标签"
    )
    return _clamp(value), detail


def offramp_reach_aux(case: Case, withdrawal_id: str, reached: str | None) -> dict:
    """出口可达性的记录形态：**B 域**，只作展示，不参与任何打分。

    实际的可达性判定在出口识别模块里（那里才允许引用出口类型标签）；
    本函数只负责把它包成带域标注的字段，供 pipeline 附加到候选上。
    """
    return {
        "evidence_domain": "B",
        "label_dependent": True,
        "reached_node": reached,
        "reached_label": case.node(reached).label if reached else None,
        "used_in_scoring": False,
        "note": "依赖 node.type 标签；按规格 C3 不得参与 I_onchain 口径的排序",
    }


def withdrawal_rounds(withdrawals: list[Edge], batch_window_hours: float) -> list[dict]:
    """按批处理窗口把出金切成轮次；每轮记录起止时间与笔数。"""
    if not withdrawals:
        return []
    ordered = sorted(withdrawals, key=lambda e: (e.at, e.id))
    rounds: list[dict] = [{"start": ordered[0].at, "end": ordered[0].at, "size": 1}]
    for edge in ordered[1:]:
        current = rounds[-1]
        if _hours(edge.at, current["end"]) > batch_window_hours:
            rounds.append({"start": edge.at, "end": edge.at, "size": 1})
        else:
            current["end"] = edge.at
            current["size"] += 1
    return rounds


def _cooccurrence_feature(deposit: Edge, withdrawal: Edge, rounds: list[dict]) -> tuple[float, str]:
    target_round = None
    for item in rounds:
        if item["start"] <= withdrawal.at <= item["end"]:
            target_round = item
            break
    if target_round is None:
        return 40.0, "该出金不落在任何可识别的批次内，共现证据不足"
    hold = _hours(target_round["start"], deposit.at)
    if not (0 <= hold <= HOLD_LIMIT_HOURS):
        return 25.0, f"入金与出金相隔 {abs(hold):.1f} 小时，不属于同一处理轮次"
    size = max(1, target_round["size"])
    value = 100.0 * (1.0 - 0.10 * min(5, size - 1))
    detail = (
        f"入金在出金前 {hold:.1f} 小时到达，与该轮 {size} 笔出金同处一个批次；"
        "同轮笔数越多，单对确定性越低"
    )
    return _clamp(value), detail


def _fee_fit_feature(deposit: Edge, withdrawal: Edge) -> tuple[float, str]:
    if deposit.amount == ZERO:
        return 0.0, "入金为 0，费率维度无法评估"
    ratio = (deposit.amount - withdrawal.amount) / deposit.amount
    low, high = FEE_BAND
    if low <= ratio <= high:
        value = 98.0 - 260.0 * abs(float(ratio) - 0.005)
        detail = f"隐含抽成 {float(ratio) * 100:.2f}%，与混币服务公开费率量级一致"
    elif ratio > high:
        value = 55.0 * math.exp(-float((ratio - high) / Decimal("0.05")))
        detail = f"隐含抽成 {float(ratio) * 100:.2f}%，远超常见费率，费率维度不成立"
    else:
        value = max(0.0, 40.0 + 200.0 * float(ratio))
        detail = f"出金高于入金（隐含费率 {float(ratio) * 100:.2f}%），费率维度不成立"
    return _clamp(value), detail


def fee_ratio(deposit: Edge, withdrawal: Edge) -> float | None:
    """隐含抽成比例 (入金−出金)/入金。规格 C5 要求保留原始计算值。"""
    if deposit.amount == ZERO:
        return None
    return float((deposit.amount - withdrawal.amount) / deposit.amount)


def link_features(
    case: Case,
    deposit: Edge,
    withdrawal: Edge,
    clusters: dict[str, int],
    rounds: list[dict],
    tolerance: Decimal,
) -> dict[str, tuple[float, str]]:
    """计算一对（入金, 出金）的六维原始值与说明。"""
    return {
        "amount": _amount_feature(deposit, withdrawal),
        "time": _time_feature(deposit, withdrawal),
        "denomination": _denomination_feature(deposit, withdrawal, clusters, tolerance),
        "path_rarity": _path_rarity_feature(case, withdrawal),
        "cooccurrence": _cooccurrence_feature(deposit, withdrawal, rounds),
        "fee_fit": _fee_fit_feature(deposit, withdrawal),
    }


# 规格 C11 第二步：资金流关系类型。
# **只建立类型，不推断类型**——在没有证明入金与出金满足一对一约束之前，
# 一律输出 unresolved。谁能给出非 unresolved？只有人工标注（见 relation_resolved_by）。
RELATION_TYPES = (
    "one_to_one",
    "one_to_many",
    "many_to_one",
    "many_to_many",
    "unresolved",
)

DEFAULT_RELATION = "unresolved"

# 六态 → 旧三态（规格 EVIDENCE_SCHEMA_V022 §x-chaintrace-mapping）。
# 旧消费者只认 ALLOWED/CONDITIONAL/WITHHELD，行为保持不变。
LEGACY_FROM_IDENTIFIABILITY = {
    "SUPPORTED": "ALLOWED",
    "CONDITIONAL": "CONDITIONAL",
    "CONFLICTED": "WITHHELD",
    "INSUFFICIENT": "WITHHELD",
    "UNIDENTIFIABLE": "CONDITIONAL",
    "WITHHELD": "WITHHELD",
}
IDENTIFIABILITY_STATES = tuple(LEGACY_FROM_IDENTIFIABILITY)


def relation_of(link: dict) -> dict:
    """返回一条候选的关系类型。默认 unresolved，且说明为什么不是别的值。

    为什么不自动判：一笔出金可以对应一笔入金，也可能对应合并出金或拆分入金；
    这些都要靠金额守恒、笔数配比或链下材料证明。仅凭"这是一个池"推不出一对一，
    所以这里只给占位值，把判断权留给人工或后续专项验证（C11 第三步）。
    """
    resolved_by = link.get("relation_resolved_by")
    value = link.get("relation_type") or DEFAULT_RELATION
    if value not in RELATION_TYPES:
        value = DEFAULT_RELATION
    return {
        "relation_type": value,
        "relation_resolved_by": resolved_by,
        "relation_basis": (
            "人工标注" if resolved_by else
            "未证明一对一约束，按规格 C11 默认 unresolved"
        ),
    }


def mark_relations(links: list[dict]) -> None:
    """给每条候选写上关系类型（就地，不改名次与分数）。"""
    for link in links:
        link.update(relation_of(link))


def score_of(features: dict[str, dict]) -> float:
    """按契约公式复算：Σ(value×weight)/100。"""
    total = sum(float(features[key]["value"]) * float(features[key]["weight"]) for key in FEATURES)
    return round(total / 100.0, 2)


def _explain(
    deposit: Edge,
    features: dict[str, dict],
    score: float,
    tier: str,
    rank: int,
    competitors: int,
    tier_params: dict,
    tie_size: int = 1,
) -> list[str]:
    ordered = sorted(
        FEATURES,
        key=lambda key: (-(float(features[key]["value"]) * float(features[key]["weight"])), FEATURES.index(key)),
    )
    lines = [
        f"{FEATURE_LABELS[key]}维度 {float(features[key]['value']):.1f} 分：{features[key]['detail']}"
        for key in ordered[:3]
    ]
    tie_text = f"，与另外 {tie_size - 1} 笔同分并列" if tie_size > 1 else ""
    lines.append(
        f"综合 {score:.2f} 分 → {tier}（HIGH≥{tier_params['high']}／MEDIUM≥{tier_params['medium']}）；"
        f"同一入金 {deposit.id} 共有 {competitors} 个候选出金，本对排名第 {rank}{tie_text}。"
        "同分同名次，名次本身不代表先后。"
        "该对应关系为算法候选，不表示资金转移已确认。"
    )
    return lines


def attribute_pool(case: Case, pool: dict, params: dict,
                   link_evidence: dict[str, list[dict]] | None = None) -> list[dict]:
    """对一个池做全配对归因。params 为顶层 parameters（含 mixer 与 attribution）。"""
    weights = params["attribution"]["weights"]
    tier_params = params["attribution"]["tier"]
    if set(weights) != set(FEATURES):
        raise InputError(f"归因权重必须恰好覆盖六个维度，实际为 {sorted(weights)}")
    total_weight = sum(float(weights[key]) for key in FEATURES)
    if abs(total_weight - 100.0) > 1e-9:
        raise InputError(f"归因权重合计必须为 100，实际为 {total_weight:g}")

    tolerance = Decimal(str(params["mixer"]["equal_output_tolerance"]))
    batch_window = float(params["mixer"]["batch_window_hours"])

    deposits: list[Edge] = pool["_deposits"]
    withdrawals: list[Edge] = pool["_withdrawals"]
    if not deposits or not withdrawals:
        return []

    clusters: dict[str, int] = {}
    for group in cluster_amounts([e.amount for e in withdrawals], tolerance):
        for index in group:
            clusters[withdrawals[index].id] = len(group)
    rounds = withdrawal_rounds(withdrawals, batch_window)
    # 规格 C3：本模块只处理 A 域（纯链上结构）。出口可达性属 B 域，
    # 其判定住在 offramp.py，由 pipeline 在输出阶段附加到 auxiliary 字段。
    # 规格 C2：每条候选都必须声明它依赖的池识别假设。
    # 注意：本模块**不读取池判别分**（判别分由 pipeline 在输出阶段附加到本字段），
    # 因此这里只写池标识与机制，不出现判别分，便于用静态检查锁住「判别分不进打分」。
    pool_hypothesis = {
        "pool_id": pool.get("pool_id"),
        "mechanism": pool.get("mechanism") or "unknown",
        "assumption": "本候选仅在『该节点是混币池』这一假设成立时有意义",
        "pool_score_used_in_scoring": False,
        "note": "池判别分衡量池的结构特征，候选分衡量配对特征，二者不构成置信度乘积",
    }

    produced: list[dict] = []
    for deposit in deposits:
        batch: list[dict] = []
        for withdrawal in withdrawals:
            raw = link_features(case, deposit, withdrawal, clusters, rounds, tolerance)
            features = {
                key: {
                    "value": round(float(raw[key][0]), 1),
                    "weight": weights[key],
                    "detail": raw[key][1],
                }
                for key in FEATURES
            }
            features["fee_fit"]["raw"] = fee_ratio(deposit, withdrawal)
            # V0.2.2：把"这条候选缺了哪些关键字段"带到候选上（放 features 之外，
            # 因为 features 的键必须严格固定为六维）
            missing = sorted({*deposit.missing_critical_fields, *withdrawal.missing_critical_fields})
            batch.append(
                {
                    "pool_id": pool["pool_id"],
                    "deposit_edge": deposit.id,
                    "withdrawal_edge": withdrawal.id,
                    "score": score_of(features),
                    "features": features,
                    "missing_critical_fields": missing,
                    "evidence_conflict_count": 0,
                    "conditional_on_pool_hypothesis": pool_hypothesis,
                }
            )
        batch.sort(key=lambda row: (-row["score"], row["withdrawal_edge"]))
        # 并列名次（标准竞赛排名）：同分同名次，下一档跳到「1 + 前面的人数」，例如 1,1,1,4。
        # 理由：同额批次里若几笔出金的六维证据完全一致，硬排 1/2/3 会让人误以为算法给出了先后顺序。
        ranks: list[int] = []
        previous_score: float | None = None
        previous_rank = 0
        for index, row in enumerate(batch, start=1):
            same = previous_score is not None and abs(row["score"] - previous_score) < 1e-9
            row["rank"] = previous_rank if same else index
            if not same:
                previous_rank = index
            previous_score = row["score"]
            ranks.append(row["rank"])
        tie_sizes = Counter(ranks)
        # 规格 C1 + C4：整个池的可识别性只算一次（划类不依赖入金），
        # 质量门槛逐入金算（缺失情况可能不同）。
        pool_groups = partition_by_amount(withdrawals, tolerance)
        for row in batch:
            row["tier"] = tier_of(row["score"], tier_params)
            row["explain"] = _explain(
                deposit,
                row["features"],
                row["score"],
                row["tier"],
                row["rank"],
                len(batch),
                tier_params,
                tie_sizes[row["rank"]],
            )
        gate = quality_gate(batch[0]["features"], weights, params["attribution"]["gate"])
        idbl = identifiability_of(pool_groups, batch, gate)
        for row in batch:
            # 证据改写放在 pipeline 里（那里才有 link_id），此处只放判定结果
            row["identifiability"] = idbl
            # 兼容性：tier 保持原语义（分数档），新增 effective_tier 承载判定结果，
            # 旧消费者只读 tier 不会因为新枚举而崩。
            row["effective_tier"] = "WITHHELD" if idbl["decision_status"] == "WITHHELD" else row["tier"]
            row["rank_withheld"] = idbl["decision_status"] == "WITHHELD"
        produced.extend(batch)
    return produced


def mark_conflicts(links: list[dict]) -> dict:
    """规格 C11 第一步：只做冲突检测，不覆盖原始逐笔排序。

    冲突定义：同一笔出金被**两笔及以上不同入金**同时选为首选（rank == 1）。
    命中时给相关链打 `contested` 与 `contested_by`，并返回案件级摘要。
    """
    by_withdrawal: dict[str, list[dict]] = {}
    for link in links:
        if link.get("rank") != 1:
            continue
        by_withdrawal.setdefault(link["withdrawal_edge"], []).append(link)

    contested_withdrawals: list[dict] = []
    for withdrawal_id, claimants in by_withdrawal.items():
        deposits = sorted({c["deposit_edge"] for c in claimants})
        if len(deposits) < 2:
            continue
        for link in claimants:
            link["contested"] = True
            link["contested_by"] = [
                {
                    "deposit_edge": c["deposit_edge"],
                    "link_id": c.get("link_id"),
                    "score": c["score"],
                    "tier": c.get("tier"),
                }
                for c in claimants
                if c["deposit_edge"] != link["deposit_edge"]
            ]
        contested_withdrawals.append(
            {
                "withdrawal_edge": withdrawal_id,
                "claimant_deposits": deposits,
                "claimant_count": len(deposits),
                "link_ids": [c.get("link_id") for c in claimants],
                "resolution": "unresolved",
                "note": "一笔出金只可能对应一笔入金；此处并列指认属冲突，需人工裁决或补证",
            }
        )

    for link in links:
        link.setdefault("contested", False)
        link.setdefault("contested_by", [])

    return {
        "contested_withdrawals": sorted(contested_withdrawals, key=lambda x: x["withdrawal_edge"]),
        "contested_count": len(contested_withdrawals),
        "extra_claims": sum(x["claimant_count"] - 1 for x in contested_withdrawals),
        "ordering_preserved": True,
    }


def withheld_deposits(case: Case, pools: list[dict]) -> list[dict]:
    """登记"关键字段缺失、因此无法给出任何候选分"的入金。

    为什么不把它们塞进 links：那需要编造一个分数。分数一旦编出来，
    下游排序、档位、案件级汇总都会把它当真实证据用。
    这里只如实记录"这笔入金无法评估、缺了什么、本可有多少候选"。
    """
    out: list[dict] = []
    pool_ids = {p["pool_id"] for p in pools}
    for edge in case.edges:
        if edge.is_complete:
            continue
        if edge.pattern not in ("mixer_in", "mixer_out"):
            continue
        # 不做池归属过滤：pool 对象只带 pool_id，不含节点 id，无法可靠归属。
        # 宁可多报（消费者可据 node 判断），也不要漏报一笔"从未被评估过"的入金。
        out.append({
            "deposit_edge": edge.id,
            "node": edge.dst if edge.pattern == "mixer_in" else edge.src,
            "missing_critical_fields": list(edge.missing_critical_fields),
            "amount_status": edge.amount_status,
            "time_status": edge.time_status,
            "identifiability_status": "WITHHELD",
            "reason_code": "CRITICAL_FIELD_MISSING",
            "candidate_count": 0,
            "note": ("该笔入金的关键字段缺失，系统不输出归因分与名次；"
                     "补齐后可重新计算"),
        })
    return out


EVIDENCE_SCOPES = ("transaction", "link", "address", "account",
                   "service", "asset", "case")

# 只有这些 scope 能影响逐笔结论（规格 §5.1）
LINK_LEVEL_SCOPES = ("link",)


def is_link_resolving(record: dict, link: dict) -> bool:
    """这条证据能否改写该 link 的可识别性？四条必须同时成立。

    定位方式二选一：subject_link_id（LK 编号）或
    （subject_deposit_edge, subject_withdrawal_edge）边对——后者更稳定，
    因为 LK 编号会随排序变化。
    """
    if record.get("scope") not in LINK_LEVEL_SCOPES:
        return False
    if record.get("verification_status") != "verified":
        return False
    if record.get("directness") != "direct":
        return False
    if record.get("subject_link_id"):
        return record["subject_link_id"] == link.get("link_id")
    d, w = record.get("subject_deposit_edge"), record.get("subject_withdrawal_edge")
    return bool(d and w and d == link.get("deposit_edge") and w == link.get("withdrawal_edge"))


def apply_link_evidence(identifiability: dict, records: list[dict],
                        link: dict) -> dict:
    """按证据调整单条 link 的可识别性。

    返回新的 identifiability 块（不改原对象，避免同一 batch 互相污染）。
    """
    if not records:
        return identifiability
    usable = [r for r in records if is_link_resolving(r, link)]
    ignored = [r for r in records if not is_link_resolving(r, link)]
    out = dict(identifiability)
    out["evidence_applied"] = [r.get("evidence_id") for r in usable]
    out["evidence_ignored"] = [
        {"evidence_id": r.get("evidence_id"),
         "reason": _ignore_reason(r, link)} for r in ignored
    ]
    if not usable:
        return out

    # 有反向证据 → CONFLICTED；否则有正向证据 → SUPPORTED
    if any(r.get("counter_evidence_ids") or r.get("stance") == "against" for r in usable):
        istatus, code = "CONFLICTED", "EVIDENCE_CONFLICT"
        reason = "已核验的逐笔证据之间存在冲突，须先解决冲突"
    else:
        istatus, code = "SUPPORTED", "EXTERNAL_EVIDENCE_CONFIRMED"
        reason = "已有经过核验的逐笔证据直接指向该关联"
    out["identifiability_status"] = istatus
    out["reason_code"] = code
    out["decision_status"] = LEGACY_FROM_IDENTIFIABILITY[istatus]
    out["reason"] = reason
    # 结构等价类大小是客观事实，另存不丢；top_class_size 语义升级为"有效定位结果"，
    # 使既有不变量 ALLOWED ⇒ top_class_size == 1 依然成立。
    if istatus == "SUPPORTED":
        out.setdefault("structural_top_class_size", out.get("top_class_size"))
        out["top_class_size"] = 1
        out["resolved_by_evidence"] = [r.get("evidence_id") for r in usable]
    return out


def _ignore_reason(record: dict, link: dict) -> str:
    if record.get("scope") not in LINK_LEVEL_SCOPES:
        return f"scope={record.get('scope')} 不属于逐笔层级，不能改变逐笔结论"
    if record.get("verification_status") != "verified":
        return "证据未核验，不得作为结论依据"
    if record.get("directness") != "direct":
        return "证据为间接/推断，不足以直接确认逐笔关联"
    if not (record.get("subject_link_id") or record.get("subject_deposit_edge")):
        return "证据未指向本候选"
    return "不满足改写条件"


def attribute_case(case: Case, pools: list[dict], params: dict,
                   link_evidence: dict[str, list[dict]] | None = None) -> list[dict]:
    links: list[dict] = []
    for pool in pools:
        links.extend(attribute_pool(case, pool, params, link_evidence))
    return links

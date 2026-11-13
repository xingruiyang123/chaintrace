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
# 出金下游继续流向法币出口的观察跳数
DOWNSTREAM_HOPS = 2

OFFRAMP_TYPES = ("exchange", "otc", "payment_gateway", "underground_bank", "bridge", "defi")


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
    }


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


def _reaches_offramp(case: Case, start: str, hops: int = DOWNSTREAM_HOPS) -> str | None:
    """从 start 出发（不穿过混币池）在 hops 跳内是否到达法币出口；返回命中的出口 id。"""
    frontier = [start]
    seen = {start}
    for _ in range(hops):
        next_frontier: list[str] = []
        for node_id in frontier:
            for edge in case.out_edges(node_id):
                target = case.node(edge.dst)
                if target.type == "mixer":
                    continue
                if target.type in OFFRAMP_TYPES:
                    return target.id
                if target.id not in seen:
                    seen.add(target.id)
                    next_frontier.append(target.id)
        frontier = next_frontier
        if not frontier:
            break
    return None


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


def _path_rarity_feature(case: Case, withdrawal: Edge) -> tuple[float, str]:
    dst = withdrawal.dst
    indegree = len(case.in_edges(dst))
    offramp = _reaches_offramp(case, dst)
    value = 20.0 + (45.0 if offramp else 0.0) + 25.0 / (1 + max(0, indegree - 1))
    if offramp:
        detail = (
            f"出金地址 {dst} 的入度为 {indegree}，并在 {DOWNSTREAM_HOPS} 跳内继续流入"
            f"{case.node(offramp).label}，仍在本案资金链上"
        )
    else:
        detail = (
            f"出金地址 {dst} 的入度为 {indegree}，{DOWNSTREAM_HOPS} 跳内未继续流向法币出口，"
            "链上位置证据弱"
        )
    return _clamp(value), detail


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


def attribute_pool(case: Case, pool: dict, params: dict) -> list[dict]:
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
            batch.append(
                {
                    "pool_id": pool["pool_id"],
                    "deposit_edge": deposit.id,
                    "withdrawal_edge": withdrawal.id,
                    "score": score_of(features),
                    "features": features,
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
        produced.extend(batch)
    return produced


def attribute_case(case: Case, pools: list[dict], params: dict) -> list[dict]:
    links: list[dict] = []
    for pool in pools:
        links.extend(attribute_pool(case, pool, params))
    return links

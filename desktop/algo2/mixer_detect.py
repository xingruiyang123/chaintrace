"""混币池识别：对候选池逐条给出可复算的信号证据。

判别分口径（契约 §3.2）：
    detection_score = min(100, Σ 命中信号权重)

七个信号的权重与阈值写在 SIGNALS 里，并随每条信号一起输出（value / threshold /
weight / hit / detail），所以"为什么是它"和"为什么不是它"都能在界面上复算。
命中与未命中都输出，未命中的信号同样带 detail。

本模块只做结构推断：节点上的 type='mixer' 只是候选来源之一，不决定分数。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .graph import Case, Dataset, Edge, ZERO, money_text

# 信号权重（合计 126，靠 min(100) 截断；权重越高表示该信号越难被普通地址偶然满足）
SIGNAL_WEIGHTS: dict[str, int] = {
    "M1": 20,  # 多输入聚合
    "M2": 22,  # 等额输出
    "M3": 16,  # 批处理时间窗
    "M4": 14,  # 地址一次性
    "M5": 12,  # 面额标准化
    "M6": 16,  # 入出金不守恒
    "M7": 26,  # 固定面额存提
}

SIGNAL_LABELS: dict[str, str] = {
    "M1": "多输入聚合",
    "M2": "等额输出",
    "M3": "批处理时间窗",
    "M4": "地址一次性",
    "M5": "面额标准化",
    "M6": "入出金不守恒",
    "M7": "固定面额存提",
}

# 服务抽成的合理区间：0.01%–3%。低于下限视为"同额搬运"，高于上限视为拆分或找零。
SERVICE_FEE_BAND = (Decimal("0.0001"), Decimal("0.03"))
# 面额标准化的判据：有效数字不超过 3 位视为整额/短尾数面额。
STANDARD_DENOMINATION_DIGITS = 3
# 面额标准化的命中门槛（占比）。
STANDARD_DENOMINATION_MIN_PCT = Decimal("60")


def default_params() -> dict:
    """契约 §3 的 parameters.mixer：只放契约已列的四个键。"""
    return {
        "min_inputs": 3,
        "equal_output_tolerance": 0.0005,
        "batch_window_hours": 48,
        "min_equal_outputs": 3,
    }


def _hours(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def significant_digits(value: Decimal) -> int:
    """有效数字位数（去掉尾随零），用于判断"整额/标准面额"。"""
    if value == ZERO:
        return 1
    digits = "".join(str(d) for d in value.normalize().as_tuple().digits).rstrip("0")
    return len(digits) or 1


def cluster_amounts(amounts: list[Decimal], tolerance: Decimal) -> list[list[int]]:
    """按相对容差把金额聚成等额组，返回下标分组，按组大小降序。

    用相对容差而非绝对值：5.96 与 6.46 在 0.05% 容差下不会被合并，
    但 0.0005 与 0.0006 这类小面额也不会因为绝对差小而误并。
    """
    groups: list[list[int]] = []
    for index, amount in enumerate(amounts):
        for group in groups:
            reference = amounts[group[0]]
            if reference == ZERO:
                if amount == ZERO:
                    group.append(index)
                    break
            elif abs(amount - reference) <= reference * tolerance:
                group.append(index)
                break
        else:
            groups.append([index])
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups


def _signal(signal_id: str, value, threshold: str, hit: bool, detail: str) -> dict:
    return {
        "id": signal_id,
        "label": SIGNAL_LABELS[signal_id],
        "value": value,
        "threshold": threshold,
        "weight": SIGNAL_WEIGHTS[signal_id],
        "hit": bool(hit),
        "detail": detail,
    }


def _one_time_destinations(case: Case, pool_id: str, withdrawals: list[Edge]) -> int:
    """出金目标地址中"只用一次"的数量。

    判据：该地址只收到这一笔入金，且在收到之前没有作为任何边的起点出现过
    （即不是案内已知活跃地址）。混币服务通常为每笔出金生成新收款地址。
    """
    count = 0
    for edge in withdrawals:
        dst = case.node(edge.dst)
        if len(case.in_edges(dst.id)) != 1:
            continue
        earlier_out = [e for e in case.out_edges(dst.id) if e.at < edge.at]
        if earlier_out:
            continue
        count += 1
    return count


def _rounds(times: list[datetime], batch_window_hours: float) -> int:
    """把时间点按"间隔超过批处理窗口即断轮"聚类，返回轮次数。"""
    if not times:
        return 0
    ordered = sorted(times)
    rounds = 1
    for previous, current in zip(ordered, ordered[1:]):
        if _hours(current, previous) > batch_window_hours:
            rounds += 1
    return rounds


def classify_mechanism(
    signals: list[dict],
    deposits: list[Edge],
    withdrawals: list[Edge],
) -> tuple[str, list[str], list[str]]:
    """按**定义性结构**判定机制，不用总分归类（规格 C6）。

    边界（必须遵守）：
      - 总分只用于「这个池有多像混币」，不用于「属于哪种混币」；
      - 每种机制只列**必要条件**，不列加权证据；
      - 条件不足时返回 unknown / insufficient_evidence，**禁止回退为 coinjoin**。

    返回 (mechanism, 满足条件的机制列表, 判定依据)。
    """
    hit = {s["id"]: bool(s["hit"]) for s in signals}
    satisfied: list[str] = []
    basis: list[str] = []

    # CoinJoin 类：多输入聚合（M1）与等额输出（M2）同时成立——
    # 这两条是定义性结构，单输入也能等额分发（工资/空投），故 M1 不可省。
    if hit.get("M1") and hit.get("M2"):
        satisfied.append("coinjoin")
        basis.append("M1 多输入聚合 且 M2 等额输出 同时成立")

    # 固定面额池：固定面额存提（M7），不要求多输入。
    if hit.get("M7"):
        satisfied.append("fixed_denomination")
        basis.append("M7 固定面额存提成立")

    # 托管式：存在抽成（M6 入出金不守恒）、无等额输出组、且呈**归集**形态
    # （出金笔数不多于入金笔数）。第三条是必需的：
    # 仅凭「有抽成且无等额组」会把正常换汇/支付业务一并算进来（实测 OTC 商户即如此）。
    consolidating = len(withdrawals) <= len(deposits)
    if hit.get("M6") and not hit.get("M2") and consolidating:
        satisfied.append("custodial")
        basis.append(
            f"M6 入出金不守恒成立、无等额输出组，且呈归集形态"
            f"（入金 {len(deposits)} 笔 / 出金 {len(withdrawals)} 笔）"
        )

    if len(satisfied) >= 2:
        return "ambiguous", satisfied, basis + ["同时满足多种机制的必要条件，无法唯一归类"]
    if len(satisfied) == 1:
        return satisfied[0], satisfied, basis
    hit_count = sum(1 for v in hit.values() if v)
    if hit_count < 2:
        return "insufficient_evidence", satisfied, ["命中信号少于 2 条，结构证据不足以归类"]
    return "unknown", satisfied, ["不符合任何已知机制的必要条件"]


def counter_evidence(signals: list[dict], deposits: list[Edge], withdrawals: list[Edge]) -> list[dict]:
    """反证分两类（规格 C6）。

    - mechanism_specific：直接否定某机制的**定义性结构**，可降级机制判定；
    - generic：与混币行为相关但缺乏权重依据的结构特征，**只作解释提示，不扣分**。
    """
    hit = {s["id"]: bool(s["hit"]) for s in signals}
    value = {s["id"]: s["value"] for s in signals}
    hints: list[dict] = []

    sources = {e.src for e in deposits}
    if len(sources) <= 1:
        hints.append({
            "id": "X-COINJOIN-SINGLE-INPUT",
            "scope": "mechanism_specific",
            "label": "单一入金来源",
            "detail": f"入金来源地址仅 {len(sources)} 个，不满足 CoinJoin 的多输入共同签名结构",
            "effect": "blocks_coinjoin",
        })

    if not hit.get("M2"):
        hints.append({
            "id": "X-NO-EQUAL-OUTPUT",
            "scope": "mechanism_specific",
            "label": "无等额输出组",
            "detail": f"最大等额组仅 {value.get('M2')} 笔，不满足等额切片结构",
            "effect": "blocks_coinjoin",
        })

    total_in = sum((e.amount for e in deposits), ZERO)
    total_out = sum((e.amount for e in withdrawals), ZERO)
    if total_in == total_out and total_in != ZERO:
        hints.append({
            "id": "X-CONSERVED",
            "scope": "generic",
            "label": "金额完全守恒",
            "detail": "入金与出金金额完全相等，未见服务抽成，与常见混币服务费率特征不符",
            "effect": "hint_only",
        })

    if hit.get("M3"):
        hints.append({
            "id": "X-BATCH-WINDOW",
            "scope": "generic",
            "label": "批处理时间窗",
            "detail": "出金集中在批处理窗口内，但批量发放类正常业务同样具备该特征",
            "effect": "hint_only",
        })

    if not hit.get("M4"):
        hints.append({
            "id": "X-ADDRESS-REUSE",
            "scope": "generic",
            "label": "地址复用度高",
            "detail": "没有出现一次性出金地址，更接近常规归集或经营收款",
            "effect": "hint_only",
        })

    return hints


def pool_candidates(case: Case, params: dict) -> list[str]:
    """候选池 = 显式标注为 mixer 的节点 + 结构上像混币池的节点。"""
    min_inputs = int(params["min_inputs"])
    candidates: list[str] = []
    for node in case.nodes.values():
        if node.type == "mixer":
            candidates.append(node.id)
            continue
        if node.type != "wallet":
            continue
        deposits = case.in_edges(node.id)
        withdrawals = case.out_edges(node.id)
        if len({e.src for e in deposits}) < min_inputs or len(withdrawals) < 2:
            continue
        touched = deposits + withdrawals
        mixer_like = [e for e in touched if e.pattern in ("mixer_in", "mixer_out")]
        if len(mixer_like) * 2 >= len(touched):
            candidates.append(node.id)
    return sorted(candidates)


def analyse_pool(case: Case, pool_id: str, params: dict) -> dict:
    """算一个池的全部信号与判别分。"""
    pool = case.node(pool_id)
    tolerance = Decimal(str(params["equal_output_tolerance"]))
    batch_window = float(params["batch_window_hours"])
    min_inputs = int(params["min_inputs"])
    min_equal = int(params["min_equal_outputs"])

    deposits = sorted(case.in_edges(pool_id), key=lambda e: (e.at, e.id))
    withdrawals = sorted(case.out_edges(pool_id), key=lambda e: (e.at, e.id))
    inflow = sum((e.amount for e in deposits), ZERO)
    outflow = sum((e.amount for e in withdrawals), ZERO)

    signals: list[dict] = []

    # M1 多输入聚合
    sources = {e.src for e in deposits}
    m1_value = len(sources)
    signals.append(
        _signal(
            "M1",
            m1_value,
            f">={min_inputs}",
            m1_value >= min_inputs,
            f"观测量到的直接入金地址 {m1_value} 个"
            + ("，达到多输入聚合门槛" if m1_value >= min_inputs else f"，低于门槛 {min_inputs} 个"),
        )
    )

    # M2 等额输出
    groups = cluster_amounts([e.amount for e in withdrawals], tolerance)
    largest = groups[0] if groups else []
    m2_value = len(largest)
    if largest:
        sample = withdrawals[largest[0]]
        m2_detail = (
            f"最大等额组 {m2_value} 笔，面额 {money_text(sample.amount)}"
            + ("，达到等额拆分门槛" if m2_value >= min_equal else f"，低于门槛 {min_equal} 笔")
        )
    else:
        m2_detail = "该池没有出金，无法评估等额拆分"
    signals.append(
        _signal("M2", m2_value, f">={min_equal}", m2_value >= min_equal and m2_value > 0, m2_detail)
    )

    # M3 批处理时间窗
    if withdrawals:
        span = _hours(withdrawals[-1].at, withdrawals[0].at)
        m3_hit = span <= batch_window and len(withdrawals) >= 2
        m3_detail = (
            f"{len(withdrawals)} 笔出金集中在 {span:.1f} 小时内"
            + ("，符合批处理" if m3_hit else f"，超过批处理窗口 {batch_window:g} 小时或笔数不足")
        )
    else:
        span = 0.0
        m3_hit = False
        m3_detail = "该池没有出金，无法评估批处理"
    signals.append(_signal("M3", round(span, 1), f"<={batch_window:g}h", m3_hit, m3_detail))

    # M4 地址一次性
    m4_value = _one_time_destinations(case, pool_id, withdrawals)
    m4_detail = (
        f"{m4_value} 个出金目标地址只收到一笔入金、此前也未在案内出现"
        + ("，符合一次性收款地址" if m4_value >= 2 else "，不足 2 个，地址复用度偏高")
    )
    signals.append(_signal("M4", m4_value, ">=2", m4_value >= 2, m4_detail))

    # M5 面额标准化
    if withdrawals:
        standard = [e for e in withdrawals if significant_digits(e.amount) <= STANDARD_DENOMINATION_DIGITS]
        m5_pct = Decimal(len(standard)) * Decimal(100) / Decimal(len(withdrawals))
        m5_hit = m5_pct >= STANDARD_DENOMINATION_MIN_PCT
        m5_detail = (
            f"{len(standard)}/{len(withdrawals)} 笔出金为整额或短尾数面额（占比 {m5_pct:.0f}%）"
            + ("，呈面额标准化" if m5_hit else "，面额零散")
        )
        m5_value = float(round(m5_pct, 1))
    else:
        m5_hit = False
        m5_value = 0.0
        m5_detail = "该池没有出金，无法评估面额标准化"
    signals.append(_signal("M5", m5_value, f">={STANDARD_DENOMINATION_MIN_PCT:.0f}%", m5_hit, m5_detail))

    # M6 入出金不守恒（服务抽成）
    if inflow > ZERO:
        ratio = (inflow - outflow) / inflow
        m6_value = float(round(ratio * 100, 2))
        low, high = SERVICE_FEE_BAND
        m6_hit = low <= ratio <= high
        if ratio < ZERO:
            m6_detail = (
                f"入金 {money_text(inflow)}、出金 {money_text(outflow)}，差额 {m6_value:.2f}%，"
                "出金反而多于入金，不像服务抽成（更可能是该池还有未纳入的入金）"
            )
        elif ratio < low:
            m6_detail = f"入金 {money_text(inflow)}、出金 {money_text(outflow)}，差额 {m6_value:.2f}%，近乎守恒，缺少服务抽成特征"
        elif ratio > high:
            m6_detail = f"入金 {money_text(inflow)}、出金 {money_text(outflow)}，差额 {m6_value:.2f}%，超出抽成区间，更像拆分或未观测完整"
        else:
            m6_detail = f"入金 {money_text(inflow)}、出金 {money_text(outflow)}，差额 {m6_value:.2f}%，落在服务抽成区间"
    else:
        m6_value = 0.0
        m6_hit = False
        m6_detail = "该池没有入金，无法评估守恒关系"
    signals.append(_signal("M6", m6_value, "0.01%–3%", m6_hit, m6_detail))

    # M7 固定面额存提
    deposit_groups = cluster_amounts([e.amount for e in deposits], tolerance)
    top_deposit = deposit_groups[0] if deposit_groups else []
    m7_value = len(top_deposit)
    m7_hit = False
    if top_deposit and len(top_deposit) >= 2:
        face = deposits[top_deposit[0]].amount
        matched = [e for e in withdrawals if abs(e.amount - face) <= face * tolerance]
        m7_hit = bool(matched)
        m7_detail = (
            f"{m7_value} 笔存款同为标准面额 {money_text(face)}"
            + (f"，并有 {len(matched)} 笔同面额提现" if matched else "，但未见同面额提现")
        )
    elif top_deposit:
        m7_detail = f"同面额存款仅 {m7_value} 笔，未形成固定面额存提模式"
    else:
        m7_detail = "该池没有入金，无法评估固定面额存提"
    signals.append(_signal("M7", m7_value, ">=2 笔同面额", m7_hit, m7_detail))

    hit_ids = [s["id"] for s in signals if s["hit"]]
    score = min(100, sum(s["weight"] for s in signals if s["hit"]))

    mechanism, satisfied, basis = classify_mechanism(signals, deposits, withdrawals)
    counter_hints = counter_evidence(signals, deposits, withdrawals)

    moments = [e.at for e in deposits + withdrawals]
    window_hours = round(_hours(max(moments), min(moments)), 1) if len(moments) > 1 else 0.0

    return {
        "pool_id": pool_id,
        "label": pool.label,
        "mechanism": mechanism,
        "mechanism_satisfied": satisfied,
        "mechanism_basis": basis,
        "counter_hints": counter_hints,
        "detection_score": int(score),
        "signals": signals,
        "rounds": _rounds(moments, batch_window),
        "window_hours": window_hours,
        "inflow": money_text(inflow),
        "outflow": money_text(outflow),
        "deposit_edges": [e.id for e in deposits],
        "withdrawal_edges": [e.id for e in withdrawals],
        "_hit_ids": hit_ids,
        "_deposits": deposits,
        "_withdrawals": withdrawals,
    }


def detect_case(case: Case, params: dict | None = None) -> list[dict]:
    """返回该案件全部池的判别结果，按分数降序。"""
    params = params or default_params()
    pools = [analyse_pool(case, pid, params) for pid in pool_candidates(case, params)]
    pools.sort(key=lambda p: (-p["detection_score"], p["pool_id"]))
    return pools


def public_pool(pool: dict) -> dict:
    """剥掉内部中间字段，只留契约字段。"""
    return {k: v for k, v in pool.items() if not k.startswith("_")}


def detect(dataset: Dataset, params: dict | None = None) -> dict[str, list[dict]]:
    params = params or default_params()
    return {case.case_id: detect_case(case, params) for case in dataset.cases}

"""法币出口的资产可控性评分（契约 §1 与 §3.4）。

口径：
    S_profile = 0.28·规则友好 + 0.26·执行效率 + 0.20·成本 + 0.18·响应时效
                + 0.08·证据充分 + 优先通道加分，截断到 [0,100]
    S_ctrl    = clamp(0, 100, 0.65·S_profile + 0.25·S_off + 0.10·C − P)
    P         = high 15 / medium 8 / low 2
    信号灯    = GREEN ≥75 / YELLOW ≥60 / RED <60

公式里的每个输入值都会输出（含 profile 五维与权重），界面可以原样复算，
不需要在前端硬编码任何系数。
"""

from __future__ import annotations

from .graph import ZERO

# 五维权重：契约 §1 固定值，合计 1.00
PROFILE_WEIGHTS = {
    "rule_friendly": 0.28,
    "execution_efficiency": 0.26,
    "cost": 0.20,
    "response_hours": 0.18,
    "evidence": 0.08,
}

# 各类出口的画像基线。刻意分开写，是因为"可控"与"可判"不是一回事：
# 地下钱庄最容易识别（类型规则高分），但最难执行（无合规主体）。
TYPE_PROFILE = {
    "exchange": {"rule_friendly": 82, "execution_efficiency": 45, "cost": 74, "response_hours": 12, "evidence": 40},
    "otc": {"rule_friendly": 34, "execution_efficiency": 40, "cost": 50, "response_hours": 6, "evidence": 40},
    "payment_gateway": {"rule_friendly": 62, "execution_efficiency": 42, "cost": 70, "response_hours": 24, "evidence": 40},
    "underground_bank": {"rule_friendly": 18, "execution_efficiency": 26, "cost": 28, "response_hours": 8, "evidence": 40},
    "bridge": {"rule_friendly": 44, "execution_efficiency": 34, "cost": 44, "response_hours": 36, "evidence": 40},
    "dex": {"rule_friendly": 38, "execution_efficiency": 32, "cost": 40, "response_hours": 48, "evidence": 40},
}

# 预计冻结窗口（小时）——估计值，界面须标注「估计」。
FREEZE_WINDOW_HOURS = {
    "exchange": 36,
    "otc": 12,
    "payment_gateway": 48,
    "underground_bank": 6,
    "bridge": 12,
    "dex": 2,
}

CONSTRAINT_TEXT = {
    "rule_friendly": "缺合规主体或正式接口，只能走替代取证路径",
    "execution_efficiency": "执行链路长，落地协作环节多",
    "cost": "协作与取证成本较高，需要上级审批",
    "response_hours": "对方响应时效不可控，资金可能先行转出",
    "evidence": "证据链尚不完整，需补齐前后手资金流",
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def level_of(score: float, signal: dict) -> str:
    """信号灯。边界归属上档：75 → GREEN，60 → YELLOW。"""
    if score >= float(signal["green"]):
        return "GREEN"
    if score >= float(signal["yellow"]):
        return "YELLOW"
    return "RED"


def default_params() -> dict:
    return {
        "alpha": 0.65,
        "beta": 0.25,
        "gamma": 0.10,
        "penalty": {"high": 15, "medium": 8, "low": 2},
        "signal": {"green": 75, "yellow": 60},
    }


def evaluate(node, offramp_type: str, s_off: float, ctx: dict, params: dict) -> dict:
    """算一个出口的可控性。

    ctx 需提供：deposit_count、inflow_total(Decimal)、from_mixer(Decimal)、
    best_link_score、node_rule_hits（是否命中节点类型规则）。
    """
    base = TYPE_PROFILE.get(offramp_type, TYPE_PROFILE["dex"])
    has_entity = bool(node.entity)
    deposit_count = int(ctx["deposit_count"])
    from_mixer = ctx["from_mixer"]
    inflow_total = ctx["inflow_total"]
    risk = node.risk

    rule_friendly = base["rule_friendly"]
    if has_entity:
        rule_friendly += 6
    if risk == "high":
        rule_friendly -= 6
    if deposit_count >= 2:
        rule_friendly += 4

    execution = base["execution_efficiency"] + 0.35 * s_off
    if risk == "high":
        execution -= 4

    cost = base["cost"]
    # 入金越大，处置与返还环节越复杂，成本可控性下降
    if inflow_total >= 100000:
        cost -= 14
    elif inflow_total >= 1000:
        cost -= 7

    response_score = 100.0 - base["response_hours"] * 1.2

    evidence = 35.0 + 0.3 * s_off
    if has_entity:
        evidence += 8
    if deposit_count >= 2:
        evidence += 6

    priority_bonus = 0
    if ctx["best_link_score"] >= 70:
        priority_bonus += 6
    if from_mixer > ZERO:
        priority_bonus += 3
    priority_bonus = min(10, priority_bonus)

    profile = {
        "rule_friendly": round(_clamp(rule_friendly), 1),
        "execution_efficiency": round(_clamp(execution), 1),
        "cost": round(_clamp(cost), 1),
        "response_hours": round(_clamp(response_score), 1),
        "evidence": round(_clamp(evidence), 1),
        "priority_bonus": priority_bonus,
    }

    s_profile = sum(profile[key] * weight for key, weight in PROFILE_WEIGHTS.items()) + priority_bonus
    s_profile = round(_clamp(s_profile), 1)

    c_value = 100 if ctx["node_rule_hits"] else 55
    p_value = int(params["penalty"].get(risk, params["penalty"]["medium"]))

    alpha = float(params["alpha"])
    beta = float(params["beta"])
    gamma = float(params["gamma"])
    s_ctrl = alpha * s_profile + beta * float(s_off) + gamma * float(c_value) - float(p_value)
    s_ctrl = round(_clamp(s_ctrl), 1)

    ranked = sorted(
        PROFILE_WEIGHTS,
        key=lambda key: (profile[key], PROFILE_WEIGHTS[key]),
    )
    main_constraints = [CONSTRAINT_TEXT[key] for key in ranked[:2] if profile[key] < 70]
    if not main_constraints:
        main_constraints = ["主要维度均已达标，按常规协作流程推进"]

    freeze = FREEZE_WINDOW_HOURS.get(offramp_type, 24)
    if risk == "high":
        freeze = max(2, int(round(freeze * 0.7)))

    formula = (
        f"{alpha:.2f}×{s_profile:.1f} + {beta:.2f}×{float(s_off):.1f} + "
        f"{gamma:.2f}×{c_value} − {p_value} = {s_ctrl:.1f}"
    )

    return {
        "s_profile": s_profile,
        "s_off": round(float(s_off), 1),
        "c": c_value,
        "p": p_value,
        "s_ctrl": s_ctrl,
        "level": level_of(s_ctrl, params["signal"]),
        "main_constraints": main_constraints,
        "freeze_window_hours": freeze,
        "profile": profile,
        "profile_weights": dict(PROFILE_WEIGHTS),
        "formula": formula,
        "estimate_note": "冻结窗口与响应时效为估计值，依据出口类型与风险等级推算，不是实测。",
    }

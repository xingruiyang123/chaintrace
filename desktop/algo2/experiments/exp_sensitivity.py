# -*- coding: utf-8 -*-
"""特征消融与权重稳定性实验（B1–B5）。

只读既有模块（attribution / mixer_detect / pipeline / graph），不改算法、不改 fixtures。
全部结果写入 algo2/experiments/out/sensitivity.json，固定随机种子，重复运行结果一致。

B1 六维离一消融：逐维权重置 0、其余按比例归一到 100，重跑评估集，看 p@1 / p@3 / MRR。
B2 权重乘性扰动：权重 ×(1+U(-a,a))，a=0.3 / 0.5 各 200 次，看指标区间与每笔入金的 rank-1 稳定性。
B3 六维 Spearman 秩相关：检查"同一事实被重复奖励"（amount↔denomination、fee_fit↔amount）。
B4 七信号离一消融：真池与负样本池上分别把信号权重置 0，算判别贡献。
B5 七信号共线：命中 0/1 向量的 phi 系数。
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[2]
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

from algo2 import attribution, graph, mixer_detect, pipeline  # noqa: E402

SEED = 20261113
FEATURES = attribution.FEATURES
FIXTURE = DESKTOP / "algo2" / "fixtures" / "mixer_eval_cases.json"
NEG_FIXTURE = DESKTOP / "algo2" / "experiments" / "fixtures_negative_pools.json"
OUT = DESKTOP / "algo2" / "experiments" / "out" / "sensitivity.json"

# 任务书给定的基线（用于核对本法基线可复现，不参与排序）
DECLARED_BASELINE = {"p_at_1": 0.8387, "p_at_3": 0.9355, "mrr": 0.8962}
SIGNAL_IDS = ("M1", "M2", "M3", "M4", "M5", "M6", "M7")


# —————————————————————— 小工具 ——————————————————————


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _r(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def normalize(weights: dict) -> dict:
    """把权重按比例缩放到合计恰好 100.0（浮点尾部误差修正到最后一项）。"""
    total = sum(float(weights[key]) for key in FEATURES)
    scaled = {key: float(weights[key]) * 100.0 / total for key in FEATURES}
    residual = 100.0 - sum(scaled.values())
    if abs(residual) > 0.0:
        last = FEATURES[-1]
        scaled[last] = scaled[last] + residual
    return scaled


def quantile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（与 numpy 默认 linear 口径一致），输入需已排序。"""
    n = len(sorted_values)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def ranks_of(values: list[float]) -> list[float]:
    """平均秩（并列取平均），用于手写 Spearman。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    dx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    dy = math.sqrt(sum((b - my) ** 2 for b in ys))
    if dx == 0.0 or dy == 0.0:
        return None
    return num / (dx * dy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """手写 Spearman：秩上的 Pearson。任一侧全同值则无定义，返回 None。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return pearson(ranks_of(xs), ranks_of(ys))


def phi_coefficient(bits_a: list[bool], bits_b: list[bool]) -> float | None:
    """两个 0/1 向量的 phi 系数（= 2×2 卡方的平方根，带符号）。任一边际全同则无定义。"""
    n = len(bits_a)
    if n == 0 or len(bits_b) != n:
        return None
    n11 = sum(1 for a, b in zip(bits_a, bits_b) if a and b)
    n10 = sum(1 for a, b in zip(bits_a, bits_b) if a and not b)
    n01 = sum(1 for a, b in zip(bits_a, bits_b) if not a and b)
    n00 = n - n11 - n10 - n01
    denom = (n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)
    if denom == 0:
        return None
    return (n11 * n00 - n10 * n01) / math.sqrt(denom)


# —————————————————————— 评估器 ——————————————————————


class Evaluator:
    """把"给定六维权重 → 评估集指标 + 每笔入金的 rank-1"跑一遍。

    池识别只依赖 mixer 参数，与归因权重无关，故按案缓存，避免每次重复检测。
    """

    def __init__(self, dataset: graph.Dataset, params: dict) -> None:
        self.dataset = dataset
        self.params = params
        self.pools = {case.case_id: mixer_detect.detect_case(case, params["mixer"]) for case in dataset.cases}
        self.runs = 0

    def run(self, weights: dict) -> dict:
        params = dict(self.params)
        params["attribution"] = dict(self.params["attribution"])
        params["attribution"]["weights"] = {key: float(weights[key]) for key in FEATURES}
        per_case: list[dict] = []
        deposits: dict[tuple, dict] = {}
        tiers: Counter = Counter()
        scores: list[float] = []
        link_tiers: dict[tuple, str] = {}

        for case in self.dataset.cases:
            pools = self.pools[case.case_id]
            links = attribution.attribute_case(case, pools, params)
            metrics = pipeline.case_metrics(case, links, params)
            attr = metrics.get("attribution")
            truth = case.truth_links()

            by_deposit: dict[str, list[dict]] = {}
            for link in links:
                by_deposit.setdefault(link["deposit_edge"], []).append(link)
                tiers[link["tier"]] += 1
                scores.append(float(link["score"]))
                link_tiers[(case.case_id, link["deposit_edge"], link["withdrawal_edge"])] = link["tier"]
            for deposit_id, rows in by_deposit.items():
                best = max(row["score"] for row in rows)
                rank1 = tuple(sorted(row["withdrawal_edge"] for row in rows if abs(row["score"] - best) < 1e-9))
                truth_set = {w for (d, w) in truth if d == deposit_id}
                credit = (1.0 / len(rank1)) if (truth_set and any(w in truth_set for w in rank1)) else 0.0
                deposits[(case.case_id, rows[0]["pool_id"], deposit_id)] = {
                    "n_candidates": len(rows),
                    "rank1": rank1,
                    "truth": tuple(sorted(truth_set)),
                    "p_at_1": credit,
                }

            per_case.append(
                {
                    "case_id": case.case_id,
                    "n": (attr or {}).get("n", 0),
                    "p_at_1": (attr or {}).get("p_at_1"),
                    "p_at_3": (attr or {}).get("p_at_3"),
                    "mrr": (attr or {}).get("mrr"),
                    "competition": (attr or {}).get("competition"),
                }
            )

        self.runs += 1
        return {
            "weights": {key: _r(weights[key], 6) for key in FEATURES},
            "metrics": merge_metrics(per_case),
            "per_case": per_case,
            "deposits": deposits,
            "tier_counts": {name: tiers.get(name, 0) for name in ("HIGH", "MEDIUM", "LOW")},
            "mean_score": _r(sum(scores) / len(scores), 2) if scores else None,
            "link_tiers": link_tiers,
        }


def merge_metrics(rows: list[dict]) -> dict:
    """按 n 加权合并（与 benchmark._merge 同口径）。"""
    rows = [row for row in rows if row.get("n")]
    if not rows:
        return {"p_at_1": None, "p_at_3": None, "mrr": None, "n": 0}
    total = sum(int(row["n"]) for row in rows)
    return {
        "p_at_1": _r(sum(float(row["p_at_1"]) * int(row["n"]) for row in rows) / total),
        "p_at_3": _r(sum(float(row["p_at_3"]) * int(row["n"]) for row in rows) / total),
        "mrr": _r(sum(float(row["mrr"]) * int(row["n"]) for row in rows) / total),
        "n": total,
    }


def delta(metrics: dict, base: dict) -> dict:
    return {
        key: _r(float(metrics[key]) - float(base[key])) for key in ("p_at_1", "p_at_3", "mrr") if metrics.get(key) is not None
    }


# —————————————————————— B1 ——————————————————————


def b1_leave_one_out(ev: Evaluator, base_weights: dict, base_metrics: dict, base_run: dict) -> dict:
    rows = []
    for feature in FEATURES:
        raw = {key: (0.0 if key == feature else float(base_weights[key])) for key in FEATURES}
        weights = normalize(raw)
        result = ev.run(weights)
        rows.append(
            {
                "feature": feature,
                "label": attribution.FEATURE_LABELS[feature],
                "removed_weight": float(base_weights[feature]),
                "weights_after": {key: _r(weights[key], 4) for key in FEATURES},
                "metrics": result["metrics"],
                "delta": delta(result["metrics"], base_metrics),
                "tier_counts": result["tier_counts"],
                "tier_delta_HIGH": result["tier_counts"]["HIGH"] - base_run["tier_counts"]["HIGH"],
                "mean_score": result["mean_score"],
                "mean_score_delta": _r(result["mean_score"] - base_run["mean_score"], 2),
                "per_case_p_at_1": {
                    row["case_id"]: row["p_at_1"] for row in result["per_case"] if row.get("n")
                },
            }
        )
    rows.sort(key=lambda row: (row["delta"]["p_at_1"], row["feature"]))

    # 判据写死在产物里，避免口头解释与数字脱节
    load_bearing = [row["feature"] for row in rows if row["delta"]["p_at_1"] <= -0.02]
    marginal = [
        row["feature"]
        for row in rows
        if -0.02 < row["delta"]["p_at_1"] < 0.0
    ]
    droppable = [row["feature"] for row in rows if row["delta"]["p_at_1"] >= 0.0]
    return {
        "method": "逐维权重置 0，其余五维按原比例缩放到合计 100，重跑整份评估集（8 案 / 31 笔有真值入金）。",
        "criterion": {
            "load_bearing": "Δp@1 ≤ −0.02（去掉即掉至少 2 个百分点）",
            "marginal": "−0.02 < Δp@1 < 0（能掉但不到 2 个百分点）",
            "droppable": "Δp@1 ≥ 0（去掉不掉分，甚至略升）",
        },
        "rows": rows,
        "ranking_by_p_at_1_drop": [
            {"feature": row["feature"], "delta_p_at_1": row["delta"]["p_at_1"], "delta_p_at_3": row["delta"]["p_at_3"],
             "delta_mrr": row["delta"]["mrr"]}
            for row in rows
        ],
        "verdict": {"load_bearing": load_bearing, "marginal": marginal, "droppable_or_redundant": droppable},
    }


# —————————————————————— B2 ——————————————————————


def b2_perturbation(
    ev: Evaluator, base_weights: dict, base_metrics: dict, single_baseline_p1: float, base_run: dict
) -> dict:
    levels = []
    for a in (0.3, 0.5):
        runs = 200
        p1_values: list[float] = []
        p3_values: list[float] = []
        mrr_values: list[float] = []
        rank1_counter: dict[tuple, Counter] = {}
        seen_deposits: dict[tuple, dict] = {}
        tier_counter: dict[tuple, Counter] = {}
        high_counts: list[int] = []
        mean_scores: list[float] = []
        weight_min: dict[str, float] = {key: float("inf") for key in FEATURES}
        weight_max: dict[str, float] = {key: float("-inf") for key in FEATURES}

        for index in range(runs):
            rng = random.Random(SEED + int(round(a * 1000)) * 1000 + index)
            noisy = {key: float(base_weights[key]) * (1.0 + rng.uniform(-a, a)) for key in FEATURES}
            weights = normalize(noisy)
            for key in FEATURES:
                weight_min[key] = min(weight_min[key], weights[key])
                weight_max[key] = max(weight_max[key], weights[key])
            result = ev.run(weights)
            p1_values.append(result["metrics"]["p_at_1"])
            p3_values.append(result["metrics"]["p_at_3"])
            mrr_values.append(result["metrics"]["mrr"])
            high_counts.append(result["tier_counts"]["HIGH"])
            mean_scores.append(result["mean_score"])
            for key, info in result["deposits"].items():
                rank1_counter.setdefault(key, Counter())[info["rank1"]] += 1
                seen_deposits.setdefault(key, info)
            for key, tier in result["link_tiers"].items():
                tier_counter.setdefault(key, Counter())[tier] += 1

        p1_sorted = sorted(p1_values)
        p3_sorted = sorted(p3_values)
        mrr_sorted = sorted(mrr_values)

        deposits = []
        for key, counter in rank1_counter.items():
            info = seen_deposits[key]
            modal_rank1, modal_count = counter.most_common(1)[0]
            # top1（并列取 id 最小）稳定性：把"并列集合"退化成单一首名
            top1_counter = Counter()
            for rank1, count in counter.items():
                top1_counter[rank1[0]] += count
            top1_id, top1_count = top1_counter.most_common(1)[0]
            deposits.append(
                {
                    "case_id": key[0],
                    "pool_id": key[1],
                    "deposit_edge": key[2],
                    "n_candidates": info["n_candidates"],
                    "baseline_rank1": list(info["rank1"]),
                    "baseline_p_at_1": info["p_at_1"],
                    "truth_withdrawal": list(info["truth"]),
                    "rank1_stability": _r(modal_count / runs),
                    "distinct_rank1_sets": len(counter),
                    "modal_rank1": list(modal_rank1),
                    "top1_stability": _r(top1_count / runs),
                    "modal_top1": top1_id,
                    "rank1_sets": [
                        {"withdrawals": list(rank1), "count": count}
                        for rank1, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
                    ],
                }
            )
        deposits.sort(key=lambda row: (row["rank1_stability"], row["top1_stability"], row["case_id"], row["deposit_edge"]))

        flips_vs_amount = sum(1 for value in p1_values if value < single_baseline_p1)
        flips_one_step = sum(1 for value in p1_values if value < base_metrics["p_at_1"] - 1.0 / base_metrics["n"] - 1e-12)
        changed_any = [row for row in deposits if row["rank1_stability"] < 1.0]

        levels.append(
            {
                "a": a,
                "runs": runs,
                "seed_rule": f"random.Random({SEED} + {int(round(a * 1000))}*1000 + run_index)",
                "p_at_1": {
                    "mean": _r(sum(p1_values) / runs),
                    "sd": _r(stdev(p1_values)),
                    "p5": _r(quantile(p1_sorted, 0.05)),
                    "p95": _r(quantile(p1_sorted, 0.95)),
                    "min": _r(p1_sorted[0]),
                    "max": _r(p1_sorted[-1]),
                    "baseline": base_metrics["p_at_1"],
                },
                "p_at_3": {
                    "mean": _r(sum(p3_values) / runs),
                    "sd": _r(stdev(p3_values)),
                    "p5": _r(quantile(p3_sorted, 0.05)),
                    "p95": _r(quantile(p3_sorted, 0.95)),
                    "min": _r(p3_sorted[0]),
                    "max": _r(p3_sorted[-1]),
                    "baseline": base_metrics["p_at_3"],
                },
                "mrr": {
                    "mean": _r(sum(mrr_values) / runs),
                    "sd": _r(stdev(mrr_values)),
                    "p5": _r(quantile(mrr_sorted, 0.05)),
                    "p95": _r(quantile(mrr_sorted, 0.95)),
                    "baseline": base_metrics["mrr"],
                },
                "weights_range": {key: [_r(weight_min[key], 2), _r(weight_max[key], 2)] for key in FEATURES},
                "flip": {
                    "definition_vs_single_feature": f"该次扰动下整体 p@1 < 仅金额匹配基线 {single_baseline_p1}（即结论由「本法更好」翻成「不如单特征」）",
                    "count_vs_single_feature_baseline": flips_vs_amount,
                    "definition_vs_baseline_minus_one_deposit": f"整体 p@1 < 基线 − 1/31 = {_r(base_metrics['p_at_1'] - 1.0 / base_metrics['n'])}（即掉超过一笔入金）",
                    "count_below_baseline_minus_one_deposit": flips_one_step,
                    "p_at_1_one_deposit_step": _r(1.0 / base_metrics["n"]),
                },
                "rank1_stability": {
                    "deposits": len(deposits),
                    "deposits_with_truth": sum(1 for row in deposits if row["truth_withdrawal"]),
                    "all_stable": len(deposits) - len(changed_any),
                    "ever_changed": len(changed_any),
                    "mean_stability": _r(sum(row["rank1_stability"] for row in deposits) / len(deposits)),
                },
                "tier_drift": {
                    "links": len(tier_counter),
                    "links_ever_changing": sum(1 for counter in tier_counter.values() if len(counter) > 1),
                    "share_ever_changing": _r(
                        sum(1 for counter in tier_counter.values() if len(counter) > 1) / max(1, len(tier_counter))
                    ),
                    "baseline_tier_counts": base_run["tier_counts"],
                    "high_count": {
                        "mean": _r(sum(high_counts) / runs, 1),
                        "min": min(high_counts),
                        "max": max(high_counts),
                        "baseline": base_run["tier_counts"]["HIGH"],
                    },
                    "mean_score": {
                        "baseline": base_run["mean_score"],
                        "min": _r(min(mean_scores), 2),
                        "max": _r(max(mean_scores), 2),
                    },
                },
                "per_deposit": deposits,
            }
        )
    return {
        "method": "六维权重 ×(1+U(−a,a)) 后重新归一到 100，各档独立扰动 200 次，重跑整份评估集；随机种子按 run_index 固定，可复现。",
        "definitions": {
            "rank1_stability": "200 次扰动里，「该入金得分最高的出金集合」保持与最常见集合一致的比例；并列时集合整体比较。",
            "top1_stability": "把并列集合按出金边 id 取最小者作为首名，200 次里首名不变的比例。",
            "per_deposit": "按 rank1_stability 升序排列；若全部为 1.000，顺序即按案件号与入金边号。",
            "tier_drift": "档位（HIGH≥70／MEDIUM≥45／LOW）按分数阈值切分，权重扰动会改变分数水平，故档位比排序脆弱；这里统计 200 次里曾改过档的候选对占比。",
        },
        "levels": levels,
    }


# —————————————————————— B2 补充：排序对权重的解析不变性 ——————————————————————


def b2_invariance(dataset: graph.Dataset, params: dict) -> dict:
    """对每笔入金算「top-1 与 top-2 的分差要多大的权重变形才会被抹平」。

    六维取值与权重无关，故对固定的一对候选，分差是权重的线性函数：
        gap(w) = Σ_k w_k · d_k,   d_k = v_top1,k − v_top2,k
    记 pos = Σ_{d>0} w_k d_k、neg = Σ_{d<0} w_k |d_k|，则把负向维度整体放大 f 倍即可抹平，
        f* = pos / neg。
    乘性噪声 ±a 下，任意两维权重的相对比最多变为 (1+a)/(1−a) 倍，因此
        f* ≥ (1+a)/(1−a)  ⇔  a ≤ (f*−1)/(f*+1)
    时该笔的 rank-1 不可能被扰动改变 —— 这是必要条件，不依赖抽样。
    """
    base = params["attribution"]["weights"]
    rows: list[dict] = []
    for case in dataset.cases:
        pools = mixer_detect.detect_case(case, params["mixer"])
        for pool in pools:
            by_deposit: dict[str, list[dict]] = {}
            for link in attribution.attribute_pool(case, pool, params):
                by_deposit.setdefault(link["deposit_edge"], []).append(link)
            for deposit_id, cands in by_deposit.items():
                cands.sort(key=lambda row: (-row["score"], row["withdrawal_edge"]))
                top = cands[0]
                rest = [row for row in cands[1:] if abs(row["score"] - top["score"]) > 1e-9]
                if not rest:
                    rows.append(
                        {
                            "case_id": case.case_id,
                            "pool_id": pool["pool_id"],
                            "deposit_edge": deposit_id,
                            "kind": "tied_top",
                            "gap": 0.0,
                            "required_distortion": None,
                            "immune": False,
                        }
                    )
                    continue
                # 对每个竞争者算「要抹平它需要放大多少倍反向权重」，取最紧的一个
                tightest_ratio = None
                tightest_row = None
                tightest_second = None
                for other in rest:
                    diff = {
                        key: float(top["features"][key]["value"]) - float(other["features"][key]["value"])
                        for key in FEATURES
                    }
                    pos = sum(float(base[key]) * diff[key] for key in FEATURES if diff[key] > 0)
                    neg = sum(-float(base[key]) * diff[key] for key in FEATURES if diff[key] < 0)
                    ratio = None if neg == 0.0 else pos / neg
                    if ratio is not None and (tightest_ratio is None or ratio < tightest_ratio):
                        tightest_ratio = ratio
                        tightest_row = diff
                        tightest_second = other
                second = tightest_second if tightest_second is not None else rest[0]
                diff = tightest_row if tightest_row is not None else {
                    key: float(top["features"][key]["value"]) - float(second["features"][key]["value"])
                    for key in FEATURES
                }
                pos = sum(float(base[key]) * diff[key] for key in FEATURES if diff[key] > 0)
                neg = sum(-float(base[key]) * diff[key] for key in FEATURES if diff[key] < 0)
                rows.append(
                    {
                        "case_id": case.case_id,
                        "pool_id": pool["pool_id"],
                        "deposit_edge": deposit_id,
                        "kind": "separated",
                        "top1": top["withdrawal_edge"],
                        "top2": second["withdrawal_edge"],
                        "n_competitors": len(rest),
                        "gap": _r(top["score"] - second["score"], 2),
                        "diff": {key: _r(diff[key], 1) for key in FEATURES},
                        "pos_mass": _r(pos, 2),
                        "neg_mass": _r(neg, 2),
                        "required_distortion": None if neg == 0.0 else _r(pos / neg, 3),
                        "immune": neg == 0.0,
                    }
                )

    separated = [row for row in rows if row["kind"] == "separated"]
    distort = [row["required_distortion"] for row in separated if row["required_distortion"] is not None]
    tightest = min(separated, key=lambda row: row["required_distortion"] or float("inf")) if distort else None
    min_distortion = min(distort) if distort else None
    a_threshold = None if not min_distortion else (min_distortion - 1.0) / (min_distortion + 1.0)
    return {
        "definition": "对每笔入金，取 top-1 与 top-2 的六维差值 d，required_distortion = Σ_{d>0} w·d ÷ Σ_{d<0} w·|d|；该值等于「把反对维度的权重放大多少倍才会抹平分差」。",
        "analytic_bound": "乘性噪声 ±a 下任意两维相对权重最多变为 (1+a)/(1−a) 倍；若该值小于 required_distortion，则该笔 rank-1 不可能被扰动改变。",
        "n_deposits": len(rows),
        "n_tied_top": len(rows) - len(separated),
        "n_immune": sum(1 for row in separated if row["immune"]),
        "n_needs_distortion": len(distort),
        "min_required_distortion": min_distortion,
        "median_required_distortion": _r(quantile(sorted(distort), 0.5), 3) if distort else None,
        "tightest": tightest,
        "a_threshold": None if a_threshold is None else _r(a_threshold, 4),
        "flip_possible_at": {
            str(a): (None if a_threshold is None else bool(a > a_threshold + 1e-12))
            for a in (0.3, 0.5, 0.7, 0.9, 1.0)
        },
        "rows": rows,
    }


def b2_threshold_check(ev: Evaluator, base_weights: dict, base_run: dict, analytic_threshold: float | None) -> dict:
    """把扰动幅度继续加大，验证解析阈值：翻盘点应出现在 a ≈ analytic_threshold 之后。"""
    rows = []
    for a in (0.6, 0.7, 0.9):
        runs = 200
        changed_runs = 0
        p1_values: list[float] = []
        changed_deposits: set[tuple] = set()
        for index in range(runs):
            rng = random.Random(SEED + int(round(a * 1000)) * 1000 + index)
            noisy = {key: float(base_weights[key]) * (1.0 + rng.uniform(-a, a)) for key in FEATURES}
            result = ev.run(normalize(noisy))
            p1_values.append(result["metrics"]["p_at_1"])
            moved = [
                key for key in base_run["deposits"] if result["deposits"][key]["rank1"] != base_run["deposits"][key]["rank1"]
            ]
            if moved:
                changed_runs += 1
                changed_deposits.update(moved)
        rows.append(
            {
                "a": a,
                "runs": runs,
                "runs_with_rank1_change": changed_runs,
                "deposits_ever_changed": len(changed_deposits),
                "p_at_1": {
                    "min": _r(min(p1_values)),
                    "max": _r(max(p1_values)),
                    "mean": _r(sum(p1_values) / runs),
                    "baseline": base_run["metrics"]["p_at_1"],
                },
            }
        )
    return {
        "purpose": "检验 B2 的解析阈值：如果 rank-1 只在 a 超过解析阈值之后才开始变动，说明「±30%/±50% 不翻」是结构性的，不是抽样运气。",
        "analytic_threshold_a": analytic_threshold,
        "levels": rows,
    }


# —————————————————————— B3 ——————————————————————


def b3_correlation(dataset: graph.Dataset, params: dict) -> dict:
    groups: list[list[dict[str, float]]] = []
    pair_rows = []
    tolerance = Decimal(str(params["mixer"]["equal_output_tolerance"]))
    for case in dataset.cases:
        pools = mixer_detect.detect_case(case, params["mixer"])
        for pool in pools:
            deposits = pool["_deposits"]
            withdrawals = pool["_withdrawals"]
            clusters: dict[str, int] = {}
            for group in mixer_detect.cluster_amounts([e.amount for e in withdrawals], tolerance):
                for index in group:
                    clusters[withdrawals[index].id] = len(group)
            rounds = attribution.withdrawal_rounds(withdrawals, float(params["mixer"]["batch_window_hours"]))
            for deposit in deposits:
                # 组内相关：一笔入金的全部候选出金构成一组，秩只在组内排
                within = []
                for withdrawal in withdrawals:
                    raw = attribution.link_features(case, deposit, withdrawal, clusters, rounds, tolerance)
                    sample = {key: round(float(raw[key][0]), 1) for key in FEATURES}
                    within.append(sample)
                    pair_rows.append(
                        {
                            "case_id": case.case_id,
                            "pool_id": pool["pool_id"],
                            "deposit_edge": deposit.id,
                            "withdrawal_edge": withdrawal.id,
                            **sample,
                        }
                    )
                groups.append(within)

    flat = [sample for group in groups for sample in group]

    def matrix_of(samples: list[dict[str, float]], within_groups: bool) -> list[list[float | None]]:
        if within_groups:
            ranked = {
                key: [rank for group in groups for rank in ranks_of([sample[key] for sample in group])]
                for key in FEATURES
            }
            build = lambda key: ranked[key]  # noqa: E731
        else:
            build = lambda key: [sample[key] for sample in samples]  # noqa: E731
        out: list[list[float | None]] = []
        for key_a in FEATURES:
            row: list[float | None] = []
            xs = build(key_a)
            for key_b in FEATURES:
                value = pearson(xs, build(key_b))
                row.append(None if value is None else _r(value))
            out.append(row)
        return out

    pooled_matrix = matrix_of(flat, False)
    within_matrix = matrix_of(flat, True)

    def pairs_from(matrix: list[list[float | None]]) -> list[dict]:
        out = []
        for i, key_a in enumerate(FEATURES):
            for j in range(i + 1, len(FEATURES)):
                out.append(
                    {
                        "a": key_a,
                        "b": FEATURES[j],
                        "a_label": attribution.FEATURE_LABELS[key_a],
                        "b_label": attribution.FEATURE_LABELS[FEATURES[j]],
                        "spearman": matrix[i][j],
                    }
                )
        return sorted(out, key=lambda row: (-abs(row["spearman"] or 0.0), row["a"], row["b"]))

    pairs = pairs_from(pooled_matrix)
    within_pairs = pairs_from(within_matrix)

    def find(source: list[dict], a: str, b: str) -> float | None:
        for row in source:
            if {row["a"], row["b"]} == {a, b}:
                return row["spearman"]
        return None

    def focus(source: list[dict]) -> dict:
        return {
            "amount_vs_denomination": find(source, "amount", "denomination"),
            "fee_fit_vs_amount": find(source, "amount", "fee_fit"),
            "denomination_vs_fee_fit": find(source, "denomination", "fee_fit"),
            "time_vs_cooccurrence": find(source, "time", "cooccurrence"),
            "path_rarity_vs_amount": find(source, "path_rarity", "amount"),
        }

    return {
        "method": "取评估集所有池的「入金×出金」候选对（笛卡尔积），用 attribution.link_features 取六维原始值（与打分同一函数），手写平均秩 Spearman。",
        "n_pairs": len(flat),
        "n_groups": len(groups),
        "features": list(FEATURES),
        "spearman_matrix": pooled_matrix,
        "pairs_ranked": pairs,
        "pairs_above_0_5": [row for row in pairs if (row["spearman"] or 0) > 0.5],
        "pairs_below_minus_0_5": [row for row in pairs if (row["spearman"] or 0) < -0.5],
        "focus": focus(pairs),
        "within_deposit": {
            "method": "同一笔入金的所有候选出金构成一组，六维只在组内排秩后合并算 Pearson（排除池与入金之间的水平差异，反映「同一候选集内两维是否同向」）。",
            "spearman_matrix": within_matrix,
            "pairs_ranked": within_pairs,
            "pairs_above_0_5": [row for row in within_pairs if (row["spearman"] or 0) > 0.5],
            "pairs_below_minus_0_5": [row for row in within_pairs if (row["spearman"] or 0) < -0.5],
            "focus": focus(within_pairs),
        },
        "note": "相关高不等于重复计分：还需看两维在同一对候选上的取值是否同向驱动同一排名。这里只给秩相关，不做因果判断。",
    }


# —————————————————————— B4 / B5 ——————————————————————


def _load_negative_pools(path: Path, mixer_params: dict) -> tuple[list[dict], dict]:
    """读取负样本池文件（由另一个任务生成，格式未固定），返回 [(case, pool_id, label)] 与元信息。

    容忍三种形态：完整数据集 {cases:[...]}、案例列表、以及 {pools:[{case_id, pool_id, ...}]}。
    文件不存在时返回空列表 + skipped 元信息，由调用方如实记录。
    """
    meta: dict = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        meta.update({"skipped": True, "reason": "负样本池文件不存在（由另一个任务生成），按任务书跳过 B4 负样本部分与 B5 的负池样本。"})
        return [], meta

    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    meta["top_level_keys"] = sorted(raw.keys()) if isinstance(raw, dict) else f"list(len={len(raw)})"

    case_dicts: list[dict] = []
    explicit_pools: list[dict] = []

    def take(obj):
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("nodes"), list) and isinstance(obj.get("edges"), list):
            case_dicts.append(obj)
        elif isinstance(obj.get("case"), dict) and isinstance(obj["case"].get("nodes"), list):
            case_dicts.append(obj["case"])
            if obj.get("pool_id"):
                explicit_pools.append({"case_id": obj["case"].get("case_id"), "pool_id": obj["pool_id"]})
        elif obj.get("pool_id"):
            explicit_pools.append(obj)

    if isinstance(raw, dict):
        for key in ("cases", "case_list", "datasets", "pools", "negative_pools", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                for item in value:
                    take(item)
        if not case_dicts and not explicit_pools and isinstance(raw.get("case"), dict):
            take(raw)
    elif isinstance(raw, list):
        for item in raw:
            take(item)

    units: list[dict] = []
    for index, case_dict in enumerate(case_dicts):
        envelope = dict(case_dict)
        if "case_id" not in envelope:
            envelope["case_id"] = f"NEG-{index + 1:02d}"
        wrapper = {
            "schema_version": graph.SCHEMA_VERSION,
            "is_simulated": True,
            "name": "negative-pools",
            "cases": [envelope],
        }
        try:
            dataset = graph.load_dataset(wrapper)
        except graph.InputError as exc:
            meta.setdefault("load_errors", []).append({"case": envelope.get("case_id"), "error": str(exc)})
            continue
        case = dataset.cases[0]
        wanted = [item["pool_id"] for item in explicit_pools if item.get("case_id") == case.case_id]
        if not wanted and isinstance(envelope.get("pool_node"), str):
            # 负样本文件用 pool_node 显式指定"被检验的池节点"（结构上刻意不像混币池，故 pool_candidates 不会给出）
            wanted = [envelope["pool_node"]]
        if not wanted:
            wanted = mixer_detect.pool_candidates(case, mixer_params)
        if not wanted:
            wanted = [node.id for node in case.nodes.values() if node.type == "mixer"]
        wanted = [pid for pid in wanted if pid in case.nodes]
        meta.setdefault("cases", []).append(
            {
                "case_id": case.case_id,
                "negative_type": envelope.get("negative_type"),
                "expected_label": envelope.get("expected_label"),
                "pool_ids": wanted,
                "nodes": len(case.nodes),
                "edges": len(case.edges),
            }
        )
        for pool_id in wanted:
            units.append(
                {
                    "case": case,
                    "pool_id": pool_id,
                    "case_id": case.case_id,
                    "negative_type": envelope.get("negative_type"),
                    "source": "negative_fixture",
                }
            )

    meta["pools_found"] = len(units)
    meta["skipped"] = len(units) == 0
    if meta["skipped"]:
        meta["reason"] = "负样本池文件存在但未解析出可评估的池（见 cases / load_errors）。"
    return units, meta


def b4_signal_ablation(
    true_units: list[dict], negative_units: list[dict], mixer_params: dict, negative_meta: dict
) -> tuple[dict, list[dict]]:
    def analyse(units: list[dict]) -> list[dict]:
        rows = []
        for unit in units:
            pool = mixer_detect.analyse_pool(unit["case"], unit["pool_id"], mixer_params)
            hit = {signal["id"]: bool(signal["hit"]) for signal in pool["signals"]}
            weight = {signal["id"]: float(signal["weight"]) for signal in pool["signals"]}
            raw_sum = sum(weight[sid] for sid in SIGNAL_IDS if hit.get(sid))
            rows.append(
                {
                    "case_id": unit["case_id"],
                    "pool_id": unit["pool_id"],
                    "negative_type": unit.get("negative_type"),
                    "hit": hit,
                    "weight": weight,
                    "raw_sum": raw_sum,
                    "base_score": min(100.0, raw_sum),
                    "truncated": raw_sum > 100.0,
                }
            )
        return rows

    true_rows = analyse(true_units)
    neg_rows = analyse(negative_units) if negative_units else []

    def mean(values: list[float]) -> float | None:
        return (sum(values) / len(values)) if values else None

    table = []
    for sid in SIGNAL_IDS:
        true_base = [row["base_score"] for row in true_rows]
        true_ablated = [
            min(100.0, row["raw_sum"] - (row["weight"][sid] if row["hit"].get(sid) else 0.0)) for row in true_rows
        ]
        entry = {
            "signal": sid,
            "label": mixer_detect.SIGNAL_LABELS[sid],
            "weight": mixer_detect.SIGNAL_WEIGHTS[sid],
            "true_pools": len(true_rows),
            "true_hit_count": sum(1 for row in true_rows if row["hit"].get(sid)),
            "true_baseline_mean": _r(mean(true_base) or 0.0, 2),
            "true_ablated_mean": _r(mean(true_ablated) or 0.0, 2),
            "true_drop": _r((mean(true_base) or 0.0) - (mean(true_ablated) or 0.0), 2),
        }
        if neg_rows:
            neg_base = [row["base_score"] for row in neg_rows]
            neg_ablated = [
                min(100.0, row["raw_sum"] - (row["weight"][sid] if row["hit"].get(sid) else 0.0)) for row in neg_rows
            ]
            entry.update(
                {
                    "neg_pools": len(neg_rows),
                    "neg_hit_count": sum(1 for row in neg_rows if row["hit"].get(sid)),
                    "neg_baseline_mean": _r(mean(neg_base) or 0.0, 2),
                    "neg_ablated_mean": _r(mean(neg_ablated) or 0.0, 2),
                    "neg_drop": _r((mean(neg_base) or 0.0) - (mean(neg_ablated) or 0.0), 2),
                    "discriminative_contribution": _r(
                        ((mean(true_base) or 0.0) - (mean(true_ablated) or 0.0))
                        - ((mean(neg_base) or 0.0) - (mean(neg_ablated) or 0.0)),
                        2,
                    ),
                }
            )
        table.append(entry)

    ranked = sorted(
        table,
        key=lambda row: (
            -(row.get("discriminative_contribution", row["true_drop"])),
            row["signal"],
        ),
    )
    result = {
        "method": "对每个信号把权重置 0（其余不变），从 analyse_pool 输出的 hit × weight 重算 min(100, Σ) 判别分；真池=评估集中构造真值落到的池。",
        "signal_weights": dict(mixer_detect.SIGNAL_WEIGHTS),
        "signal_weight_total": sum(mixer_detect.SIGNAL_WEIGHTS.values()),
        "truncation_note": "权重合计 126，判别分靠 min(100) 截断；任一池若命中组合总分超过 100，扣掉某个信号后可能仍 ≥100，此时该信号在该池上的边际贡献为 0。",
        "true_pools": true_rows,
        "true_pool_baseline_mean": _r(mean([row["base_score"] for row in true_rows]) or 0.0, 2),
        "truncated_true_pools": sum(1 for row in true_rows if row["truncated"]),
        "negative": {
            "source": negative_meta,
            "skipped": not bool(neg_rows),
            "pools": neg_rows,
            "baseline_mean": _r(mean([row["base_score"] for row in neg_rows]) or 0.0, 2) if neg_rows else None,
            "truncated_pools": sum(1 for row in neg_rows if row["truncated"]),
        },
        "table": table,
        "ranking_by_discriminative_contribution": [
            {
                "signal": row["signal"],
                "label": row["label"],
                "discriminative_contribution": row.get("discriminative_contribution"),
                "true_drop": row["true_drop"],
                "neg_drop": row.get("neg_drop"),
            }
            for row in ranked
        ],
        "ranking_by_true_pool_drop": [
            {"signal": row["signal"], "label": row["label"], "true_drop": row["true_drop"]}
            for row in sorted(table, key=lambda row: (-row["true_drop"], row["signal"]))
        ],
    }
    return result, (true_rows + neg_rows)


def b5_collinearity(pool_rows: list[dict]) -> dict:
    vectors = {sid: [bool(row["hit"].get(sid)) for row in pool_rows] for sid in SIGNAL_IDS}
    n = len(pool_rows)
    matrix: list[list[float | None]] = []
    for sid_a in SIGNAL_IDS:
        row: list[float | None] = []
        for sid_b in SIGNAL_IDS:
            value = phi_coefficient(vectors[sid_a], vectors[sid_b])
            row.append(None if value is None else _r(value))
        matrix.append(row)

    pairs = []
    for i, sid_a in enumerate(SIGNAL_IDS):
        for j in range(i + 1, len(SIGNAL_IDS)):
            pairs.append(
                {
                    "a": sid_a,
                    "b": SIGNAL_IDS[j],
                    "a_label": mixer_detect.SIGNAL_LABELS[sid_a],
                    "b_label": mixer_detect.SIGNAL_LABELS[SIGNAL_IDS[j]],
                    "phi": matrix[i][j],
                    "a_hits": sum(vectors[sid_a]),
                    "b_hits": sum(vectors[SIGNAL_IDS[j]]),
                    "both_hits": sum(1 for row in pool_rows if row["hit"].get(sid_a) and row["hit"].get(SIGNAL_IDS[j])),
                }
            )
    pairs.sort(key=lambda row: (-(row["phi"] if row["phi"] is not None else -9), row["a"], row["b"]))
    zero_variance = [sid for sid in SIGNAL_IDS if len(set(vectors[sid])) == 1]
    return {
        "method": "在真池 +（若存在）负样本池上取七信号命中 0/1 向量，两两算 phi 系数（2×2 卡方的带符号平方根）。",
        "n_pools": n,
        "signals": list(SIGNAL_IDS),
        "hit_counts": {sid: sum(vectors[sid]) for sid in SIGNAL_IDS},
        "phi_matrix": matrix,
        "pairs": pairs,
        "pairs_above_0_5": [row for row in pairs if (row["phi"] or 0) > 0.5],
        "pairs_below_minus_0_5": [row for row in pairs if (row["phi"] or 0) < -0.5],
        "zero_variance_signals": zero_variance,
        "warning": (
            "phi 在 n=7 且某信号恒命中（0 方差）时无定义，恒命中信号对之间的相关无法从样本估计；"
            "此时该列全部为 null，不能据此断言「不共线」。"
            if zero_variance
            else ""
        ),
    }


# —————————————————————— 主流程 ——————————————————————


def main() -> int:
    started = time.time()
    digest = graph.file_sha256(FIXTURE)
    dataset, _raw = graph.load_file(FIXTURE)
    params = pipeline.default_parameters()
    base_weights = dict(params["attribution"]["weights"])

    ev = Evaluator(dataset, params)
    base = ev.run(normalize(base_weights))
    base_metrics = base["metrics"]

    # 单特征基线（口径与 benchmark 相同，仅用于 B2 判断"结论是否翻转"）
    single_rows: dict[str, list[dict]] = {"仅金额匹配": [], "仅时间最近": []}
    for case in dataset.cases:
        metrics = pipeline.case_metrics(
            case, attribution.attribute_case(case, ev.pools[case.case_id], params), params
        )
        if not metrics.get("attribution"):
            continue
        for row in metrics["baselines"]:
            single_rows[row["name"]].append(row)
    single = {name: merge_metrics(rows) for name, rows in single_rows.items()}
    single_p1 = float(single["仅金额匹配"]["p_at_1"])

    b1 = b1_leave_one_out(ev, base_weights, base_metrics, base)
    b2 = b2_perturbation(ev, base_weights, base_metrics, single_p1, base)
    b2["invariance"] = b2_invariance(dataset, params)
    b2["closest_to_flip"] = sorted(
        [row for row in b2["invariance"]["rows"] if row["kind"] == "separated" and row["required_distortion"] is not None],
        key=lambda row: row["required_distortion"],
    )[:8]
    b2["threshold_check"] = b2_threshold_check(ev, base_weights, base, b2["invariance"]["a_threshold"])
    b3 = b3_correlation(dataset, params)

    # B4/B5：真池取"构造真值落到的池"
    true_units = []
    for case in dataset.cases:
        truth = case.truth_links()
        seen = set()
        for deposit_edge, _ in sorted(truth):
            pool_id = case.edge(deposit_edge).dst
            if (case.case_id, pool_id) in seen:
                continue
            seen.add((case.case_id, pool_id))
            true_units.append({"case": case, "pool_id": pool_id, "case_id": case.case_id, "source": "truth_pool"})

    negative_units, negative_meta = _load_negative_pools(NEG_FIXTURE, params["mixer"])
    b4, pool_rows = b4_signal_ablation(true_units, negative_units, params["mixer"], negative_meta)
    b5 = b5_collinearity(pool_rows)

    payload = {
        "schema_version": "mixer_offramp/sensitivity/v0.1",
        "is_simulated": True,
        "generated_at": _now(),
        "generated_by": "algo2/experiments/exp_sensitivity.py",
        "seed": SEED,
        "wall_time_seconds": None,
        "dataset": {
            "path": str(FIXTURE.relative_to(DESKTOP)).replace("\\", "/"),
            "source_file_sha256": digest,
            "cases": len(dataset.cases),
            "nodes": sum(len(case.nodes) for case in dataset.cases),
            "edges": sum(len(case.edges) for case in dataset.cases),
            "truth_links": sum(len(case.truth_links()) for case in dataset.cases),
            "scored_deposits": base_metrics["n"],
            "pools_evaluated": sum(len(pools) for pools in ev.pools.values()),
        },
        "baseline": {
            "six_dim_weights": {key: float(base_weights[key]) for key in FEATURES},
            "metrics": base_metrics,
            "declared_reference": DECLARED_BASELINE,
            "declared_reference_match": all(
                abs(float(base_metrics[key]) - float(DECLARED_BASELINE[key])) < 5e-5
                for key in ("p_at_1", "p_at_3", "mrr")
            ),
            "single_feature_baselines": single,
            "per_case": base["per_case"],
        },
        "B1_leave_one_out": b1,
        "B2_weight_perturbation": b2,
        "B3_feature_correlation": b3,
        "B4_signal_leave_one_out": b4,
        "B5_signal_collinearity": b5,
        "limits": [
            "全部数据为 algo2/generate_fixture.py 生成的合成评估集，真值来自构造过程；结论只在「归因一致性」口径下成立，不代表实战效果。",
            "p@1 在并列时按 1/并列数折算，因此 SIM-EVAL-02（固定面额池，结构不可解）会显著拉低均值，消融与扰动的差异也主要来自这一类案件。",
            "评估集只有 8 案 / 31 笔可评分入金，1 笔入金 = 0.0323 的 p@1；小于该步长的差异不宜解读为真实优劣。",
            "B4 的判别贡献只反映「真池均分下降 − 负池均分下降」，负池样本若来自另一个任务且生成口径不同，两者可比性有限。",
            "B3/B5 都是相关统计，不是重复计分的证明；共线只是「同一事实被同向奖励」的必要条件。",
        ],
    }
    payload["wall_time_seconds"] = _r(time.time() - started, 1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # —— 控制台摘要 ——
    print("=" * 78)
    print(f"基线复现：p@1={base_metrics['p_at_1']}  p@3={base_metrics['p_at_3']}  MRR={base_metrics['mrr']}  n={base_metrics['n']}")
    print(f"  与任务书基线一致：{payload['baseline']['declared_reference_match']}")
    print(f"  单特征基线：仅金额 p@1={single['仅金额匹配']['p_at_1']} / 仅时间 p@1={single['仅时间最近']['p_at_1']}")
    print("-" * 78)
    print("B1 六维离一消融（Δp@1 / Δp@3 / ΔMRR；档位与均分变化）")
    for row in b1["rows"]:
        print(f"  {row['label']:<6}{row['feature']:<14} 去掉 {row['removed_weight']:>4.0f}  "
              f"p@1={row['metrics']['p_at_1']:.4f} Δ={row['delta']['p_at_1']:+.4f}  "
              f"Δp@3={row['delta']['p_at_3']:+.4f}  ΔMRR={row['delta']['mrr']:+.4f}  "
              f"HIGH {row['tier_counts']['HIGH']:>3d}（Δ{row['tier_delta_HIGH']:+d}） 均分Δ={row['mean_score_delta']:+.2f}")
    print(f"  基线档位：{base['tier_counts']}  均分={base['mean_score']}")
    print(f"  承重：{b1['verdict']['load_bearing']}  边缘：{b1['verdict']['marginal']}  可删：{b1['verdict']['droppable_or_redundant']}")
    print("-" * 78)
    print("B2 权重扰动（各 200 次）")
    for level in b2["levels"]:
        d = level["p_at_1"]
        print(f"  a={level['a']}: p@1 mean={d['mean']:.4f} sd={d['sd']:.4f} "
              f"[p5={d['p5']:.4f}, p95={d['p95']:.4f}] min={d['min']:.4f} max={d['max']:.4f}")
        print(f"         p@3 mean={level['p_at_3']['mean']:.4f} [p5={level['p_at_3']['p5']:.4f}, p95={level['p_at_3']['p95']:.4f}]  "
              f"MRR mean={level['mrr']['mean']:.4f}")
        print(f"         翻转(低于仅金额基线 {single_p1})={level['flip']['count_vs_single_feature_baseline']} 次；"
              f"rank-1 变动入金 {level['rank1_stability']['ever_changed']}/{level['rank1_stability']['deposits']}，"
              f"平均稳定度={level['rank1_stability']['mean_stability']:.3f}")
        if level["rank1_stability"]["ever_changed"]:
            for row in level["per_deposit"][:3]:
                print(f"           最不稳 {row['case_id']} {row['deposit_edge']} 稳定度={row['rank1_stability']:.3f} "
                      f"候选={row['n_candidates']} 集合数={row['distinct_rank1_sets']}")
        else:
            print("           rank-1 无一变动")
        drift = level["tier_drift"]
        print(f"         档位漂移：{drift['links_ever_changing']}/{drift['links']} 条候选对"
              f"（{drift['share_ever_changing']*100:.1f}%）在 200 次里改过档；"
              f"HIGH 数 {drift['high_count']['min']}–{drift['high_count']['max']}（基线 {drift['high_count']['baseline']}）")
    inv = b2["invariance"]
    print(f"  不变性：{inv['n_deposits']} 笔入金中 {inv['n_immune']} 笔 top-1 全维压制（任何非负权重都不翻）、"
          f"{inv['n_needs_distortion']} 笔需要放大反向维度权重")
    print(f"    最小 required_distortion = {inv['min_required_distortion']}（中位 {inv['median_required_distortion']}）；"
          f"翻盘阈值 a > {inv['a_threshold']}")
    print(f"    解析判定 flip_possible_at = {inv['flip_possible_at']}")
    if inv["tightest"]:
        print(f"    最紧的一笔：{inv['tightest']['case_id']} {inv['tightest']['deposit_edge']} "
              f"gap={inv['tightest']['gap']} diff={inv['tightest']['diff']}")
    print("    离翻盘最近的 5 笔入金（required_distortion 升序）：")
    for row in b2["closest_to_flip"][:5]:
        print(f"      {row['case_id']} {row['deposit_edge']:<22} 需放大 {row['required_distortion']:.2f} 倍 "
              f"gap={row['gap']}")
    print("-" * 78)
    print(f"B3 六维 Spearman（{b3['n_pairs']} 对候选 / {b3['n_groups']} 组）")
    print("  " + " ".join(f"{key[:6]:>8}" for key in FEATURES))
    for i, key in enumerate(FEATURES):
        print(f"  {key[:12]:<12}" + " ".join(
            f"{('  --  ' if value is None else f'{value:+.3f}'):>8}" for value in b3["spearman_matrix"][i]))
    print(f"  >0.5 的对：{[ (r['a']+'-'+r['b'], r['spearman']) for r in b3['pairs_above_0_5']]}")
    print(f"  <−0.5 的对：{[ (r['a']+'-'+r['b'], r['spearman']) for r in b3['pairs_below_minus_0_5']]}")
    print(f"  重点：amount↔denomination={b3['focus']['amount_vs_denomination']}  fee_fit↔amount={b3['focus']['fee_fit_vs_amount']}")
    print("  — 组内（同一入金候选集内排秩）—")
    print("  " + " ".join(f"{key[:6]:>8}" for key in FEATURES))
    for i, key in enumerate(FEATURES):
        print(f"  {key[:12]:<12}" + " ".join(
            f"{('  --  ' if value is None else f'{value:+.3f}'):>8}" for value in b3["within_deposit"]["spearman_matrix"][i]))
    print(f"  >0.5 的对：{[ (r['a']+'-'+r['b'], r['spearman']) for r in b3['within_deposit']['pairs_above_0_5']]}")
    print(f"  <−0.5 的对：{[ (r['a']+'-'+r['b'], r['spearman']) for r in b3['within_deposit']['pairs_below_minus_0_5']]}")
    print(f"  重点：amount↔denomination={b3['within_deposit']['focus']['amount_vs_denomination']}  "
          f"fee_fit↔amount={b3['within_deposit']['focus']['fee_fit_vs_amount']}")
    print("-" * 78)
    print(f"B4 七信号离一消融（真池 {len(true_units)} 个；负池 {len(negative_units)} 个，跳过={b4['negative']['skipped']}；"
          f"真池均分 {b4['true_pool_baseline_mean']}，负池均分 {b4['negative']['baseline_mean']}）")
    for row in b4["ranking_by_discriminative_contribution"]:
        neg_text = "n/a" if row["neg_drop"] is None else format(row["neg_drop"], "+.2f")
        con_text = "n/a" if row["discriminative_contribution"] is None else format(
            row["discriminative_contribution"], "+.2f"
        )
        print(f"  {row['signal']} {row['label']:<10} 真池均分下降={row['true_drop']:+.2f}  负池下降={neg_text}  判别贡献={con_text}")
    print("-" * 78)
    print(f"B5 信号共线（{b5['n_pools']} 个池，零方差信号={b5['zero_variance_signals']}，命中数={b5['hit_counts']}）")
    print(f"  >0.5 的对：{[ (r['a']+'-'+r['b'], r['phi']) for r in b5['pairs_above_0_5']]}")
    print(f"  <−0.5 的对：{[ (r['a']+'-'+r['b'], r['phi']) for r in b5['pairs_below_minus_0_5']]}")
    tc = b2["threshold_check"]
    print("-" * 78)
    print(f"B2 阈值实证（解析阈值 a>{tc['analytic_threshold_a']}）")
    for row in tc["levels"]:
        print(f"  a={row['a']}: 200 次里 rank-1 变动 {row['runs_with_rank1_change']} 次，"
              f"受影响入金 {row['deposits_ever_changed']} 笔，p@1 {row['p_at_1']['min']}–{row['p_at_1']['max']}")
    print("=" * 78)
    print(f"已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

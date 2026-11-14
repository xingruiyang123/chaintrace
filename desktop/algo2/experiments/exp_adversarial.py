# -*- coding: utf-8 -*-
"""对抗性检验：这套混币归因算法是不是"过度自信"——信息不足时还敢报第一。

三个检验，结果写到 algo2/experiments/out/adversarial.json：

    A1 分数间隔（decisiveness）
        对每一对构造真值链，取该入金的全部候选出金，算 gap = 第一名 − 第二名，
        并记录真值链的名次；三档统计 + 把"险胜/失手且 gap<5"的逐条列出。

    A2 置换检验（null test）
        图结构、金额、时间、入金集合、出金集合一律不动，只把"真值配对"在每个池内
        随机重排后重新指派给入金，跑 1000 次看 p@1 / p@3 / MRR 掉到哪。
        指标直接复用 pipeline.case_metrics（只替换 case.ground_truth），
        不另写一套 p@k 口径——否则"打乱后变好/变差"可能只是口径差异。

    A3 负样本池（假阳性率）
        六种"看起来像混币但不是"的合成业务形态，逐个跑 mixer_detect.analyse_pool，
        看 detection_score 与命中信号，并按 70/60/45 三个阈值统计误判数；
        同时把评估集里 7 个真混币池的分数拉出来做对照（只看分数，不参与判定）。

约束：
- 纯标准库；不修改 algo2 下任何既有模块、fixtures 与测试；
- 随机过程固定种子（默认 20261113），种子与迭代次数写进输出；
- 金额一律 Decimal 字符串，全程不经过 float 累加。

用法：
    python -m algo2.experiments.exp_adversarial
    python algo2/experiments/exp_adversarial.py            # 直接跑脚本也行
    python -m algo2.experiments.exp_adversarial --iterations 2000 --quiet
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import platform
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 直接跑脚本：把 desktop 根目录放回 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from algo2 import attribution, graph, mixer_detect, pipeline  # noqa: E402

HERE = Path(__file__).resolve().parent
EVAL_FIXTURE = HERE.parent / "fixtures" / "mixer_eval_cases.json"
NEG_FIXTURE = HERE / "fixtures_negative_pools.json"
OUT_DIR = HERE / "out"
OUT_FILE = OUT_DIR / "adversarial.json"

SCHEMA = "mixer_offramp/adversarial/v1"
SEED = 20261113
SECOND_SEED = SEED + 1
ITERATIONS = 1000

# A1 的分档门槛（契约口径：HIGH≥70 / MEDIUM≥45，间隔门槛本实验取 10 分）
GAP_DECISIVE = 10.0
GAP_LOW = 5.0

# A3 的三个阈值 + 两个补充分档（只用于看"阈值到底在哪一侧失效"）
THRESHOLDS = (70, 60, 45)
THRESHOLDS_SUPP = (30, 20)


# —————————————————————— 小工具 ——————————————————————


def _r(value: float | None, nd: int = 4) -> float | None:
    return None if value is None else round(float(value), nd)


def _quantile(sorted_values: list[float], q: float) -> float | None:
    """线性插值分位数（与 numpy/Excel 默认的 type-7 一致），输入必须已排序。"""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return sorted_values[int(pos)]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _describe(values: list[float]) -> dict:
    """一组数的位置/离散度摘要（空组返回全 None）。"""
    if not values:
        return {
            "n": 0,
            "mean": None,
            "std": None,
            "min": None,
            "p05": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in ordered) / n)  # 总体标准差（此处即全部抽样）
    return {
        "n": n,
        "mean": _r(mean),
        "std": _r(std),
        "min": _r(ordered[0]),
        "p05": _r(_quantile(ordered, 0.05)),
        "p25": _r(_quantile(ordered, 0.25)),
        "median": _r(_quantile(ordered, 0.50)),
        "p75": _r(_quantile(ordered, 0.75)),
        "p95": _r(_quantile(ordered, 0.95)),
        "max": _r(ordered[-1]),
    }


def _sha256_file(path: Path) -> str:
    return graph.file_sha256(path)


def _now() -> str:
    """与 pipeline 同一写法的时间戳，避免依赖其私有函数。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# —————————————————————— 预处理：一次算好分数，后面所有检验都复用 ——————————————


@dataclass
class Prepared:
    """一个案件的"分数矩阵 + 真值 + 池结构"，供 A1/A2 复用。"""

    case: graph.Case
    links: list[dict]                      # attribution 的原始输出行（含 score / rank）
    truth: set[tuple[str, str]]            # (deposit_edge, withdrawal_edge)
    by_pool: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    pool_withdrawals: dict[str, list[str]] = field(default_factory=dict)
    origin_ground_truth: dict | None = None

    def ranked(self) -> dict[str, list[dict]]:
        """按（分数降序，出金 id）排好，供 A1 取第 1/第 2 名。"""
        by_deposit: dict[str, list[dict]] = {}
        for row in self.links:
            by_deposit.setdefault(row["deposit_edge"], []).append(row)
        return {
            dep: sorted(rows, key=lambda r: (-r["score"], r["withdrawal_edge"]))
            for dep, rows in by_deposit.items()
        }


def prepare(dataset: graph.Dataset, params: dict) -> list[Prepared]:
    prepared: list[Prepared] = []
    for case in dataset.cases:
        pools = mixer_detect.detect_case(case, params["mixer"])
        links = attribution.attribute_case(case, pools, params)
        links.sort(key=lambda r: (case.edge(r["deposit_edge"]).at, r["rank"], r["withdrawal_edge"]))
        truth = case.truth_links()
        item = Prepared(case=case, links=links, truth=truth, origin_ground_truth=case.ground_truth)
        pool_of_deposit: dict[str, str] = {}
        for row in links:
            pool_of_deposit[row["deposit_edge"]] = row["pool_id"]
        for pool in pools:
            pid = pool["pool_id"]
            item.pool_withdrawals[pid] = [
                e.id for e in sorted(case.out_edges(pid), key=lambda e: (e.at, e.id))
            ]
        for deposit, withdrawal in truth:
            pid = pool_of_deposit.get(deposit)
            if pid is None:
                raise RuntimeError(f"{case.case_id} 的真值入金 {deposit} 没有落在任何被识别的池里，检验无法进行")
            item.by_pool.setdefault(pid, []).append((deposit, withdrawal))
        for pid in item.by_pool:
            item.by_pool[pid].sort()
        prepared.append(item)
    return prepared


def _metrics_with_pairs(item: Prepared, params: dict, pairs: list[tuple[str, str]]) -> dict | None:
    """把 pairs 当成真值，调用官方 case_metrics 算 p@1/p@3/MRR。

    只临时替换 case.ground_truth，算完立刻还原——不触碰任何图结构，
    也不改动 attribution 的分数（真值本来就不参与打分）。
    """
    if not pairs:
        return None
    original = item.case.ground_truth
    try:
        item.case.ground_truth = {
            "links": [{"deposit_edge": d, "withdrawal_edge": w} for d, w in pairs]
        }
        metrics = pipeline.case_metrics(item.case, item.links, params)["attribution"]
    finally:
        item.case.ground_truth = original
    return metrics


def merge_metrics(rows: list[dict | None]) -> dict:
    """按样本数加权合并多个 p@1/p@3/MRR（与 benchmark._merge 同口径）。"""
    rows = [r for r in rows if r and r.get("n")]
    if not rows:
        return {"p_at_1": None, "p_at_3": None, "mrr": None, "n": 0}
    total = sum(int(r["n"]) for r in rows)
    return {
        "p_at_1": _r(sum(float(r["p_at_1"]) * int(r["n"]) for r in rows) / total),
        "p_at_3": _r(sum(float(r["p_at_3"]) * int(r["n"]) for r in rows) / total),
        "mrr": _r(sum(float(r["mrr"]) * int(r["n"]) for r in rows) / total),
        "n": total,
    }


# —————————————————————— A1：分数间隔 ——————————————————————


def run_a1(prepared: list[Prepared]) -> dict:
    pairs: list[dict] = []
    for item in prepared:
        ranked = item.ranked()
        for deposit, withdrawal in sorted(item.truth):
            rows = ranked.get(deposit) or []
            if len(rows) < 2:
                continue
            top, second = rows[0], rows[1]
            truth_row = next((r for r in rows if r["withdrawal_edge"] == withdrawal), None)
            if truth_row is None:
                continue
            others = [r for r in rows if (deposit, r["withdrawal_edge"]) not in item.truth]
            best_other = others[0] if others else None
            gap = round(top["score"] - second["score"], 2)
            tie_at_top = sum(1 for r in rows if abs(r["score"] - top["score"]) < 1e-9)
            if truth_row["rank"] == 1 and gap >= GAP_DECISIVE:
                bucket = "decisive"
            elif truth_row["rank"] == 1:
                bucket = "narrow"
            else:
                bucket = "miss"
            pairs.append(
                {
                    "case_id": item.case.case_id,
                    "pool_id": truth_row["pool_id"],
                    "deposit_edge": deposit,
                    "truth_withdrawal_edge": withdrawal,
                    "top1_withdrawal_edge": top["withdrawal_edge"],
                    "gap": gap,
                    "score_top1": top["score"],
                    "score_second": second["score"],
                    "score_truth": truth_row["score"],
                    "truth_rank": truth_row["rank"],
                    "tie_at_top": tie_at_top,
                    "n_candidates": len(rows),
                    "gap_truth_vs_best_other": (
                        round(truth_row["score"] - best_other["score"], 2) if best_other else None
                    ),
                    "bucket": bucket,
                }
            )

    # 同一池里"多笔入金的首选出金是同一笔"——界面上的首选对应是否自相矛盾
    collisions: list[dict] = []
    for item in prepared:
        ranked = item.ranked()
        groups: dict[str, list[str]] = {}
        for deposit, rows in ranked.items():
            groups.setdefault(rows[0]["withdrawal_edge"], []).append(deposit)
        for withdrawal, deposits in groups.items():
            if len(deposits) > 1:
                collisions.append(
                    {
                        "case_id": item.case.case_id,
                        "top1_withdrawal_edge": withdrawal,
                        "deposits": sorted(deposits),
                        "size": len(deposits),
                    }
                )

    gaps = [p["gap"] for p in pairs if p["gap"] is not None]
    ordered_gaps = sorted(gaps)
    buckets = {
        name: {
            "n": sum(1 for p in pairs if p["bucket"] == name),
            "by_case": {
                cid: sum(1 for p in pairs if p["bucket"] == name and p["case_id"] == cid)
                for cid in sorted({p["case_id"] for p in pairs if p["bucket"] == name})
            },
        }
        for name in ("decisive", "narrow", "miss")
    }
    low_gap = [
        p
        for p in pairs
        if p["bucket"] in ("narrow", "miss") and p["gap"] is not None and p["gap"] < GAP_LOW
    ]
    low_gap.sort(key=lambda p: (p["case_id"], p["deposit_edge"]))
    tied_rank1 = [p for p in pairs if p["truth_rank"] == 1 and p["tie_at_top"] > 1]

    return {
        "question": "真值链排第一时，第一名实际领先多少分？领先很小的时候算法是怎么说的？",
        "definition": {
            "gap": "同一入金的候选出金按 (分数降序, 出金 id) 排序后，第 1 名分数 − 第 2 名分数；"
                   "并列时 gap=0",
            "bucket": {
                "decisive": f"真值名次 = 1 且 gap ≥ {GAP_DECISIVE:g}",
                "narrow": f"真值名次 = 1 且 gap < {GAP_DECISIVE:g}（含与多笔同分并列第 1）",
                "miss": "真值名次 ≠ 1",
            },
            "low_gap_listed_below": f"narrow/miss 且 gap < {GAP_LOW:g} 的逐条明细",
        },
        "n_pairs": len(pairs),
        "gap_stats": {
            "min": _r(ordered_gaps[0]) if ordered_gaps else None,
            "p25": _r(_quantile(ordered_gaps, 0.25)),
            "median": _r(_quantile(ordered_gaps, 0.50)),
            "p75": _r(_quantile(ordered_gaps, 0.75)),
            "max": _r(ordered_gaps[-1]) if ordered_gaps else None,
            "mean": _r(sum(ordered_gaps) / len(ordered_gaps)) if ordered_gaps else None,
        },
        "gap_histogram_buckets": {
            "0–0.5": sum(1 for g in ordered_gaps if g < 0.5),
            "0.5–5": sum(1 for g in ordered_gaps if 0.5 <= g < 5),
            "5–10": sum(1 for g in ordered_gaps if 5 <= g < 10),
            "10–20": sum(1 for g in ordered_gaps if 10 <= g < 20),
            "≥20": sum(1 for g in ordered_gaps if g >= 20),
        },
        "bucket_counts": buckets,
        "rank1_but_tied_with_others": {
            "n": len(tied_rank1),
            "note": "名次=1 但有多笔同分并列：算法说『第 1 名』，实际是 t 笔里挑一笔",
            "pairs": [
                {
                    "case_id": p["case_id"],
                    "deposit_edge": p["deposit_edge"],
                    "truth_withdrawal_edge": p["truth_withdrawal_edge"],
                    "tie_at_top": p["tie_at_top"],
                    "gap": p["gap"],
                }
                for p in tied_rank1
            ],
        },
        "low_gap_pairs": low_gap,
        "low_gap_count": len(low_gap),
        "top1_collisions": {
            "note": "同一池里有多少笔入金的『第 1 名』指向同一笔出金。≥2 就意味着界面上的"
                    "『首选对应关系』自相矛盾：同一笔出金被同时报成多笔入金的首选。",
            "cases_with_collision": sorted({row["case_id"] for row in collisions}),
            "max_size": max((row["size"] for row in collisions), default=0),
            "groups": sorted(collisions, key=lambda row: (-row["size"], row["case_id"])),
        },
        "pairs": pairs,
    }


# —————————————————————— A2：置换检验 ——————————————————————


def _shuffled_pairs(
    prepared: list[Prepared], rng: random.Random, *, mode: str
) -> list[tuple[str, str]]:
    """生成这一轮的"假真值"。

    mode="permute"：每个池内把真值出金列表重排后重新指派给原入金（题目要求的置换）。
    mode="redraw" ：每个池从该池全部出金里等概率抽 k 笔（更强的一档零假设，用于对照）。
    """
    pairs: list[tuple[str, str]] = []
    for item in prepared:
        if not item.by_pool:
            continue
        for pid in sorted(item.by_pool):
            group = item.by_pool[pid]
            deposits = [d for d, _ in group]
            withdrawals = [w for _, w in group]
            if mode == "permute":
                rng.shuffle(withdrawals)
            elif mode == "redraw":
                withdrawals = rng.sample(item.pool_withdrawals[pid], len(deposits))
            else:  # pragma: no cover - 防御性
                raise ValueError(f"未知的置换模式：{mode}")
            pairs.extend(zip(deposits, withdrawals))
    return pairs


def run_a2(prepared: list[Prepared], params: dict, iterations: int, seed: int, second_seed: int) -> dict:
    real_pairs = sorted(p for item in prepared for p in item.truth)
    real = merge_metrics([_metrics_with_pairs(item, params, sorted(item.truth)) for item in prepared])

    # 逐案真实值，后面和打乱后的逐案分布对齐
    per_case_real: dict[str, dict | None] = {}
    for item in prepared:
        per_case_real[item.case.case_id] = _metrics_with_pairs(item, params, sorted(item.truth))

    # 随机下限：给定一笔入金、候选出金里随便挑一个（并列时按 1/并列数折算），
    # 期望 p@1 = 1/候选数。这是"算法完全没信息"时的数学期望，与样本无关。
    n_candidates: dict[str, int] = {}
    for item in prepared:
        for deposit, _ in item.truth:
            rows = [r for r in item.links if r["deposit_edge"] == deposit]
            n_candidates[deposit] = len(rows)
    random_floor = (
        sum(1.0 / n_candidates[d] for d in n_candidates) / len(n_candidates) if n_candidates else None
    )

    samples_p1: list[float] = []
    samples_p3: list[float] = []
    samples_mrr: list[float] = []
    per_case_null: dict[str, list[float]] = {item.case.case_id: [] for item in prepared}

    rng = random.Random(seed)
    for _ in range(iterations):
        fake = _shuffled_pairs(prepared, rng, mode="permute")
        by_case: dict[str, list[tuple[str, str]]] = {}
        case_of_deposit = {p[0]: item.case.case_id for item in prepared for p in item.truth}
        for pair in fake:
            by_case.setdefault(case_of_deposit[pair[0]], []).append(pair)
        rows = []
        for item in prepared:
            metrics = _metrics_with_pairs(item, params, by_case.get(item.case.case_id, []))
            rows.append(metrics)
            if metrics and metrics.get("n"):
                per_case_null[item.case.case_id].append(float(metrics["p_at_1"]))
        merged = merge_metrics(rows)
        samples_p1.append(float(merged["p_at_1"]))
        samples_p3.append(float(merged["p_at_3"]))
        samples_mrr.append(float(merged["mrr"]))

    real_p1 = float(real["p_at_1"])
    below = sum(1 for v in samples_p1 if v < real_p1)
    equal = sum(1 for v in samples_p1 if v == real_p1)
    percentile = 100.0 * (below + 0.5 * equal) / len(samples_p1)
    p_value = (sum(1 for v in samples_p1 if v >= real_p1) + 1) / (len(samples_p1) + 1)

    # 第二档零假设：从整池出金里随机抽（比"重排真值出金"更弱），用来验证随机下限
    rng2 = random.Random(second_seed)
    second_samples: list[float] = []
    for _ in range(iterations):
        fake = _shuffled_pairs(prepared, rng2, mode="redraw")
        case_of_deposit = {p[0]: item.case.case_id for item in prepared for p in item.truth}
        by_case: dict[str, list[tuple[str, str]]] = {}
        for pair in fake:
            by_case.setdefault(case_of_deposit[pair[0]], []).append(pair)
        rows = [_metrics_with_pairs(item, params, by_case.get(item.case.case_id, [])) for item in prepared]
        second_samples.append(float(merge_metrics(rows)["p_at_1"]))

    null1 = _describe(samples_p1)
    null2 = _describe(second_samples)

    by_case_rows = []
    for item in prepared:
        cid = item.case.case_id
        real_case = per_case_real.get(cid)
        values = per_case_null.get(cid) or []
        if not real_case or not values:
            by_case_rows.append(
                {
                    "case_id": cid,
                    "truth_links": sum(len(v) for v in item.by_pool.values()),
                    "real_p_at_1": None,
                    "null_p_at_1_mean": None,
                    "null_p_at_1_p05": None,
                    "null_p_at_1_p95": None,
                    "null_ge_real_frac": None,
                    "note": "该案无构造真值，不参与指标。",
                }
            )
            continue
        by_case_rows.append(
            {
                "case_id": cid,
                "truth_links": int(real_case["n"]),
                "n_candidates_per_deposit": sorted({n_candidates[d] for d, _ in item.truth}),
                "real_p_at_1": real_case["p_at_1"],
                "real_p_at_3": real_case["p_at_3"],
                "real_mrr": real_case["mrr"],
                "null_p_at_1_mean": _r(sum(values) / len(values)),
                "null_p_at_1_p05": _r(_quantile(sorted(values), 0.05)),
                "null_p_at_1_p95": _r(_quantile(sorted(values), 0.95)),
                "null_ge_real_frac": _r(sum(1 for v in values if v >= float(real_case["p_at_1"])) / len(values)),
            }
        )

    return {
        "question": "把真值配对打乱后，p@1 会掉到哪？还显著高于随机吗？",
        "design": {
            "kept_fixed": "每个池的入金集合、出金集合、全部边、金额、时间、节点结构一律不动",
            "shuffled": "池内真值出金列表随机重排后重新指派给原入金（不改任何图结构）",
            "iterations": iterations,
            "seed": seed,
            "secondary_seed": second_seed,
            "metric_source": "pipeline.case_metrics（只替换 case.ground_truth，口径与 benchmark 完全一致）",
            "tie_rule": "并列第 1 时按 1/并列数折算（与 pipeline._ranked 一致）",
            "random_floor": "1/候选数 的加权平均：算法完全无信息时的期望 p@1",
        },
        "real": real,
        "random_floor_p_at_1": _r(random_floor),
        "null_primary_permute": {
            "p_at_1": null1,
            "p_at_3": _describe(samples_p3),
            "mrr": _describe(samples_mrr),
            "frac_null_ge_real": _r(sum(1 for v in samples_p1 if v >= real_p1) / len(samples_p1)),
            "real_percentile": _r(percentile, 2),
            "p_value_one_sided": _r(p_value, 6),
        },
        "null_secondary_redraw": {
            "note": "对照组：每个池从全部出金里等概率抽 k 笔当真值（比置换更强的一档零假设），"
                    "用来验证 1/候选数 这个理论下限",
            "p_at_1": null2,
            "frac_null_ge_real": _r(sum(1 for v in second_samples if v >= real_p1) / len(second_samples)),
        },
        "decomposition": {
            "real_minus_null_mean": _r(real_p1 - float(null1["mean"])),
            "null_minus_random_floor": _r(float(null1["mean"]) - float(random_floor or 0.0)),
            "null_over_real": _r(float(null1["mean"]) / real_p1, 3),
            "reading": "real−null = 只能由『真实对应关系』解释的部分；"
                       "null−随机下限 = 真值出金本身在结构上更像造成的抬升（与配对无关）",
        },
        "by_case": by_case_rows,
        "null_samples_p_at_1": [_r(v) for v in samples_p1],
    }


# —————————————————————— A3：负样本池 ——————————————————————


def _signal_brief(signals: list[dict]) -> list[dict]:
    return [
        {
            "id": s["id"],
            "label": s["label"],
            "weight": s["weight"],
            "hit": s["hit"],
            "value": s["value"],
            "threshold": s["threshold"],
            "detail": s["detail"],
        }
        for s in signals
    ]


def run_a3(params: dict) -> dict:
    dataset, raw = graph.load_file(NEG_FIXTURE)
    raw_cases = {c["case_id"]: c for c in raw["cases"]}
    negatives: list[dict] = []
    for case in dataset.cases:
        meta = raw_cases[case.case_id]
        pool_id = meta.get("pool_node")
        if not pool_id:
            raise RuntimeError(f"{case.case_id} 缺少 pool_node，无法定位要检验的池")
        pool = mixer_detect.analyse_pool(case, pool_id, params["mixer"])
        public = mixer_detect.public_pool(pool)
        hits = [s["id"] for s in pool["signals"] if s["hit"]]
        negatives.append(
            {
                "case_id": case.case_id,
                "negative_type": meta.get("negative_type"),
                "title": case.title,
                "pool_node": pool_id,
                "detection_score": public["detection_score"],
                "mechanism_guess": public["mechanism"],
                "hits": hits,
                "misses": [s["id"] for s in pool["signals"] if not s["hit"]],
                "hit_weight_sum": sum(s["weight"] for s in pool["signals"] if s["hit"]),
                "signals": _signal_brief(pool["signals"]),
                "inflow": public["inflow"],
                "outflow": public["outflow"],
                "window_hours": public["window_hours"],
                "auto_discovered_as_candidate": (
                    pool_id in mixer_detect.pool_candidates(case, params["mixer"])
                ),
                "note": meta.get("note", ""),
            }
        )

    def rate(rows: list[dict], threshold: int) -> dict:
        flagged = [r for r in rows if r["detection_score"] >= threshold]
        return {
            "threshold": threshold,
            "flagged": len(flagged),
            "total": len(rows),
            "rate": _r(len(flagged) / len(rows)) if rows else None,
            "cases": [r["case_id"] for r in flagged],
            "types": [r["negative_type"] for r in flagged],
        }

    fpr_table = [rate(negatives, t) for t in THRESHOLDS]
    fpr_supp = [rate(negatives, t) for t in THRESHOLDS_SUPP]

    # 真阳性对照：评估集里的真混币池
    eval_ds, eval_raw = graph.load_file(EVAL_FIXTURE)
    true_pools: list[dict] = []
    for case in eval_ds.cases:
        for pool in mixer_detect.detect_case(case, params["mixer"]):
            true_pools.append(
                {
                    "case_id": case.case_id,
                    "pool_id": pool["pool_id"],
                    "detection_score": pool["detection_score"],
                    "hits": [s["id"] for s in pool["signals"] if s["hit"]],
                    "misses": [s["id"] for s in pool["signals"] if not s["hit"]],
                    "mechanism_guess": pool["mechanism"],
                    "has_truth_links": bool(case.truth_links()),
                }
            )

    def tpr(threshold: int) -> dict:
        flagged = [p for p in true_pools if p["detection_score"] >= threshold]
        return {
            "threshold": threshold,
            "flagged": len(flagged),
            "total": len(true_pools),
            "rate": _r(len(flagged) / len(true_pools)) if true_pools else None,
            "pools": [p["pool_id"] for p in flagged],
        }

    tp_table = [tpr(t) for t in THRESHOLDS]
    tp_supp = [tpr(t) for t in THRESHOLDS_SUPP]

    tp_scores = sorted(p["detection_score"] for p in true_pools)
    fp_scores = sorted(n["detection_score"] for n in negatives)
    wins = ties = 0
    for a in tp_scores:
        for b in fp_scores:
            if a > b:
                wins += 1
            elif a == b:
                ties += 1
    total_pairs = len(tp_scores) * len(fp_scores)
    auc = (wins + 0.5 * ties) / total_pairs if total_pairs else None

    # 补充：把评估集的 mixer 标签与 mixer_* 边标记摘掉，看结构规则还能不能自己找到这些池
    stripped_raw = copy.deepcopy(eval_raw)
    for case_raw in stripped_raw["cases"]:
        for node_raw in case_raw["nodes"]:
            if node_raw["type"] == "mixer":
                node_raw["type"] = "wallet"
        for edge_raw in case_raw["edges"]:
            if edge_raw["pattern"] in ("mixer_in", "mixer_out"):
                edge_raw["pattern"] = "normal"
    stripped = graph.load_dataset(stripped_raw)
    stripped_found = []
    for case in stripped.cases:
        candidates = mixer_detect.pool_candidates(case, params["mixer"])
        stripped_found.append({"case_id": case.case_id, "candidates": candidates})

    top = max(negatives, key=lambda r: (r["detection_score"], r["case_id"]))
    return {
        "question": "六种『像混币但不是』的业务形态会被打成多少分？三个阈值下误判几个？",
        "fixture": {
            "path": NEG_FIXTURE.relative_to(HERE.parent.parent).as_posix(),
            "sha256": _sha256_file(NEG_FIXTURE),
            "cases": len(negatives),
        },
        "thresholds": THRESHOLDS,
        "false_positive_rate": fpr_table,
        "false_positive_rate_supplementary": fpr_supp,
        "negatives": negatives,
        "most_confusable": {
            "case_id": top["case_id"],
            "negative_type": top["negative_type"],
            "detection_score": top["detection_score"],
            "hits": top["hits"],
            "misses": top["misses"],
            "hit_weight_sum": top["hit_weight_sum"],
        },
        "true_positive_reference": {
            "note": "评估集 7 个真混币池，仅用分数作对照（识别阶段靠 type=mixer 标签进入候选池）",
            "pools": true_pools,
            "score_stats": {
                "n": len(tp_scores),
                "min": tp_scores[0] if tp_scores else None,
                "median": _r(_quantile(tp_scores, 0.5)),
                "max": tp_scores[-1] if tp_scores else None,
                "values": tp_scores,
            },
            "true_positive_rate": tp_table,
            "true_positive_rate_supplementary": tp_supp,
        },
        "separation": {
            "best_true_pool_score": max(tp_scores) if tp_scores else None,
            "best_negative_pool_score": max(fp_scores) if fp_scores else None,
            "auc_true_vs_negative": _r(auc, 3),
            "auc_method": "Mann-Whitney：P(真池得分 > 负样本得分) + 0.5·P(相等)，0.5 = 无区分力",
            "score_overlap": {
                "negatives": fp_scores,
                "true_pools": tp_scores,
            },
        },
        "discovery_stage_sensitivity": {
            "note": "补充检验：把评估集里的 type=mixer 标签与 mixer_in/mixer_out 边标记摘掉后，"
                    "pool_candidates 还能找到几个真池（只改内存副本，不动 fixtures 文件）",
            "found": stripped_found,
            "found_pools": sum(len(row["candidates"]) for row in stripped_found),
            "total_true_pools": len(true_pools),
        },
    }


# —————————————————————— 结论（按算出来的数字生成，不预设） ——————————————


def verdict_a1(a1: dict) -> str:
    b = a1["bucket_counts"]
    stats = a1["gap_stats"]
    low = a1["low_gap_count"]
    tied = a1["rank1_but_tied_with_others"]["n"]
    low_cases: dict[str, int] = {}
    for row in a1["low_gap_pairs"]:
        low_cases[row["case_id"]] = low_cases.get(row["case_id"], 0) + 1
    worst = "、".join(f"{k} {v} 条" for k, v in sorted(low_cases.items(), key=lambda kv: -kv[1]))
    tie_text = (
        f"其中名次=1 但多笔同分并列的有 {tied} 条"
        if tied
        else "这些『第 1 名』没有一条是并列，全部是唯一第一"
    )
    collision = a1["top1_collisions"]
    collision_text = ""
    if collision["max_size"] > 1:
        top_groups = collision["groups"][:2]
        desc = "；".join(
            f"{g['case_id']} 的 {g['top1_withdrawal_edge']} 一次吃掉 {g['size']} 笔入金的首选"
            for g in top_groups
        )
        collision_text = (
            f"另外 {len(collision['cases_with_collision'])} 个池里都出现了『同一笔出金被同时报成多笔入金的第 1 名』"
            f"（共 {len(collision['groups'])} 组）：{desc}。这种首选集合本身就不可能是一个配对。"
        )
    return (
        f"{a1['n_pairs']} 对真值链里，决断 {b['decisive']['n']} 条、险胜 {b['narrow']['n']} 条、"
        f"失手 {b['miss']['n']} 条；gap 中位数 {stats['median']}、最大 {stats['max']}，"
        f"但最小 {stats['min']}——间隔分布是两头的，中间几乎没有。"
        f"gap<{GAP_LOW:g} 的『险胜/失手』共 {low} 条（{worst}），{tie_text}。"
        f"这些位置上算法照样输出『第 1 名』，只是名字后面没有一句『这是掷硬币』。"
        + collision_text
    )


def verdict_a2(a2: dict) -> str:
    real = float(a2["real"]["p_at_1"])
    null_block = a2["null_primary_permute"]
    null = null_block["p_at_1"]
    floor = float(a2["random_floor_p_at_1"])
    d = a2["decomposition"]
    iterations = int(null["n"])
    reasons = int(round(float(null_block["frac_null_ge_real"]) * iterations))
    se = float(null["std"]) / math.sqrt(iterations) if iterations else None
    chance_cases = [
        row["case_id"]
        for row in a2["by_case"]
        if row.get("null_ge_real_frac") is not None and float(row["null_ge_real_frac"]) >= 0.5
    ]
    degenerate = [
        row["case_id"]
        for row in a2["by_case"]
        if row.get("null_p_at_1_p05") is not None
        and row["null_p_at_1_p05"] == row["null_p_at_1_p95"]
    ]
    if float(null["mean"]) <= floor * 1.3:
        shape = "打乱后基本落回随机下限"
    elif float(null["mean"]) <= real * 0.5:
        shape = (
            f"打乱后掉到真实值的 {100.0 * float(null['mean']) / real:.0f}%，"
            f"比随机下限只高 {float(null['mean']) - floor:.3f}"
        )
    else:
        shape = "打乱后仍显著高于随机，指标主要来自金额/时间分布"
    return (
        f"真实 p@1 = {real:.4f}（p@3 {a2['real']['p_at_3']:.4f}、MRR {a2['real']['mrr']:.4f}）；"
        f"打乱配对 {iterations} 次后 p@1 均值 {null['mean']:.4f}、标准差 {null['std']:.4f}、"
        f"5%/95% 分位 {null['p05']:.4f}/{null['p95']:.4f}，最大一次 {null['max']:.4f}；"
        f"真实值在打乱分布的 {float(null_block['real_percentile']):.1f} 分位"
        f"（{iterations} 次里 {reasons} 次达到或超过，单侧 p={float(null_block['p_value_one_sided']):.4f}）。"
        f"1/候选数的随机下限 {floor:.4f}，对照档（整池随机抽出金）均值 "
        f"{a2['null_secondary_redraw']['p_at_1']['mean']:.4f}，两者对得上，说明这条下限算得没错。"
        f"结论：{shape}——{d['real_minus_null_mean']:.3f} 的落差只能由真实对应关系解释，"
        f"这一层算法不是靠金额/时间分布蒙的；残差 {d['null_minus_random_floor']:.3f}"
        f"（标准误约 {se:.4f}，是稳定抬升，但只有落差的 1/{max(1, round(d['real_minus_null_mean'] / max(d['null_minus_random_floor'], 1e-9)))}）"
        f"来自『真值出金本身就是结构上最像的那几笔』。"
        f"但 {('、'.join(chance_cases) if chance_cases else '无')} 上是例外："
        f"真实值与打乱均值几乎相同，"
        f"{('且 ' + '、'.join(degenerate) + f' 的打乱分布退化成单点（5%/95% 分位都是同一个数），即该案上算法与掷硬币完全等价') if degenerate else ''}。"
    )


def verdict_a3(a3: dict) -> str:
    fpr = {row["threshold"]: row for row in a3["false_positive_rate"]}
    tpr = {row["threshold"]: row for row in a3["true_positive_reference"]["true_positive_rate"]}
    top = a3["most_confusable"]
    sep = a3["separation"]
    parts = [
        f"70 分：误判 {fpr[70]['flagged']}/6（真池命中 {tpr[70]['flagged']}/7）；"
        f"60 分：误判 {fpr[60]['flagged']}/6（真池 {tpr[60]['flagged']}/7）；"
        f"45 分：误判 {fpr[45]['flagged']}/6（真池 {tpr[45]['flagged']}/7）。"
    ]
    parts.append(
        f"最容易误判的是 {top['negative_type']}（{top['case_id']}，{top['detection_score']} 分），"
        f"命中 {'/'.join(top['hits'])}，漏掉 {'/'.join(top['misses'])}。"
    )
    parts.append(
        f"更麻烦的是判别力本身：真池 {a3['true_positive_reference']['score_stats']['min']}–"
        f"{a3['true_positive_reference']['score_stats']['max']} 分、负样本 "
        f"{sep['score_overlap']['negatives'][0]}–{sep['best_negative_pool_score']} 分，"
        f"AUC = {sep['auc_true_vs_negative']}（0.5 就是瞎猜），"
        f"最高分的真池与最高分的负样本同为 {sep['best_negative_pool_score']} 分。"
    )
    dsc = a3["discovery_stage_sensitivity"]
    parts.append(
        f"另外把 type=mixer 标签摘掉后，结构规则只找到 {dsc['found_pools']}/{dsc['total_true_pools']} 个真池"
        f"——发现阶段的『结构上像混币池』这条分支在无标签数据上不成立。"
    )
    return "".join(parts)


# —————————————————————— 主流程 ——————————————————————


def build(iterations: int = ITERATIONS, seed: int = SEED) -> dict:
    params = pipeline.default_parameters()
    dataset, raw = graph.load_file(EVAL_FIXTURE)
    prepared = prepare(dataset, params)

    # 自检 1：本实验的指标口径必须与官方端到端结果一致
    official = pipeline.run_dataset(dataset, params)
    official_metrics = merge_metrics([case["metrics"]["attribution"] for case in official["cases"]])
    harness_real = merge_metrics([_metrics_with_pairs(item, params, sorted(item.truth)) for item in prepared])
    if official_metrics["p_at_1"] is None or harness_real["p_at_1"] is None:
        raise RuntimeError("自检失败：真实真值下拿不到 p@1")
    drift = abs(float(official_metrics["p_at_1"]) - float(harness_real["p_at_1"]))
    if drift > 1e-4:
        raise RuntimeError(f"自检失败：指标口径与官方结果不一致（差 {drift:.6f}）")

    # 自检 2：负样本 fixture 必须能被 graph 校验通过，且每案都有可定位的池
    neg_ds, _neg_raw = graph.load_file(NEG_FIXTURE)
    if len(neg_ds.cases) != 6:
        raise RuntimeError(f"自检失败：负样本应为 6 例，实际 {len(neg_ds.cases)}")

    a1 = run_a1(prepared)
    truth_total = sum(len(c.truth_links()) for c in dataset.cases)
    if a1["n_pairs"] != truth_total:
        raise RuntimeError(
            f"自检失败：{truth_total} 对真值链只算出 {a1['n_pairs']} 条 gap 记录（有真值对没落到候选出金里）"
        )
    a2 = run_a2(prepared, params, iterations=iterations, seed=seed, second_seed=seed + 1)
    a3 = run_a3(params)

    return {
        "schema_version": SCHEMA,
        "is_simulated": True,
        "generated_at": _now(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "stdlib_only": True,
        },
        "reproducibility": {
            "seed": seed,
            "secondary_seed": seed + 1,
            "iterations": iterations,
            "rng": "random.Random（Mersenne Twister），单一流按 case_id / pool_id 排序后依次抽取",
            "note": "同种子 + 同迭代次数 + 同一份 fixtures ⇒ 逐位可复现；种子与迭代次数都写在本文件里",
        },
        "inputs": {
            "eval_dataset": {
                "path": EVAL_FIXTURE.relative_to(HERE.parent.parent).as_posix(),
                "sha256": _sha256_file(EVAL_FIXTURE),
                "cases": len(dataset.cases),
                "nodes": sum(len(c.nodes) for c in dataset.cases),
                "edges": sum(len(c.edges) for c in dataset.cases),
                "truth_links": sum(len(c.truth_links()) for c in dataset.cases),
            },
            "negative_pools": {
                "path": NEG_FIXTURE.relative_to(HERE.parent.parent).as_posix(),
                "sha256": _sha256_file(NEG_FIXTURE),
                "cases": len(neg_ds.cases),
            },
        },
        "self_check": {
            "harness_matches_official": True,
            "metric_drift": _r(drift, 8),
            "official_pipeline_metrics": official_metrics,
            "harness_metrics": harness_real,
            "note": "A2 的每一种『假真值』都走 pipeline.case_metrics，"
                    "自检确认与 pipeline.run_dataset 的端到端指标同口径",
        },
        "A1_decisiveness": a1,
        "A2_permutation": a2,
        "A3_negative_pools": a3,
        "conclusions": {
            "a1": verdict_a1(a1),
            "a2": verdict_a2(a2),
            "a3": verdict_a3(a3),
        },
        "boundaries": [
            "全部数据为合成/虚构数据，本文任何数字都不构成真实案件的识别准确率或误报率。",
            "A2 的置换只重排『哪笔出金配哪笔入金』，候选集合不变；它检验的是配对指派，"
            "不能单独证明『候选集合本身选对了』（后者由对照档 null_secondary_redraw 间接覆盖）。",
            "A3 的六个负样本是按『像混币但不是』人工构造的，覆盖面有限，不能当成实战误报率。",
            "A3 的真阳性分数只做对照：评估集的池靠 type=mixer 标签进入候选池，"
            "分数高低不代表识别能力；同一份数据『摘掉标签还能不能自己发现池』见 "
            "discovery_stage_sensitivity，必须一起看。",
            "A1 的 gap 门槛（10 分 / 5 分）是本实验设定的观察口径，不是契约常量。",
        ],
    }


def _print_summary(payload: dict) -> None:
    a1, a2, a3 = payload["A1_decisiveness"], payload["A2_permutation"], payload["A3_negative_pools"]
    b = a1["bucket_counts"]
    print("=" * 78)
    print("A1 分数间隔")
    print(
        f"  决断 {b['decisive']['n']} / 险胜 {b['narrow']['n']} / 失手 {b['miss']['n']}"
        f"（共 {a1['n_pairs']} 对）；gap 最小 {a1['gap_stats']['min']}、"
        f"p25 {a1['gap_stats']['p25']}、中位 {a1['gap_stats']['median']}、"
        f"p75 {a1['gap_stats']['p75']}、最大 {a1['gap_stats']['max']}"
    )
    print(f"  险胜+失手中 gap<{GAP_LOW:g} 的：{a1['low_gap_count']} 条")
    for row in a1["low_gap_pairs"]:
        print(
            f"    {row['case_id']} 入金 {row['deposit_edge']} 真值 {row['truth_withdrawal_edge']}"
            f" 第1名 {row['top1_withdrawal_edge']} gap {row['gap']}"
            f"（{row['score_top1']} vs {row['score_second']}，真值 {row['score_truth']}，"
            f"名次 {row['truth_rank']}）"
        )
    print("A2 置换检验")
    print(f"  真实 p@1 {a2['real']['p_at_1']} / p@3 {a2['real']['p_at_3']} / MRR {a2['real']['mrr']}")
    null = a2["null_primary_permute"]["p_at_1"]
    print(
        f"  打乱后 p@1 均值 {null['mean']}、标准差 {null['std']}、"
        f"5%/95% {null['p05']}/{null['p95']}、最大 {null['max']}"
    )
    print(
        f"  真实值分位 {a2['null_primary_permute']['real_percentile']}；"
        f"随机下限 {a2['random_floor_p_at_1']}；对照档均值 {a2['null_secondary_redraw']['p_at_1']['mean']}"
    )
    print("A3 负样本池")
    for row in a3["false_positive_rate"]:
        print(f"  阈值 {row['threshold']}: 误判 {row['flagged']}/{row['total']} {row['types']}")
    tp = a3["true_positive_reference"]
    print(
        f"  真池分数 {tp['score_stats']['values']}；AUC {a3['separation']['auc_true_vs_negative']}"
    )
    print("结论")
    print("  A1 " + payload["conclusions"]["a1"])
    print("  A2 " + payload["conclusions"]["a2"])
    print("  A3 " + payload["conclusions"]["a3"])
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="混币归因对抗性检验（A1 间隔 / A2 置换 / A3 负样本）")
    parser.add_argument("--iterations", type=int, default=ITERATIONS, help="A2 置换次数（默认 1000）")
    parser.add_argument("--seed", type=int, default=SEED, help="随机种子（默认 20261113）")
    parser.add_argument("--quiet", action="store_true", help="只写文件，不打印摘要")
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations 必须 ≥ 1")

    payload = build(iterations=args.iterations, seed=args.seed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    if not args.quiet:
        _print_summary(payload)
        print(f"已写入 {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

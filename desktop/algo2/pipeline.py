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

from . import attribution, controllability, mixer_detect, offramp, paths
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

MECHANISMS = ("coinjoin", "equal_output", "tornado_like", "unknown")
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
    counter = 0
    cases_out: list[dict] = []

    for case in dataset.cases:
        pools = mixer_detect.detect_case(case, params["mixer"])
        links = attribution.attribute_case(case, pools, params)
        links.sort(key=lambda row: _link_sort_key(case, row))

        truth = case.truth_links()
        for link in links:
            counter += 1
            link["link_id"] = f"LK-{counter:03d}"
            link["ground_truth_hit"] = bool(
                truth and (link["deposit_edge"], link["withdrawal_edge"]) in truth
            )

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
            }
            for link in links
        ]

        offramps = offramp.detect_case(case, pools, ordered_links, params["offramp"], rules)
        plans = paths.build_case_paths(case, pools, ordered_links, offramps)
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
                "links": ordered_links,
                "offramps": offramps,
                "paths": plans,
                "metrics": metrics,
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

        for pool in case.get("mixer_pools") or []:
            pid = pool.get("pool_id", "?")
            if pool.get("mechanism") not in MECHANISMS:
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

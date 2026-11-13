"""法币出口识别：规则库驱动，分数可逐条复算。

口径（契约 §1 / §3.4）：
    S_off(v) = min(cap, Σ 命中规则分值)

规则库落在 algo2/rules/offramp_rules.json，分「节点类型规则」与「边模式规则」两类。
每条规则带 rule_id / label / when / score / explain_template；命中的规则原样写进
输出 hits[]，前端不需要内置任何分数。

边界：出口候选只取「已标注为服务节点」的地址（exchange / otc / payment_gateway /
underground_bank / bridge / defi）。普通钱包即便有兑现特征也不判为出口——否则会把
中转地址误当成兑付主体，这是本项目刻意保守的地方。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from . import controllability
from .graph import Case, Edge, InputError, ZERO, money_text

OFFRAMP_TYPES = {
    "exchange": "exchange",
    "otc": "otc",
    "payment_gateway": "payment_gateway",
    "underground_bank": "underground_bank",
    "bridge": "bridge",
    "defi": "dex",
}

TYPE_LABELS = {
    "exchange": "交易所",
    "otc": "场外承兑商",
    "payment_gateway": "支付网关",
    "underground_bank": "地下钱庄",
    "bridge": "跨链桥",
    "defi": "DeFi 协议",
}

RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}

RULES_PATH = Path(__file__).resolve().parent / "rules" / "offramp_rules.json"

# 追溯混币池来源的最大跳数（出金 → 中转 → 出口，三跳内视为同一条断链下游）
LINEAGE_HOPS = 3
# 计算"来自混币池的金额"时允许的跳数
FROM_MIXER_HOPS = 2
# 归因分与出口的关联：出金地址几跳内到达该出口
LINK_REACH_HOPS = 3


def default_params() -> dict:
    return {"cap": 100}


def load_rules(path: str | Path | None = None) -> dict:
    target = Path(path) if path else RULES_PATH
    try:
        with target.open(encoding="utf-8-sig") as handle:
            rules = json.load(handle)
    except OSError as exc:
        raise InputError(f"无法读取出口规则库：{target}（{exc}）") from exc
    except ValueError as exc:
        raise InputError(f"出口规则库不是合法 JSON：{target}（{exc}）") from exc
    for key in ("node_rules", "edge_rules"):
        if not isinstance(rules.get(key), list):
            raise InputError(f"出口规则库缺少 {key} 列表")
    seen: set[str] = set()
    for group in ("node_rules", "edge_rules"):
        for rule in rules[group]:
            for field in ("rule_id", "label", "when", "score", "explain_template"):
                if field not in rule:
                    raise InputError(f"规则 {rule.get('rule_id', '?')} 缺少字段 {field}")
            if rule["rule_id"] in seen:
                raise InputError(f"规则编号重复：{rule['rule_id']}")
            seen.add(rule["rule_id"])
    return rules


def _render(template: str, ctx: dict) -> str:
    """安全占位符替换：不认识的占位符原样保留，不抛异常。"""
    text = template
    for key, value in ctx.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _ancestry(case: Case, pools: set[str], start: str, max_hops: int) -> tuple[int | None, str | None]:
    """沿入金边回溯，找最近的混币池；返回（跳数, 池 id）。"""
    queue: list[tuple[str, int]] = [(start, 0)]
    seen = {start}
    best: tuple[int, str] | None = None
    while queue:
        node_id, depth = queue.pop(0)
        if depth >= max_hops:
            continue
        for edge in case.in_edges(node_id):
            source = edge.src
            if source in pools:
                hop = depth + 1
                if best is None or hop < best[0]:
                    best = (hop, source)
                continue
            if source not in seen:
                seen.add(source)
                queue.append((source, depth + 1))
    return (best if best else (None, None))


def _reachable(case: Case, start: str, target: str, hops: int) -> bool:
    """从 start 出发（不穿过其它混币池）在 hops 跳内能否到达 target。"""
    if start == target:
        return True
    frontier = [start]
    seen = {start}
    for _ in range(hops):
        next_frontier: list[str] = []
        for node_id in frontier:
            for edge in case.out_edges(node_id):
                if edge.dst == target:
                    return True
                if case.node(edge.dst).type == "mixer":
                    continue
                if edge.dst not in seen:
                    seen.add(edge.dst)
                    next_frontier.append(edge.dst)
        frontier = next_frontier
        if not frontier:
            break
    return False


def _build_context(case: Case, node_id: str, pools: set[str], pool_labels: dict[str, str]) -> dict:
    node = case.node(node_id)
    deposits: list[Edge] = case.in_edges(node_id)
    inflow_total = sum((e.amount for e in deposits), ZERO)
    to_fiat = [e for e in deposits if e.is_to_fiat]
    integral = [e for e in deposits if e.amount == e.amount.to_integral_value()]
    hops, mixer_id = _ancestry(case, pools, node_id, LINEAGE_HOPS)
    from_mixer = sum(
        (
            e.amount
            for e in deposits
            if e.src in pools or _ancestry(case, pools, e.src, FROM_MIXER_HOPS)[0] is not None
        ),
        ZERO,
    )
    return {
        "node": node,
        "node_id": node_id,
        "label": node.label,
        "note": node.note or "（无备注）",
        "type_label": TYPE_LABELS.get(node.type, node.type),
        "risk_label": RISK_LABELS.get(node.risk, node.risk),
        "entity": node.entity or "未对应实体",
        "entity_text": (
            f"对应实体 {node.entity}" if node.entity else "无对应实体，主体需落地核查"
        ),
        "deposits": deposits,
        "deposit_count": len(deposits),
        "inflow_total": money_text(inflow_total),
        "inflow_value": inflow_total,
        "to_fiat_count": len(to_fiat),
        "integral_count": len(integral),
        "distinct_sources": len({e.src for e in deposits}),
        "out_degree": len(case.out_edges(node_id)),
        "mixer_hops": hops if hops is not None else "",
        "mixer_label": pool_labels.get(mixer_id, "（未追溯）") if mixer_id else "（未追溯）",
        "from_mixer": from_mixer,
        "risk": node.risk,
    }


def _node_rule_hit(rule: dict, ctx: dict) -> bool:
    when = rule["when"]
    node = ctx["node"]
    if "type" in when and node.type not in when["type"]:
        return False
    if "risk" in when and node.risk not in when["risk"]:
        return False
    if "has_entity" in when and bool(node.entity) != bool(when["has_entity"]):
        return False
    if "text_any" in when:
        haystack = f"{node.label}{node.note}"
        if not any(token in haystack for token in when["text_any"]):
            return False
    if "min_deposit_count" in when and ctx["deposit_count"] < int(when["min_deposit_count"]):
        return False
    if "min_inflow" in when and ctx["inflow_value"] < Decimal(str(when["min_inflow"])):
        return False
    return True


def _edge_rule_hit(rule: dict, ctx: dict) -> bool:
    when = rule["when"]
    if "pattern" in when:
        count = sum(1 for e in ctx["deposits"] if e.pattern == when["pattern"])
        if count < int(when.get("min_count", 1)):
            return False
    if "min_distinct_sources" in when and ctx["distinct_sources"] < int(when["min_distinct_sources"]):
        return False
    if "min_deposit_count" in when and ctx["deposit_count"] < int(when["min_deposit_count"]):
        return False
    if "min_integral_deposits" in when and ctx["integral_count"] < int(when["min_integral_deposits"]):
        return False
    if "mixer_within_hops" in when:
        hops = ctx["mixer_hops"]
        if hops == "" or int(hops) > int(when["mixer_within_hops"]):
            return False
    if "no_out_edges" in when and bool(when["no_out_edges"]) != (ctx["out_degree"] == 0):
        return False
    return True


def evaluate_node(case: Case, node_id: str, pools: set[str], pool_labels: dict[str, str], rules: dict, cap: float) -> dict:
    """对一个出口候选节点跑规则库。"""
    ctx = _build_context(case, node_id, pools, pool_labels)
    hits: list[dict] = []
    for rule in rules["node_rules"]:
        if _node_rule_hit(rule, ctx):
            hits.append(
                {
                    "rule_id": rule["rule_id"],
                    "label": rule["label"],
                    "score": int(rule["score"]),
                    "explain": _render(rule["explain_template"], ctx),
                    "kind": "node",
                }
            )
    for rule in rules["edge_rules"]:
        if _edge_rule_hit(rule, ctx):
            hits.append(
                {
                    "rule_id": rule["rule_id"],
                    "label": rule["label"],
                    "score": int(rule["score"]),
                    "explain": _render(rule["explain_template"], ctx),
                    "kind": "edge",
                }
            )
    total = sum(hit["score"] for hit in hits)
    s_off = round(min(cap, float(total)), 1)
    return {
        "ctx": ctx,
        "hits": hits,
        "s_off": s_off,
        "node_rule_hits": any(hit["kind"] == "node" for hit in hits),
    }


def _best_link_score(case: Case, node_id: str, links: list[dict]) -> float:
    best = 0.0
    for link in links:
        withdrawal = case.edge(link["withdrawal_edge"])
        if _reachable(case, withdrawal.dst, node_id, LINK_REACH_HOPS):
            best = max(best, float(link["score"]))
    return round(best, 2)


def detect_case(
    case: Case,
    pools: list[dict],
    links: list[dict],
    params: dict | None = None,
    rules: dict | None = None,
) -> list[dict]:
    params = params or default_params()
    rules = rules or load_rules()
    cap = float(params["cap"])
    pool_ids = {pool["pool_id"] for pool in pools}
    pool_labels = {pool["pool_id"]: pool["label"] for pool in pools}

    results: list[dict] = []
    for node in case.nodes.values():
        if node.type not in OFFRAMP_TYPES:
            continue
        evaluated = evaluate_node(case, node.id, pool_ids, pool_labels, rules, cap)
        ctx = evaluated["ctx"]
        best_link = _best_link_score(case, node.id, links)
        control = controllability.evaluate(
            node,
            OFFRAMP_TYPES[node.type],
            evaluated["s_off"],
            {
                "deposit_count": ctx["deposit_count"],
                "inflow_total": ctx["inflow_value"],
                "from_mixer": ctx["from_mixer"],
                "best_link_score": best_link,
                "node_rule_hits": evaluated["node_rule_hits"],
            },
            controllability.default_params(),
        )
        results.append(
            {
                "node_id": node.id,
                "label": node.label,
                "offramp_type": OFFRAMP_TYPES[node.type],
                "s_off": evaluated["s_off"],
                "hits": evaluated["hits"],
                "inflow_total": money_text(ctx["inflow_value"]),
                "from_mixer": money_text(ctx["from_mixer"]),
                "deposit_count": ctx["deposit_count"],
                "controllability": control,
                "best_link_score": best_link,
            }
        )
    results.sort(key=lambda row: (-row["controllability"]["s_ctrl"], row["node_id"]))
    return results

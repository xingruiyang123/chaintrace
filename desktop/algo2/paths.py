"""追缴路径方案：主路径 + 备选路径。

不是"最短路径"算法，而是"能不能落地"的排序：
路径分 = 0.5·断链归因分 + 0.5·出口可控性分 − 跳数惩罚。

三条硬规则（都是为了让路径"只走本案的钱"）：
1. 起点只能是本案涉案源（tags 含 SIM_SOURCE；没有该标签时退化为风险 high 的钱包里入度最小的一个）；
2. 路径上的每条边都必须从起点可达，且**不得穿过混币池**去够别的地址；
3. 合成数据里 ground_truth.noise_edges 标注的外部用户边一律不进路径（真实数据没有该字段时这条自动失效）。

备选路径必须和主路径**策略上不同**（换拦截点），否则不给备选——两条一模一样的 85.2 只会让读者以为算错了。
凑不出第二条就只给一条，fallback_path_id 为 null，不编假路径。
"""

from __future__ import annotations

from .graph import Case, Edge

SOURCE_TAG = "SIM_SOURCE"
WALK_HOPS = 6
REACH_HOPS = 3

TYPE_ACTIONS = {
    "exchange": "锁定交易所账户并发起协查",
    "otc": "落地核查承兑商实际控制人",
    "payment_gateway": "核查收单结算主体",
    "underground_bank": "落地核查并对敲账户止付",
    "bridge": "跨链锁定目标链地址",
    "defi": "定位合约与流动性池",
}

PREREQUISITES = {
    "exchange": ["立案文书与协查函齐备", "交易所 KYC 调取通道可用"],
    "otc": ["承兑商收款账户可核实", "银行侧可配合即时止付"],
    "payment_gateway": ["结算主体工商信息可核实", "跨境协作渠道可用"],
    "underground_bank": ["落地核查线索已核实", "收款账户可即时止付"],
    "bridge": ["跨链浏览器可追溯", "目标链存在可协作节点"],
    "defi": ["合约地址与流动性池可定位", "链上取证工具可用"],
}

TYPE_LABELS = {
    "exchange": "交易所",
    "otc": "承兑商",
    "payment_gateway": "支付网关",
    "underground_bank": "地下钱庄",
    "bridge": "跨链桥",
    "defi": "DeFi 协议",
    "wallet": "地址",
    "mixer": "混币池",
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _action(edge: Edge, case: Case) -> str:
    target = case.node(edge.dst)
    label = TYPE_LABELS.get(target.type, target.type)
    if edge.pattern == "split":
        return f"涉案资金拆分至{label} {target.id}"
    if edge.pattern == "aggregate":
        return f"资金归集至{label} {target.id}"
    if edge.pattern == "mixer_in":
        return f"入金混币池 {target.label}"
    if edge.pattern == "mixer_out":
        return f"混币转出（断链处）→ {target.id}"
    if edge.pattern == "bridge_out":
        return f"跨链转出至{label} {target.id}"
    if edge.pattern == "to_fiat":
        return f"入金{label} {target.label}"
    return f"普通转移至{label} {target.id}"


def source_nodes(case: Case) -> list[str]:
    """本案涉案源。优先用标签，其次退化为高风险钱包中入度最小的一个。"""
    tagged = sorted(node.id for node in case.nodes.values() if SOURCE_TAG in node.tags)
    if tagged:
        return tagged
    wallets = [node for node in case.nodes.values() if node.type == "wallet" and node.risk == "high"]
    if not wallets:
        wallets = [node for node in case.nodes.values() if node.type == "wallet"]
    if not wallets:
        return []
    wallets.sort(key=lambda node: (len(case.in_edges(node.id)), node.id))
    return [wallets[0].id]


def excluded_edges(case: Case) -> set[str]:
    """构造真值里标注为外部用户噪声的边。只在合成数据上存在，真实数据为空集。"""
    if not case.ground_truth:
        return set()
    return {str(item) for item in (case.ground_truth.get("noise_edges") or [])}


def reachable_edges(case: Case, origins: list[str], excluded: set[str]) -> set[str]:
    """从起点出发可达的边集合（不做混币池限制，池后的出金也要算进来）。"""
    seen = set(origins)
    queue = list(origins)
    edges: set[str] = set()
    while queue:
        node_id = queue.pop(0)
        for edge in case.out_edges(node_id):
            if edge.id in excluded:
                continue
            edges.add(edge.id)
            if edge.dst not in seen:
                seen.add(edge.dst)
                queue.append(edge.dst)
    return edges


def _shortest_chain(
    case: Case,
    starts: list[str],
    goal: str,
    max_hops: int,
    allowed: set[str],
    avoid_mixer: bool = True,
) -> list[Edge] | None:
    """在 max_hops 内找 starts → goal 的最短边序列。

    avoid_mixer=True 时不穿过混币池节点——上游段一旦穿过池，就已经不是"涉案资金的来路"了。
    """
    if goal in starts:
        return []
    queue: list[tuple[str, list[Edge]]] = [(start, []) for start in starts]
    seen = set(starts)
    while queue:
        node_id, path = queue.pop(0)
        if len(path) >= max_hops:
            continue
        for edge in case.out_edges(node_id):
            if edge.id not in allowed:
                continue
            if avoid_mixer and case.node(edge.dst).type == "mixer":
                continue
            if edge.dst == goal:
                return path + [edge]
            if edge.dst in seen:
                continue
            seen.add(edge.dst)
            queue.append((edge.dst, path + [edge]))
    return None


def _downstream_offramp(
    case: Case, start: str, offramps: list[dict], max_hops: int, allowed: set[str]
) -> tuple[str, list[Edge]] | None:
    """从出金地址往下找"最值得拦"的出口：可控性分最高者优先。"""
    by_id = {row["node_id"]: row for row in offramps}
    if start in by_id:
        return start, []
    best: tuple[float, str, list[Edge]] | None = None
    queue: list[tuple[str, list[Edge]]] = [(start, [])]
    seen = {start}
    while queue:
        node_id, path = queue.pop(0)
        if len(path) > max_hops:
            continue
        if node_id in by_id:
            row = by_id[node_id]
            key = (float(row["controllability"]["s_ctrl"]), -len(path))
            if best is None or key > (best[0], -len(best[2])):
                best = (float(row["controllability"]["s_ctrl"]), node_id, path)
        for edge in case.out_edges(node_id):
            if edge.id not in allowed:
                continue
            if edge.dst in seen:
                continue
            if case.node(edge.dst).type == "mixer":
                continue
            seen.add(edge.dst)
            queue.append((edge.dst, path + [edge]))
    if best is None:
        return None
    return best[1], best[2]


def _assemble(
    case: Case,
    prefix_edges: list[Edge],
    deposit: Edge,
    withdrawal: Edge,
    tail: list[Edge],
    offramp_id: str,
) -> tuple[list[dict], list[str]]:
    """把边序列拼成 steps，并返回节点顺序。"""
    edges = list(prefix_edges) + [deposit] + [withdrawal] + list(tail)
    nodes: list[str] = []
    nodes.append(prefix_edges[0].src if prefix_edges else deposit.src)
    for edge in edges:
        nodes.append(edge.dst)
    steps: list[dict] = []
    for index, node_id in enumerate(nodes):
        if index < len(edges):
            action = _action(edges[index], case)
        else:
            offramp = case.node(offramp_id)
            action = TYPE_ACTIONS.get(offramp.type, "锁定出口并发起协作")
        steps.append(
            {
                "order": index + 1,
                "node_id": node_id,
                "edge_id": edges[index].id if index < len(edges) else None,
                "action": action,
            }
        )
    return steps, nodes


def _build(
    case: Case,
    link: dict,
    offramp: dict,
    origin: str,
    allowed: set[str],
    excluded: set[str],
    path_id: str,
    name: str,
) -> dict | None:
    deposit = case.edge(link["deposit_edge"])
    withdrawal = case.edge(link["withdrawal_edge"])
    # 规则 3：噪声边直接排除
    if deposit.id in excluded or withdrawal.id in excluded:
        return None
    # 规则 2：入金起点必须能从涉案源走到，且中途不穿过混币池
    prefix = _shortest_chain(case, [origin], deposit.src, WALK_HOPS, allowed, avoid_mixer=True)
    if prefix is None:
        return None
    # 出金之后的落地段同样只能用可达边
    downstream = _downstream_offramp(case, withdrawal.dst, [offramp], REACH_HOPS, allowed)
    if downstream is None:
        return None
    offramp_id, tail = downstream
    steps, nodes = _assemble(case, prefix, deposit, withdrawal, tail, offramp_id)
    if steps[0]["node_id"] != origin:
        return None

    control = offramp["controllability"]
    score = 0.5 * float(link["score"]) + 0.5 * float(control["s_ctrl"])
    score -= min(15.0, 2.0 * max(0, len(steps) - 4))
    score = round(_clamp(score), 1)

    estimated_hours = int(control["freeze_window_hours"]) + 4 * len(steps)
    if control["s_ctrl"] >= 75:
        cost_level = "低"
    elif control["s_ctrl"] >= 55:
        cost_level = "中"
    else:
        cost_level = "高"

    if control["level"] in ("GREEN", "YELLOW"):
        intercept = offramp_id
    else:
        # 出口本身是红灯时，拦截点前移到出金落地的那个地址——继续按出口申报没有意义
        intercept = withdrawal.dst if len(tail) > 0 else nodes[-1]

    offramp_type = offramp["offramp_type"]
    risks = [
        f"资金在协作前被再次转出（出口保留窗口约 {control['freeze_window_hours']} 小时，为估计值）",
        f"归因分 {float(link['score']):.1f} 为候选判定，若配对不成立则本路径不成立",
    ]
    if control["level"] == "RED":
        risks.append("出口可控性为红灯，冻结缺少正式接口，只能落地核查")

    reason = ""
    if intercept != offramp_id:
        reason = f"（{offramp_id} 为 {control['level']} 出口，拦截点前移到 {intercept}）"

    return {
        "path_id": path_id,
        "name": name,
        "steps": steps,
        "score": score,
        "estimated_hours": estimated_hours,
        "cost_level": cost_level,
        "best_intercept_node": intercept,
        "prerequisites": list(PREREQUISITES.get(offramp_type, ["协作渠道可用"])),
        "risks": risks,
        "fallback_path_id": None,
        "differentiator": f"起点 {origin}；拦截点 {intercept}{reason}。",
    }


def build_case_paths(
    case: Case,
    pools: list[dict],
    links: list[dict],
    offramps: list[dict],
) -> list[dict]:
    if not pools or not links or not offramps:
        return []
    origins = source_nodes(case)
    if not origins:
        return []
    excluded = excluded_edges(case)
    allowed = reachable_edges(case, origins, excluded)

    ranked_links = sorted(links, key=lambda row: (-float(row["score"]), row["deposit_edge"], row["withdrawal_edge"]))
    ranked_offramps = sorted(
        offramps, key=lambda row: (-float(row["controllability"]["s_ctrl"]), row["node_id"])
    )

    # 把所有"起点可达 + 不碰噪声边"的组合都算出来，再按路径分排序。
    # 主路径 = 分数最高的一条；备选 = 换拦截点里分数最高的一条。
    candidates: list[dict] = []
    seen_chains: set[tuple] = set()
    for link in ranked_links:
        for offramp in ranked_offramps:
            for origin in origins:
                built = _build(case, link, offramp, origin, allowed, excluded, "", "")
                if built is None:
                    continue
                chain = tuple((step["node_id"], step["edge_id"]) for step in built["steps"])
                if chain in seen_chains:
                    continue
                seen_chains.add(chain)
                candidates.append(built)
    if not candidates:
        return []

    candidates.sort(key=lambda row: (-float(row["score"]), float(row["estimated_hours"]), row["steps"][0]["node_id"]))
    main = dict(candidates[0])
    main["path_id"] = "P-01"
    main["name"] = "主路径"
    main["fallback_path_id"] = None

    alt = None
    for candidate in candidates[1:]:
        if candidate["best_intercept_node"] != main["best_intercept_node"]:
            alt = dict(candidate)
            break

    if alt is None:
        return [main]

    alt["path_id"] = "P-02"
    alt["name"] = "备选路径"
    alt["fallback_path_id"] = None
    end_node = alt["steps"][-1]["node_id"]
    reason = (
        f"（{end_node} 为红灯出口，拦截点前移到 {alt['best_intercept_node']}）"
        if alt["best_intercept_node"] != end_node
        else ""
    )
    alt["differentiator"] = (
        f"与 P-01 换拦截点：{main['best_intercept_node']} → {alt['best_intercept_node']}"
        f"{reason}；起点仍为 {alt['steps'][0]['node_id']}。"
    )
    main["fallback_path_id"] = alt["path_id"]
    return [main, alt]

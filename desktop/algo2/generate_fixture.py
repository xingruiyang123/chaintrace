"""生成评估数据集：8 个虚拟案件，覆盖真实结构 + 刻意制造的竞争条件。

为什么需要它：原始两案只有 27 条边，且每笔入金几乎都能靠金额唯一对上，
在那上面算 p@1 必然是 1.0，说明不了任何问题。评估集把"难"造出来：

1. 等额竞争——同一池里存在与真出金金额完全相同的"外部用户出金"（诱饵），
   仅金额匹配会并列，仅时间最近会被诱饵带偏；
2. 时间竞争——诱饵的出金时间比真出金更靠近入金；
3. 结构不可解——固定面额池（Tornado 式）的多笔同面额提现，任何方法都无法唯一定位，
   这一案用来如实展示上界，而不是拿它刷分；
4. 负例——完全没有混币的普通链路，用来检查不会凭空识别出混币池。

真值（ground_truth）由构造过程直接写出，只用于评估，不进入任何打分函数。

运行：python -m algo2.generate_fixture
输出：algo2/fixtures/mixer_eval_cases.json
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .graph import SCHEMA_VERSION, money_text

BASE = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
FEE = Decimal("0.005")  # 池抽成 0.5%
WITHDRAW_FEE = Decimal("0.001")  # 出口前的转移损耗 0.1%

FIXTURE_PATH = "algo2/fixtures/mixer_eval_cases.json"


def _q8(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00000001"))


def _hours(value: float) -> timedelta:
    return timedelta(hours=value)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Builder:
    """一个案件的图构造器：只管节点、边与真值，不做任何打分。"""

    def __init__(self, case_id: str, title: str, network: str, asset: str):
        self.case_id = case_id
        self.title = title
        self.network = network
        self.asset = asset
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.links: list[dict] = []
        self.offramps: list[str] = []
        self.noise_edges: list[str] = []
        self.moments: list[datetime] = []
        self._node_seq = 0
        self._edge_seq = 0

    def node(self, node_id: str, label: str, node_type: str, risk: str, entity: str | None = None,
             tags: tuple[str, ...] = (), note: str = "", first_seen: datetime | None = None) -> str:
        self.nodes.append(
            {
                "id": node_id,
                "label": label,
                "type": node_type,
                "entity": entity,
                "risk": risk,
                "tags": list(tags),
                "first_seen": _iso(first_seen or BASE),
                "note": note,
            }
        )
        return node_id

    def edge(self, src: str, dst: str, amount: Decimal, moment: datetime, pattern: str,
             fee: Decimal | None = None) -> str:
        self._edge_seq += 1
        edge_id = f"{self.case_id}-tx-{self._edge_seq:03d}"
        self.edges.append(
            {
                "id": edge_id,
                "from": src,
                "to": dst,
                "amount": money_text(_q8(amount)),
                "asset": self.asset,
                "time": _iso(moment),
                "txid": f"SIM{self.case_id[-2:]}{self._edge_seq:06d}",
                "pattern": pattern,
                "fee": money_text(_q8(fee)) if fee is not None else None,
            }
        )
        self.moments.append(moment)
        return edge_id

    def truth_link(self, deposit_edge: str, withdrawal_edge: str) -> None:
        self.links.append({"deposit_edge": deposit_edge, "withdrawal_edge": withdrawal_edge})

    def mark_offramp(self, node_id: str) -> None:
        if node_id not in self.offramps:
            self.offramps.append(node_id)

    def mark_noise(self, edge_id: str) -> None:
        self.noise_edges.append(edge_id)

    def case(self) -> dict:
        window_from = min(self.moments) - _hours(24) if self.moments else BASE
        window_to = max(self.moments) + _hours(24) if self.moments else BASE + _hours(24)
        return {
            "case_id": self.case_id,
            "title": self.title,
            "network": self.network,
            "asset": self.asset,
            "window": {"from": _iso(window_from), "to": _iso(window_to)},
            "nodes": self.nodes,
            "edges": self.edges,
            "ground_truth": {
                "note": "生成时已知的构造真值，仅用于在合成数据上评估归因一致性。",
                "links": self.links,
                "offramp_nodes": self.offramps,
                "noise_edges": self.noise_edges,
            },
        }


OFFRAMP_LABELS = {
    "exchange": ("虚拟交易所", "exchange", "medium", "合规交易所，具执法协作通道"),
    "otc": ("虚拟承兑商", "otc", "high", "未持牌场外承兑"),
    "payment_gateway": ("虚拟支付网关", "payment_gateway", "medium", "跨境收单结算"),
    "underground_bank": ("虚拟地下钱庄", "underground_bank", "high", "对敲结算"),
    "bridge": ("虚拟跨链桥", "bridge", "medium", "跨链资产转移"),
}


def build_mixer_case(
    idx: int,
    title: str,
    network: str,
    asset: str,
    pool_label: str,
    pool_note: str,
    deposits: list[dict],
    decoys: list[dict],
    noise_deposits: list[dict],
    offramps: list[str],
    change: Decimal | None = None,
    bridge: str | None = None,
    mechanism_note: str = "",
    background: int = 6,
) -> dict:
    """按配方造一个混币案件。

    deposits：真入金，amount/in_after/out_after（小时）三段
    decoys：诱饵出金，amount/after（相对某笔真入金的入金时刻）/target_deposit
    noise_deposits：外部用户入金，amount/after
    background：与本案无关、但在同一出口兑现的外部流水，用于制造出口侧的干扰
    """
    case_id = f"SIM-EVAL-{idx:02d}"
    b = Builder(case_id, title, network, asset)
    tag = f"SIM_EVAL_{idx:02d}"
    src = b.node(f"src-{idx}", f"涉案归集地址 {idx}（虚拟）", "wallet", "high", None, (f"{tag}_SOURCE",), "归集后准备转移")
    split = b.node(f"split-{idx}", f"拆分地址 {idx}（虚拟）", "wallet", "high", None, (f"{tag}_SPLIT",), "")
    pool = b.node(f"pool-{idx}", pool_label, "mixer", "high", f"SIM_POOL_{idx}", (f"{tag}_MIXER",), pool_note or mechanism_note)

    offramp_nodes: dict[str, str] = {}
    for kind in offramps:
        label, node_type, risk, note = OFFRAMP_LABELS[kind]
        node_id = f"{kind}-{idx}"
        b.node(node_id, f"{label} {idx}", node_type, risk, f"SIM_{kind.upper()}_{idx}", (f"{tag}_OFFRAMP",), note)
        b.mark_offramp(node_id)
        offramp_nodes[kind] = node_id

    bridge_node = None
    if bridge:
        label, node_type, risk, note = OFFRAMP_LABELS["bridge"]
        bridge_node = b.node(f"bridge-{idx}", f"{label} {idx}", "bridge", risk, f"SIM_BRIDGE_{idx}", (f"{tag}_BRIDGE",), note)

    # 源头 → 拆分地址
    b.edge(src, split, Decimal("500.00000000"), BASE + _hours(1), "split", Decimal("0.00010000"))

    deposit_edges: list[str] = []
    for i, dep in enumerate(deposits, start=1):
        amount = Decimal(dep["amount"])
        moment = BASE + _hours(float(dep["in_after"]))
        b.edge(split, pool, amount, moment, "mixer_in", amount * Decimal("0.00004000"))
        dep_edge = b.edges[-1]["id"]
        deposit_edges.append(dep_edge)

        out_amount = _q8(amount * (Decimal(1) - FEE))
        out_moment = moment + _hours(float(dep["out_after"]))
        postmix = b.node(f"postmix-{idx}-{i}", f"混币后中转 {idx}-{i}（虚拟）", "wallet", "high", None, (f"{tag}_POSTMIX",), "")
        wd_edge = b.edge(pool, postmix, out_amount, out_moment, "mixer_out", out_amount * Decimal("0.00005000"))
        b.truth_link(dep_edge, wd_edge)

        mid = b.node(f"mid-{idx}-{i}", f"落地中转 {idx}-{i}（虚拟）", "wallet", "medium", None, (f"{tag}_POSTMIX",), "")
        target = offramp_nodes[dep.get("offramp", offramps[i % len(offramps)])]
        if bridge_node and dep.get("via_bridge"):
            b.edge(postmix, bridge_node, out_amount, out_moment + _hours(3), "bridge_out", out_amount * Decimal("0.00002000"))
            b.edge(bridge_node, target, _q8(out_amount * (Decimal(1) - WITHDRAW_FEE)), out_moment + _hours(9), "to_fiat", None)
        else:
            b.edge(postmix, mid, out_amount, out_moment + _hours(2), "normal", out_amount * Decimal("0.00003000"))
            b.edge(mid, target, _q8(out_amount * (Decimal(1) - WITHDRAW_FEE)), out_moment + _hours(8), "to_fiat", None)

        # 混币后常伴随一次"零散分散"：小额转入另一地址后停住
        spare = b.node(f"spare-{idx}-{i}", f"零散地址 {idx}-{i}（虚拟）", "wallet", "low", None, (f"{tag}_POSTMIX",), "")
        b.edge(postmix, spare, _q8(out_amount * Decimal("0.004")), out_moment + _hours(1), "split", None)

        # 第一笔真出金还会分流一小部分到另一个出口（现实中的"多口落地"）
        if i == 1 and len(offramps) > 1:
            divert_mid = b.node(f"divert-{idx}", f"分流中转 {idx}（虚拟）", "wallet", "medium", None, (f"{tag}_POSTMIX",), "")
            divert_amount = _q8(out_amount * Decimal("0.02"))
            b.edge(postmix, divert_mid, divert_amount, out_moment + _hours(4), "normal", None)
            b.edge(divert_mid, offramp_nodes[offramps[1]], divert_amount, out_moment + _hours(11), "to_fiat", None)

    # 诱饵出金：金额与真出金完全相同，时间更靠近入金，资金停在死胡同地址
    for j, decoy in enumerate(decoys, start=1):
        ref = deposits[int(decoy["target_deposit"])]
        ref_amount = Decimal(ref["amount"])
        ref_moment = BASE + _hours(float(ref["in_after"]))
        out_amount = _q8(ref_amount * (Decimal(1) - FEE))
        dead = b.node(f"deadend-{idx}-{j}", f"外部用户地址 {idx}-{j}（虚拟）", "wallet", "low", None, (f"{tag}_NOISE",), "仅收不转的地址")
        edge_id = b.edge(pool, dead, out_amount, ref_moment + _hours(float(decoy["after"])), "mixer_out", out_amount * Decimal("0.00005000"))
        b.mark_noise(edge_id)

    # 找零输出：金额小、时间同批
    if change is not None:
        leftover = b.node(f"change-{idx}", f"找零地址 {idx}（虚拟）", "wallet", "medium", None, (f"{tag}_CHANGE",), "")
        last = deposits[-1]
        edge_id = b.edge(pool, leftover, change, BASE + _hours(float(last["in_after"]) + float(last["out_after"]) + 0.2), "mixer_out", None)
        b.mark_noise(edge_id)
        target = offramp_nodes[offramps[0]]
        b.edge(leftover, target, change, BASE + _hours(float(last["in_after"]) + float(last["out_after"]) + 6), "to_fiat", None)

    # 外部用户入金：制造 M1 所需的输入多样性，同时不进入真值
    for k, noise in enumerate(noise_deposits, start=1):
        amount = Decimal(noise["amount"])
        moment = BASE + _hours(float(noise["after"]))
        ext = b.node(f"ext-{idx}-{k}", f"外部用户地址 {idx}-{k}（虚拟）", "wallet", "low", None, (f"{tag}_NOISE",), "")
        edge_id = b.edge(ext, pool, amount, moment, "mixer_in", amount * Decimal("0.00004000"))
        b.mark_noise(edge_id)

    # 出口侧的无关流水：同一交易所还有别的用户在兑现
    for k in range(1, background + 1):
        amount = _q8(Decimal("1.23000000") * Decimal(k) + Decimal("0.47000000"))
        ext = b.node(f"bg-{idx}-{k}", f"无关兑现用户 {idx}-{k}（虚拟）", "wallet", "low", None, (f"{tag}_BACKGROUND",), "")
        kind = offramps[k % len(offramps)]
        b.edge(ext, offramp_nodes[kind], amount, BASE + _hours(20 + 3 * k), "to_fiat", Decimal("0.00010000"))

    return b.case()


def build_negative_case(idx: int) -> dict:
    """负例：普通资金链路，完全没有混币池。"""
    case_id = f"SIM-EVAL-{idx:02d}"
    b = Builder(case_id, "虚拟负例：普通归集后直接进入交易所", "BTC", "BTC")
    tag = f"SIM_EVAL_{idx:02d}"
    src = b.node(f"src-{idx}", f"涉案归集地址 {idx}（虚拟）", "wallet", "high", None, (f"{tag}_SOURCE",), "")
    mid = b.node(f"mid-{idx}", f"普通中转 {idx}（虚拟）", "wallet", "medium", None, (), "")
    ex = b.node(f"exchange-{idx}", f"虚拟交易所 {idx}", "exchange", "medium", f"SIM_EX_{idx}", (f"{tag}_OFFRAMP",), "合规交易所")
    otc = b.node(f"otc-{idx}", f"虚拟承兑商 {idx}", "otc", "high", f"SIM_OTC_{idx}", (f"{tag}_OFFRAMP",), "小额承兑")
    b.mark_offramp(ex)
    b.mark_offramp(otc)
    b.edge(src, mid, Decimal("18.50000000"), BASE + _hours(2), "normal", Decimal("0.00008000"))
    b.edge(mid, ex, Decimal("18.40000000"), BASE + _hours(7), "to_fiat", Decimal("0.00020000"))
    b.edge(mid, otc, Decimal("0.08000000"), BASE + _hours(9), "to_fiat", Decimal("0.00002000"))
    b.edge(ex, otc, Decimal("3.00000000"), BASE + _hours(30), "normal", Decimal("0.00004000"))
    # 同一交易所的其他用户流水：让出口的入金笔数与来源数都不唯一，避免靠"只有一条边"蒙对
    for k in range(1, 5):
        wallet = b.node(f"bg-{idx}-{k}", f"无关用户 {idx}-{k}（虚拟）", "wallet", "low", None, (f"{tag}_BACKGROUND",), "")
        b.edge(wallet, ex, _q8(Decimal("0.87000000") * Decimal(k) + Decimal("0.13000000")), BASE + _hours(12 + 4 * k), "to_fiat", None)
    b.edge(src, ex, Decimal("0.50000000"), BASE + _hours(24), "to_fiat", Decimal("0.00001000"))
    return b.case()


def build_all() -> dict:
    cases = [
        # 1. CoinJoin 池：3 笔真入金 + 2 个等额诱饵 + 2 笔外部入金
        build_mixer_case(
            1,
            "虚拟评估一：CoinJoin 池等额输出，存在等额外部用户出金",
            "BTC",
            "BTC",
            "虚拟 CoinJoin 池 E1",
            "多输入聚合、等额输出、批处理",
            deposits=[
                {"amount": "5.00000000", "in_after": 10, "out_after": 46},
                {"amount": "8.00000000", "in_after": 12, "out_after": 52},
                {"amount": "12.00000000", "in_after": 14, "out_after": 44},
                {"amount": "6.50000000", "in_after": 16, "out_after": 49},
                {"amount": "15.00000000", "in_after": 18, "out_after": 55},
            ],
            decoys=[
                {"target_deposit": 0, "after": 40},
                {"target_deposit": 2, "after": 38},
            ],
            noise_deposits=[
                {"amount": "3.50000000", "after": 11},
                {"amount": "6.20000000", "after": 13},
            ],
            offramps=["exchange", "otc"],
            change=Decimal("0.03000000"),
        ),
        # 2. 固定面额池：结构上不可解，用来展示上界
        build_mixer_case(
            2,
            "虚拟评估二：固定面额池，多笔同面额提现无法唯一对应",
            "TRON",
            "TRON:USDT",
            "虚拟固定面额池 E2",
            "固定面额 100000 存提，延迟提现",
            deposits=[
                {"amount": "100000.00000000", "in_after": 5, "out_after": 70},
                {"amount": "100000.00000000", "in_after": 15, "out_after": 55},
                {"amount": "100000.00000000", "in_after": 25, "out_after": 40},
                {"amount": "100000.00000000", "in_after": 35, "out_after": 62},
                {"amount": "100000.00000000", "in_after": 45, "out_after": 47},
            ],
            decoys=[],
            noise_deposits=[{"amount": "100000.00000000", "after": 20}],
            offramps=["otc", "underground_bank"],
        ),
        # 3. 跨链桥：出金经跨链桥落到交易所
        build_mixer_case(
            3,
            "虚拟评估三：混币出金经跨链桥落到交易所",
            "ETH",
            "ETH",
            "虚拟 CoinJoin 池 E3",
            "等额输出后跨链转出",
            deposits=[
                {"amount": "20.00000000", "in_after": 6, "out_after": 50, "via_bridge": True},
                {"amount": "35.00000000", "in_after": 9, "out_after": 60, "via_bridge": True},
                {"amount": "9.00000000", "in_after": 12, "out_after": 57, "via_bridge": True},
            ],
            decoys=[{"target_deposit": 1, "after": 54}],
            noise_deposits=[{"amount": "12.50000000", "after": 8}],
            offramps=["exchange"],
            bridge="bridge",
            change=Decimal("0.12000000"),
        ),
        # 4. OTC 转地下钱庄：两层落地
        build_mixer_case(
            4,
            "虚拟评估四：混币后经承兑商对敲至地下钱庄",
            "TRON",
            "TRON:USDT",
            "虚拟固定面额池 E4",
            "固定面额存提后经承兑商对敲",
            deposits=[
                {"amount": "50000.00000000", "in_after": 4, "out_after": 36},
                {"amount": "80000.00000000", "in_after": 7, "out_after": 48},
                {"amount": "120000.00000000", "in_after": 11, "out_after": 42},
                {"amount": "60000.00000000", "in_after": 14, "out_after": 45},
            ],
            decoys=[
                {"target_deposit": 0, "after": 30},
                {"target_deposit": 3, "after": 39},
            ],
            noise_deposits=[{"amount": "30000.00000000", "after": 9}],
            offramps=["otc", "underground_bank"],
            change=Decimal("240.00000000"),
        ),
        # 5. 支付网关：跨境收单
        build_mixer_case(
            5,
            "虚拟评估五：混币出金经支付网关跨境结算",
            "ETH",
            "ETH",
            "虚拟 CoinJoin 池 E5",
            "等额输出、批处理、下游集中",
            deposits=[
                {"amount": "9.00000000", "in_after": 3, "out_after": 40},
                {"amount": "15.00000000", "in_after": 6, "out_after": 47},
                {"amount": "22.00000000", "in_after": 9, "out_after": 44},
            ],
            decoys=[{"target_deposit": 0, "after": 34}, {"target_deposit": 1, "after": 41}],
            noise_deposits=[{"amount": "4.40000000", "after": 5}, {"amount": "7.70000000", "after": 8}],
            offramps=["payment_gateway", "exchange"],
        ),
        # 6. 零竞争：每笔入金金额互不相同，也没有诱饵
        build_mixer_case(
            6,
            "虚拟评估六：零竞争条件下的基线案件",
            "BTC",
            "BTC",
            "虚拟 CoinJoin 池 E6",
            "金额互不相同，无等额外流",
            deposits=[
                {"amount": "2.30000000", "in_after": 4, "out_after": 30},
                {"amount": "7.70000000", "in_after": 8, "out_after": 36},
                {"amount": "19.10000000", "in_after": 12, "out_after": 44},
                {"amount": "30.50000000", "in_after": 16, "out_after": 39},
            ],
            decoys=[],
            noise_deposits=[{"amount": "5.00000000", "after": 6}],
            offramps=["exchange", "payment_gateway"],
        ),
        # 7. 大池高竞争：7 笔真入金 + 3 个等额诱饵
        build_mixer_case(
            7,
            "虚拟评估七：多笔入金的等额竞争压力案",
            "BTC",
            "BTC",
            "虚拟 CoinJoin 池 E7",
            "多输入、等额输出、多个外部用户噪声",
            deposits=[
                {"amount": "10.00000000", "in_after": 2, "out_after": 38},
                {"amount": "10.00000000", "in_after": 6, "out_after": 44},
                {"amount": "25.00000000", "in_after": 10, "out_after": 52},
                {"amount": "40.00000000", "in_after": 14, "out_after": 58},
                {"amount": "55.00000000", "in_after": 18, "out_after": 62},
                {"amount": "70.00000000", "in_after": 22, "out_after": 66},
                {"amount": "88.00000000", "in_after": 26, "out_after": 71},
            ],
            decoys=[
                {"target_deposit": 0, "after": 32},
                {"target_deposit": 1, "after": 38},
                {"target_deposit": 3, "after": 50},
            ],
            noise_deposits=[
                {"amount": "6.60000000", "after": 4},
                {"amount": "8.80000000", "after": 8},
                {"amount": "13.30000000", "after": 12},
            ],
            offramps=["exchange", "otc"],
            change=Decimal("0.45000000"),
        ),
        # 8. 负例
        build_negative_case(8),
    ]
    return {
        "name": "混币—法币出口识别 合成评估集",
        "schema_version": SCHEMA_VERSION,
        "is_simulated": True,
        "notice": "全部为程序生成的虚构数据；不含任何真实地址、实体或案件信息。",
        "generator": "algo2/generate_fixture.py",
        "ground_truth_note": "ground_truth 由构造过程直接写出，仅用于评估归因一致性，不参与任何打分。",
        "cases": cases,
    }


def main() -> None:
    from pathlib import Path

    from . import pipeline

    dataset = build_all()
    target = Path(__file__).resolve().parent / "fixtures" / "mixer_eval_cases.json"
    pipeline.write_json(dataset, target)
    edges = sum(len(c["edges"]) for c in dataset["cases"])
    nodes = sum(len(c["nodes"]) for c in dataset["cases"])
    links = sum(len(c["ground_truth"]["links"]) for c in dataset["cases"])
    print(f"已生成 {target}")
    print(f"案件 {len(dataset['cases'])} 个 · 节点 {nodes} 个 · 边 {edges} 条 · 构造真值 {links} 对")
    for case in dataset["cases"]:
        print(f"  {case['case_id']}: 节点 {len(case['nodes'])} · 边 {len(case['edges'])} · 真值 {len(case['ground_truth']['links'])}")


if __name__ == "__main__":
    main()

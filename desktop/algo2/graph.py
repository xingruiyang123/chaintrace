"""链迹核验 · 混币—法币出口识别 · 输入图读取与结构校验。

设计约束（见 docs/接口契约_混币法币识别_v0_1.md 第 2 节）：
- 金额一律是十进制字符串，内部统一转 Decimal，禁止 float 参与任何累加或比较；
- 时间一律 ISO-8601 UTC 带 Z；
- 输入必须显式标记 is_simulated: true，否则拒绝——本模块只处理合成数据。

结构校验失败一律抛 InputError，消息为中文，能直接给办案人员看。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "mixer_offramp/v1"

NODE_TYPES = (
    "wallet",
    "mixer",
    "exchange",
    "otc",
    "payment_gateway",
    "underground_bank",
    "bridge",
    "defi",
)
EDGE_PATTERNS = (
    "normal",
    "split",
    "aggregate",
    "mixer_in",
    "mixer_out",
    "to_fiat",
    "bridge_out",
)
NODE_RISKS = ("low", "medium", "high")

ZERO = Decimal("0")
CENT = Decimal("0.00000001")
# 金额对外格式：8 位小数字符串（与契约一致）
MONEY_RE = re.compile(r"^-?\d+\.\d{8}$")


class InputError(ValueError):
    """输入不满足契约要求。消息面向人，不面向程序。"""


def canonical_bytes(value: Any) -> bytes:
    """与算法一保持同一套规范化规则，便于跨模块比对哈希。"""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    """对文件原始字节做 SHA-256（用于"这份输入没被改过"的追溯）。

    与 sha256(value) 不是一回事：后者是对解析后的对象做规范化哈希，
    同一个对象换个写法（键序、空白）哈希不变；前者对字节敏感，改一个空格就变。
    """
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def money_text(value: Decimal) -> str:
    """金额对外一律输出 8 位小数字符串，不经 float。"""
    return f"{value.quantize(CENT):f}"


# 数据质量状态（规格 DATA_QUALITY_CONTRACT §1）。
# 只用于"值可不可用"，不是资金归因结论。
DQ_PRESENT = "PRESENT"
DQ_MISSING = "MISSING"
DQ_INVALID = "INVALID"
DQ_UNVERIFIED = "UNVERIFIED"
DQ_NOT_APPLICABLE = "NOT_APPLICABLE"
DQ_CONFLICTED = "CONFLICTED"

DQ_STATES = (DQ_PRESENT, DQ_MISSING, DQ_INVALID, DQ_UNVERIFIED,
             DQ_NOT_APPLICABLE, DQ_CONFLICTED)

# 关键字段：缺失时逐笔可识别性无法可靠判断 → WITHHELD
CRITICAL_EDGE_FIELDS = ("amount", "time")


def parse_amount(raw: Any, where: str) -> Decimal:
    """把契约里的十进制字符串转成 Decimal。

    拒绝 int/float：JSON 数字一旦被解析成 float，"0.1+0.2" 就会丢精度，
    这类输入直接用中文错误挡回去，而不是悄悄四舍五入。
    """
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise InputError(f"{where} 的金额必须是十进制字符串（不能是数字类型），实际为 {raw!r}")
    text = raw.strip()
    if not text:
        raise InputError(f"{where} 的金额为空")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise InputError(f"{where} 的金额无法解析为十进制数：{raw!r}") from exc
    if not value.is_finite() or value < ZERO:
        raise InputError(f"{where} 的金额必须是非负有限数：{raw!r}")
    return value


def parse_money_optional(raw: Any, where: str) -> Decimal | None:
    if raw is None:
        return None
    return parse_amount(raw, where)


def parse_time(raw: Any, where: str) -> datetime:
    """解析 ISO-8601 UTC 时间；必须带时区，否则无法跨案比较先后。"""
    if not isinstance(raw, str) or not raw.strip():
        raise InputError(f"{where} 的时间必须是 ISO-8601 字符串，实际为 {raw!r}")
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError(f"{where} 的时间无法解析：{raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise InputError(f"{where} 的时间必须带时区（UTC 用 Z 结尾）：{raw!r}")
    return value.astimezone(timezone.utc)


def parse_time_optional(raw: Any, where: str) -> datetime | None:
    if raw is None:
        return None
    return parse_time(raw, where)


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    type: str
    entity: str | None
    risk: str
    tags: tuple[str, ...]
    first_seen: datetime | None
    note: str

    @property
    def has_entity(self) -> bool:
        return bool(self.entity)


@dataclass(frozen=True)
class Edge:
    """一条交易边。

    V0.2.2：amount / at 允许为 None（"没有提供"），并各自带数据质量状态。
    注意：None 只表示**缺失**，不表示 0；下游不得把 None 折算成任何默认值。
    """
    id: str
    src: str
    dst: str
    amount: Decimal | None
    amount_text: str
    asset: str | None
    at: datetime | None
    pattern: str
    fee: Decimal | None
    txid: str | None
    amount_status: str = DQ_PRESENT
    time_status: str = DQ_PRESENT

    @property
    def is_complete(self) -> bool:
        """关键字段是否齐全；不齐全的边不能参与金额/时间类分析。"""
        return self.amount is not None and self.at is not None

    @property
    def missing_critical_fields(self) -> tuple[str, ...]:
        out = []
        if self.amount is None:
            out.append("amount")
        if self.at is None:
            out.append("time")
        return tuple(out)

    @property
    def is_mixer_in(self) -> bool:
        return self.pattern == "mixer_in"

    @property
    def is_mixer_out(self) -> bool:
        return self.pattern == "mixer_out"

    @property
    def is_to_fiat(self) -> bool:
        return self.pattern == "to_fiat"


@dataclass
class Case:
    case_id: str
    title: str
    network: str
    asset: str | None
    window_from: datetime | None
    window_to: datetime | None
    nodes: dict[str, Node]
    edges: list[Edge]
    ground_truth: dict[str, Any] | None
    _out: dict[str, list[Edge]] = field(default_factory=dict, repr=False)
    _in: dict[str, list[Edge]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._out = {nid: [] for nid in self.nodes}
        self._in = {nid: [] for nid in self.nodes}
        for edge in self.edges:
            self._out[edge.src].append(edge)
            self._in[edge.dst].append(edge)
        for bucket in (self._out, self._in):
            for items in bucket.values():
                # 时间可能缺失：缺失的排在最后，避免 None 与 datetime 比较
                items.sort(key=lambda e: (e.at is None, e.at or datetime.min.replace(tzinfo=timezone.utc), e.id))

    @property
    def degraded_edges(self) -> list[Edge]:
        """关键字段缺失的边（不参与金额/时间类分析，但要如实报告）。"""
        return [e for e in self.edges if not e.is_complete]

    @property
    def has_degraded(self) -> bool:
        return any(not e.is_complete for e in self.edges)

    def complete_view(self) -> "Case":
        """只含完整边的副本，供需要金额/时间的模块使用。

        为什么要有这个视图：mixer_detect / offramp / paths 假设 amount、at 一定存在。
        V0.2.2 不改它们的任何一行，改为在这里把不完整边滤掉再交给它们。
        """
        if not self.has_degraded:
            return self
        kept = [e for e in self.edges if e.is_complete]
        return Case(
            case_id=self.case_id, title=self.title, network=self.network,
            asset=self.asset, window_from=self.window_from, window_to=self.window_to,
            nodes=dict(self.nodes), edges=kept,
            ground_truth=self.ground_truth,
        )

    # —— 便捷访问 ——
    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    def out_edges(self, node_id: str) -> list[Edge]:
        return self._out.get(node_id, [])

    def in_edges(self, node_id: str) -> list[Edge]:
        return self._in.get(node_id, [])

    def edge(self, edge_id: str) -> Edge:
        for edge in self.edges:
            if edge.id == edge_id:
                return edge
        raise InputError(f"案件 {self.case_id} 中不存在边：{edge_id}")

    def truth_links(self) -> set[tuple[str, str]]:
        """构造真值里的（入金边, 出金边）集合；只在评估时使用，不参与打分。"""
        if not self.ground_truth:
            return set()
        pairs = self.ground_truth.get("links") or []
        return {(str(p.get("deposit_edge")), str(p.get("withdrawal_edge"))) for p in pairs}

    def truth_offramps(self) -> set[str]:
        if not self.ground_truth:
            return set()
        return {str(x) for x in (self.ground_truth.get("offramp_nodes") or [])}


@dataclass
class Dataset:
    name: str
    schema_version: str
    is_simulated: bool
    notice: str
    cases: list[Case]
    raw: dict[str, Any]


def _require_str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{where} 必须是非空字符串")
    return value


def _load_nodes(raw_nodes: Any, case_id: str) -> dict[str, Node]:
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise InputError(f"案件 {case_id} 的 nodes 必须是非空列表")
    nodes: dict[str, Node] = {}
    for index, item in enumerate(raw_nodes):
        where = f"案件 {case_id} 的 nodes[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{where} 必须是对象")
        node_id = _require_str(item.get("id"), f"{where}.id")
        if node_id in nodes:
            raise InputError(f"案件 {case_id} 中节点标识重复：{node_id}")
        node_type = item.get("type")
        if node_type not in NODE_TYPES:
            raise InputError(f"节点 {node_id} 的 type 不在枚举内：{node_type!r}")
        risk = item.get("risk")
        if risk not in NODE_RISKS:
            raise InputError(f"节点 {node_id} 的 risk 不在枚举内：{risk!r}")
        tags = item.get("tags") or []
        if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
            raise InputError(f"节点 {node_id} 的 tags 必须是字符串列表")
        entity = item.get("entity")
        if entity is not None and not isinstance(entity, str):
            raise InputError(f"节点 {node_id} 的 entity 必须是字符串或 null")
        nodes[node_id] = Node(
            id=node_id,
            label=str(item.get("label") or node_id),
            type=node_type,
            entity=entity,
            risk=risk,
            tags=tuple(tags),
            first_seen=parse_time_optional(item.get("first_seen"), f"节点 {node_id} 的 first_seen"),
            note=str(item.get("note") or ""),
        )
    return nodes


def _load_edges(raw_edges: Any, case_id: str, nodes: dict[str, Node]) -> list[Edge]:
    if not isinstance(raw_edges, list) or not raw_edges:
        raise InputError(f"案件 {case_id} 的 edges 必须是非空列表")
    edges: list[Edge] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_edges):
        where = f"案件 {case_id} 的 edges[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{where} 必须是对象")
        edge_id = _require_str(item.get("id"), f"{where}.id")
        if edge_id in seen:
            raise InputError(f"案件 {case_id} 中边标识重复：{edge_id}")
        seen.add(edge_id)
        src = _require_str(item.get("from"), f"边 {edge_id} 的 from")
        dst = _require_str(item.get("to"), f"边 {edge_id} 的 to")
        if src not in nodes:
            raise InputError(f"边 {edge_id} 引用了不存在的起点：{src}")
        if dst not in nodes:
            raise InputError(f"边 {edge_id} 引用了不存在的终点：{dst}")
        if src == dst:
            raise InputError(f"边 {edge_id} 的起点与终点相同：{src}")
        pattern = item.get("pattern")
        if pattern not in EDGE_PATTERNS:
            # 「给错了」→ 拒收（契约 §2.2.1：降级只用于「没有提供」）
            raise InputError(f"边 {edge_id} 的 pattern 不在枚举内：{pattern!r}")

        # —— 金额：缺失降级 / 非法拒收 ——
        raw_amount = item.get("amount")
        if raw_amount is None:
            amount, amount_status = None, DQ_MISSING
        else:
            amount, amount_status = parse_amount(raw_amount, f"边 {edge_id}"), DQ_PRESENT

        # —— 时间：缺失降级 / 非法拒收 ——
        raw_time = item.get("time")
        if raw_time is None:
            at, time_status = None, DQ_MISSING
        else:
            at, time_status = parse_time(raw_time, f"边 {edge_id}"), DQ_PRESENT

        edges.append(
            Edge(
                id=edge_id,
                src=src,
                dst=dst,
                amount=amount,
                amount_text="" if amount is None else str(raw_amount),
                asset=item.get("asset") if isinstance(item.get("asset"), str) else None,
                at=at,
                pattern=pattern,
                fee=parse_money_optional(item.get("fee"), f"边 {edge_id} 的 fee"),
                txid=item.get("txid") if isinstance(item.get("txid"), str) else None,
                amount_status=amount_status,
                time_status=time_status,
            )
        )
    return edges


def _load_ground_truth(raw: Any, case_id: str, edges: list[Edge], nodes: dict[str, Node]) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InputError(f"案件 {case_id} 的 ground_truth 必须是对象或 null")
    edge_ids = {e.id for e in edges}
    links = raw.get("links") or []
    if not isinstance(links, list):
        raise InputError(f"案件 {case_id} 的 ground_truth.links 必须是列表")
    for index, pair in enumerate(links):
        if not isinstance(pair, dict):
            raise InputError(f"案件 {case_id} 的 ground_truth.links[{index}] 必须是对象")
        for key in ("deposit_edge", "withdrawal_edge"):
            value = pair.get(key)
            if value not in edge_ids:
                raise InputError(f"案件 {case_id} 的 ground_truth.links[{index}].{key} 引用了不存在的边：{value!r}")
    for key in ("offramp_nodes", "noise_edges"):
        values = raw.get(key) or []
        if not isinstance(values, list):
            raise InputError(f"案件 {case_id} 的 ground_truth.{key} 必须是列表")
        pool = edge_ids if key == "noise_edges" else set(nodes)
        for value in values:
            if value not in pool:
                raise InputError(f"案件 {case_id} 的 ground_truth.{key} 引用了不存在的对象：{value!r}")
    return dict(raw)


def load_dataset(data: Any) -> Dataset:
    """校验并读取一份 mixer_offramp/v1 输入数据集。"""
    if not isinstance(data, dict):
        raise InputError("输入必须是 JSON 对象")
    if data.get("is_simulated") is not True:
        raise InputError("仅接受明确标记为模拟数据的输入：is_simulated 必须为 true")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise InputError(f"输入 schema_version 应为 {SCHEMA_VERSION}，实际为 {version!r}")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise InputError("输入的 cases 必须是非空列表")

    cases: list[Case] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise InputError(f"cases[{index}] 必须是对象")
        case_id = _require_str(item.get("case_id"), f"cases[{index}].case_id")
        if case_id in seen_ids:
            raise InputError(f"案件标识重复：{case_id}")
        seen_ids.add(case_id)
        nodes = _load_nodes(item.get("nodes"), case_id)
        edges = _load_edges(item.get("edges"), case_id, nodes)
        window = item.get("window") or {}
        if not isinstance(window, dict):
            raise InputError(f"案件 {case_id} 的 window 必须是对象")
        cases.append(
            Case(
                case_id=case_id,
                title=str(item.get("title") or case_id),
                network=str(item.get("network") or ""),
                asset=item.get("asset") if isinstance(item.get("asset"), str) else None,
                window_from=parse_time_optional(window.get("from"), f"案件 {case_id} 的 window.from"),
                window_to=parse_time_optional(window.get("to"), f"案件 {case_id} 的 window.to"),
                nodes=nodes,
                edges=edges,
                ground_truth=_load_ground_truth(item.get("ground_truth"), case_id, edges, nodes),
            )
        )
        if cases[-1].window_from and cases[-1].window_to and cases[-1].window_from > cases[-1].window_to:
            raise InputError(f"案件 {case_id} 的 window.from 晚于 window.to")

    return Dataset(
        name=str(data.get("name") or ""),
        schema_version=version,
        is_simulated=True,
        notice=str(data.get("notice") or ""),
        cases=cases,
        raw=data,
    )


def load_file(path: str | Path) -> tuple[Dataset, dict[str, Any]]:
    """从磁盘读取数据集，返回（校验后的数据集, 原始对象）。"""
    target = Path(path)
    try:
        with target.open(encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise InputError(f"无法读取输入文件：{target}（{exc}）") from exc
    except ValueError as exc:
        raise InputError(f"输入文件不是合法 JSON：{target}（{exc}）") from exc
    return load_dataset(raw), raw


def summarize_totals(dataset: Dataset) -> dict[str, int]:
    """全量规模，供报告与基准文件使用。"""
    edges = sum(len(c.edges) for c in dataset.cases)
    return {
        "cases": len(dataset.cases),
        "nodes": sum(len(c.nodes) for c in dataset.cases),
        "edges": edges,
        "truth_links": sum(len(c.truth_links()) for c in dataset.cases),
    }

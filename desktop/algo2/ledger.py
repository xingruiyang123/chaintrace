# -*- coding: utf-8 -*-
"""法币出口五维台账（规格 C7）。

设计约束（全部来自规格，违反即算缺陷）：

1. **前两维可由系统填**：`service_identity`（是否确属交易所/托管服务）与
   `attribution_evidence`（能否关联到具体账户或主体）——这两维有链上或标签依据。
2. **后三维只能人工录入**：`asset_present` / `legal_condition` / `execution_feasibility`。
   系统即使能猜，也**不得**赋值；`apply_external_entry` 会拒绝算法来源的写入。
3. **`null` 不得被填成 0**：未录入就是 `None` + `UNKNOWN`，任何统计与展示都不得把它当 0 处理。
4. **状态五档**：`CONFIRMED / DENIED / UNKNOWN / PENDING / STALE`。
   `UNKNOWN` 不等于 `DENIED`——前者是"没查"，后者是"查了且否定"。
5. **每次更新追加 history**，不覆盖旧值。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

DIMENSIONS = (
    "service_identity",
    "attribution_evidence",
    "asset_present",
    "legal_condition",
    "execution_feasibility",
)

DIMENSION_LABELS = {
    "service_identity": "服务身份",
    "attribution_evidence": "归属证据",
    "asset_present": "资产是否现存",
    "legal_condition": "法律条件",
    "execution_feasibility": "执行可行性",
}

# 允许系统赋值的维度（其余只接受人工录入）
SYSTEM_DIMENSIONS = ("service_identity", "attribution_evidence")
EXTERNAL_DIMENSIONS = tuple(d for d in DIMENSIONS if d not in SYSTEM_DIMENSIONS)

STATUSES = ("CONFIRMED", "DENIED", "UNKNOWN", "PENDING", "STALE")

STALE_AFTER_DAYS = 90          # 候选值，未标定
ENTRY_SOURCES = ("system_rule", "human_entry", "external_document",
                 # V0.2.2：与统一证据来源对齐（附加，不改旧三值语义）
                 "onchain_observed", "platform_record", "third_party",
                 "case_material", "human_verified", "algorithm_inferred")


def default_params() -> dict:
    return {
        "stale_after_days": STALE_AFTER_DAYS,
        "params_status": "UNVERIFIED",
    }


def empty_dimension() -> dict:
    """未录入的维度：value 必须保持 None，不得填 0。"""
    return {
        "value": None,
        "status": "UNKNOWN",
        "source": None,
        "observed_at": None,
        "updated_at": None,
        "verified_by": None,
        "history": [],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def system_dimension(value: float | None, status: str, source: str, detail: str,
                     evidence_ids: list[str] | None = None) -> dict:
    """系统维度：值来自规则计算，核验人是 algorithm，且必须写清依据。"""
    if status not in STATUSES:
        raise ValueError(f"未知状态：{status}")
    if value is not None and not (0.0 <= float(value) <= 100.0):
        raise ValueError(f"维度分值必须在 0–100 之间，实际 {value}")
    record = {
        "value": None if value is None else round(float(value), 1),
        "status": status,
        "source": source,
        "observed_at": _now() if value is not None else None,
        "updated_at": _now(),
        "verified_by": "algorithm" if value is not None else None,
        "history": [],
        "evidence_ids": list(evidence_ids or []),
        "detail": detail,
    }
    return record


def build_ledger(offramp: dict, evidence_ids: list[str] | None = None) -> dict:
    """由一个出口识别的结果构造五维台账。

    前两维按已有证据填；后三维**一律 UNKNOWN，值是 None**，
    并写明"需外部录入"，不得用 0 或默认值糊过去。

    `evidence_ids` 是该出口命中的规则证据编号（规格 C9 要求证据贯穿各模块）。
    """
    score = offramp.get("score")
    entity_type = offramp.get("node_type") or offramp.get("type")
    ids = list(evidence_ids or [])
    # 低分不等于"没有值"：规则给了一个强度分，只是未达确认线 → PENDING（待补充核验）。
    # （原实现给 UNKNOWN 却带 value，与"UNKNOWN 不得携带值"自相矛盾；
    #   v0.2.1 的台账自检因代码缺陷从未执行，故未暴露。）
    service = system_dimension(
        score if score is not None else None,
        "CONFIRMED" if (score or 0) >= 60 else ("PENDING" if score is not None else "UNKNOWN"),
        "system_rule:offramp_rules",
        f"按出口规则命中情况给出服务身份强度；节点类型 {entity_type}",
        evidence_ids=ids,
    )
    attribution = system_dimension(
        None,
        "UNKNOWN",
        "system_rule:attribution_evidence",
        "仅有地址标签，尚无 KYC/调证等能落到具体账户或主体的材料",
    )
    ledger = {
        "offramp_id": offramp.get("offramp_id") or offramp.get("node_id"),
        "dimensions": {
            "service_identity": service,
            "attribution_evidence": attribution,
        },
    }
    for name in EXTERNAL_DIMENSIONS:
        record = empty_dimension()
        record["evidence_ids"] = []
        record["detail"] = "需外部资料与有权限人员录入，系统不得赋值"
        ledger["dimensions"][name] = record
    ledger["completeness"] = completeness(ledger)
    return ledger


def apply_external_entry(
    ledger: dict,
    dimension: str,
    *,
    value: Any = None,
    status: str,
    source_type: str,
    source_reference: str,
    verified_by: str,
    reason: str = "",
) -> dict:
    """人工录入一维。系统维度会被拒绝；每次写入追加 history，不覆盖旧值。"""
    if dimension not in DIMENSIONS:
        raise ValueError(f"未知维度：{dimension}")
    if dimension in SYSTEM_DIMENSIONS:
        raise ValueError(f"{dimension} 由系统按规则给出，不接受人工覆盖")
    if status not in STATUSES:
        raise ValueError(f"未知状态：{status}")
    if source_type not in ENTRY_SOURCES:
        raise ValueError(f"未知来源类型：{source_type}")
    if source_type == "system_rule":
        raise ValueError("后三维不得由系统规则赋值")
    if source_type == "algorithm_inferred":
        # 规格 R2/R4：算法推断永远不能充当外部事实，尤其不能决定法律处置条件
        raise ValueError("后三维不得由算法推断赋值（算法推断不是外部事实）")
    if status in ("CONFIRMED", "DENIED"):
        if value is None:
            raise ValueError("CONFIRMED/DENIED 必须给出 value，不能是空值")
        if not verified_by:
            raise ValueError("CONFIRMED/DENIED 必须写明 verified_by")
    if status == "UNKNOWN" and value is not None:
        raise ValueError("UNKNOWN 状态不应携带 value")

    record = ledger["dimensions"][dimension]
    record["history"].append({
        "value": record.get("value"),
        "status": record.get("status"),
        "updated_at": record.get("updated_at"),
        "verified_by": record.get("verified_by"),
    })
    record["value"] = None if value is None else value
    record["status"] = status
    record["source"] = {"source_type": source_type, "source_reference": source_reference}
    record["observed_at"] = _now()
    record["updated_at"] = _now()
    record["verified_by"] = verified_by
    record["reason"] = reason
    ledger["completeness"] = completeness(ledger)
    return ledger


def completeness(ledger: dict) -> dict:
    """按状态统计，不把 UNKNOWN 当 0 计入。"""
    dims = ledger["dimensions"]
    counts = {status: 0 for status in STATUSES}
    for record in dims.values():
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    return {
        "total": len(dims),
        "by_status": counts,
        "system_filled": sum(1 for d in SYSTEM_DIMENSIONS if dims[d]["value"] is not None),
        "external_filled": sum(1 for d in EXTERNAL_DIMENSIONS if dims[d]["status"] != "UNKNOWN"),
        "note": "UNKNOWN 表示尚未录入，不等于 DENIED，也不得按 0 计入任何综合指标",
    }


def mark_stale(ledger: dict, now: datetime | None = None, days: int | None = None) -> dict:
    """超过时效的外部维度自动置 STALE（只影响人工维度，不碰系统维度）。"""
    limit = STALE_AFTER_DAYS if days is None else int(days)
    now = now or datetime.now(timezone.utc)
    for name in EXTERNAL_DIMENSIONS:
        record = ledger["dimensions"][name]
        if record["status"] in ("CONFIRMED", "DENIED", "PENDING") and record.get("updated_at"):
            try:
                stamp = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - stamp).days > limit:
                record["history"].append({
                    "value": record.get("value"),
                    "status": record.get("status"),
                    "updated_at": record.get("updated_at"),
                    "verified_by": record.get("verified_by"),
                })
                record["status"] = "STALE"
                record["updated_at"] = _now()
    ledger["completeness"] = completeness(ledger)
    return ledger


def disposal_block(ledger: dict) -> dict:
    """资产处置硬阻断（规格 C8 A 类）。

    只有**明确否定**才触发阻断：
      - asset_present = DENIED（已确认没有可处置资产）
      - legal_condition = DENIED（已确认没有合法处置通道）
    UNKNOWN / STALE 一律**不触发**——没查清不等于不能办。
    """
    dims = ledger["dimensions"]
    reasons = []
    if dims["asset_present"]["status"] == "DENIED":
        reasons.append("asset_present 明确为否：已确认不存在可处置资产")
    if dims["legal_condition"]["status"] == "DENIED":
        reasons.append("legal_condition 明确为否：已确认不存在合法处置通道")
    statuses = {dims[name]["status"] for name in EXTERNAL_DIMENSIONS}
    if reasons:
        status = "DENIED"
    elif statuses <= {"CONFIRMED"}:
        status = "CONFIRMED_OK"
    elif statuses & {"UNKNOWN", "STALE", "PENDING"}:
        status = "UNKNOWN"
    else:
        status = "UNKNOWN"
    return {
        "triggered": bool(reasons),
        "status": status,
        "reasons": reasons,
        "unknown_is_not_denied": True,
    }

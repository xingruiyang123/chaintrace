# -*- coding: utf-8 -*-
"""v0.2.1 整改批（第七轮）新增测试：F-02 外部录入通道 / F-04 证据贯穿出口 / F-05 枚举拆分。"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ALGO_DIR = HERE.parent
DESKTOP = ALGO_DIR.parent
sys.path.insert(0, str(DESKTOP))

from algo2 import graph, ledger, pipeline  # noqa: E402

FIXTURE = ALGO_DIR / "fixtures" / "mixer_eval_cases.json"
RAW = json.loads(FIXTURE.read_text(encoding="utf-8"))


def run(raw=None):
    data = copy.deepcopy(raw if raw is not None else RAW)
    return pipeline.run_dataset(graph.load_dataset(data))


# ---------------- F-04：证据贯穿出口与台账 ----------------


def test_offramp_has_evidence_records():
    result = run()
    seen = 0
    for case in result["cases"]:
        for row in case.get("offramps", []):
            ev = row.get("evidence")
            assert isinstance(ev, list) and ev, f"{row.get('node_id')} 没有证据记录"
            for rec in ev:
                for key in ("evidence_id", "source_type", "directness",
                            "verification_status", "reproducible", "source_reference"):
                    assert key in rec
            seen += 1
    assert seen > 0


def test_ledger_evidence_ids_resolve_to_offramp_evidence():
    result = run()
    for case in result["cases"]:
        for row in case.get("offramps", []):
            known = {r["evidence_id"] for r in row.get("evidence", [])}
            for name, rec in row["ledger"]["dimensions"].items():
                for eid in rec.get("evidence_ids") or []:
                    assert eid in known, f"{row['node_id']}.{name} 引用了不存在的证据 {eid}"


def test_ledger_evidence_ids_present_on_every_dimension():
    result = run()
    for case in result["cases"]:
        for row in case.get("offramps", []):
            for name in ledger.DIMENSIONS:
                assert "evidence_ids" in row["ledger"]["dimensions"][name]


# ---------------- F-05：机制枚举拆分 ----------------


def test_legacy_mechanisms_are_separate():
    assert set(pipeline.MECHANISMS) & set(pipeline.LEGACY_MECHANISMS) == set()
    assert set(pipeline.LEGACY_MECHANISMS) == {"equal_output", "tornado_like"}


def test_new_pools_never_produce_legacy_mechanism():
    result = run()
    for case in result["cases"]:
        for pool in case.get("mixer_pools", []):
            assert pool["mechanism"] in pipeline.MECHANISMS


# ---------------- F-02：外部录入通道 ----------------


def _with_entries(entries) -> dict:
    raw = copy.deepcopy(RAW)
    raw["offramp_ledger_entries"] = entries
    return raw


def test_ledger_entry_is_applied_and_history_written():
    entry = {
        "case_id": "SIM-EVAL-01",
        "offramp_id": "exchange-1",
        "dimension": "asset_present",
        "value": 60,
        "status": "CONFIRMED",
        "source_type": "human_entry",
        "source_reference": "冻结回执-001",
        "verified_by": "民警甲",
        "reason": "已冻结",
    }
    result = run(_with_entries([entry]))
    case = next(c for c in result["cases"] if c["case_id"] == "SIM-EVAL-01")
    row = next(o for o in case["offramps"] if o["node_id"] == "exchange-1")
    rec = row["ledger"]["dimensions"]["asset_present"]
    assert rec["status"] == "CONFIRMED"
    assert rec["value"] == 60
    assert rec["verified_by"] == "民警甲"
    assert len(rec["history"]) == 1
    assert rec["history"][0]["status"] == "UNKNOWN"


def test_denied_entry_triggers_asset_block():
    entry = {
        "case_id": "SIM-EVAL-01",
        "offramp_id": "exchange-1",
        "dimension": "legal_condition",
        "value": 0,
        "status": "DENIED",
        "source_type": "external_document",
        "source_reference": "法律意见-001",
        "verified_by": "民警甲",
    }
    result = run(_with_entries([entry]))
    case = next(c for c in result["cases"] if c["case_id"] == "SIM-EVAL-01")
    assert case["veto"]["asset_block"]["triggered"] is True
    assert case["veto"]["case_level_executable"] == "NO"


def test_entry_on_system_dimension_is_rejected():
    entry = {
        "case_id": "SIM-EVAL-01",
        "offramp_id": "exchange-1",
        "dimension": "service_identity",
        "value": 90,
        "status": "CONFIRMED",
        "source_type": "human_entry",
        "source_reference": "x",
        "verified_by": "民警甲",
    }
    with pytest.raises(graph.InputError):
        run(_with_entries([entry]))


def test_entry_for_unknown_offramp_is_rejected():
    entry = {
        "case_id": "SIM-EVAL-01",
        "offramp_id": "not-exist",
        "dimension": "asset_present",
        "value": 50,
        "status": "CONFIRMED",
        "source_type": "human_entry",
        "source_reference": "x",
        "verified_by": "民警甲",
    }
    with pytest.raises(graph.InputError):
        run(_with_entries([entry]))


def test_invalid_entry_is_not_silently_ignored():
    """CONFIRMED 不带 value 必须报错，而不是悄悄按未录入处理。"""
    entry = {
        "case_id": "SIM-EVAL-01",
        "offramp_id": "exchange-1",
        "dimension": "asset_present",
        "value": None,
        "status": "CONFIRMED",
        "source_type": "human_entry",
        "source_reference": "x",
        "verified_by": "民警甲",
    }
    with pytest.raises(graph.InputError):
        run(_with_entries([entry]))


def test_no_entries_keeps_all_unknown():
    result = run()
    for case in result["cases"]:
        for row in case.get("offramps", []):
            for name in ledger.EXTERNAL_DIMENSIONS:
                rec = row["ledger"]["dimensions"][name]
                assert rec["status"] == "UNKNOWN"
                assert rec["value"] is None
                assert rec["history"] == []

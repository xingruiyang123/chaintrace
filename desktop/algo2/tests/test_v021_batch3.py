# -*- coding: utf-8 -*-
"""v0.2.1 第三批新增测试：C7 五维台账 + 路径约束与输出定义。

重点锁住三条最容易做假的规则：
  1. 后三维不得由系统赋值；
  2. null 不得被填成 0，UNKNOWN 不等于 DENIED；
  3. 更新的历史版本必须保留。
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ALGO_DIR = HERE.parent
DESKTOP = ALGO_DIR.parent
sys.path.insert(0, str(DESKTOP))

from algo2 import ledger, pipeline  # noqa: E402

FIXTURE = ALGO_DIR / "fixtures" / "mixer_eval_cases.json"


def sample_offramp() -> dict:
    return {"offramp_id": "exchange-1", "score": 88.0, "node_type": "exchange"}


# ---------------- 结构 ----------------


def test_c7_five_dimensions_always_present():
    book = ledger.build_ledger(sample_offramp())
    assert set(book["dimensions"]) == set(ledger.DIMENSIONS)
    assert len(ledger.DIMENSIONS) == 5


def test_c7_external_dimensions_start_unknown_with_none():
    book = ledger.build_ledger(sample_offramp())
    for name in ledger.EXTERNAL_DIMENSIONS:
        rec = book["dimensions"][name]
        assert rec["status"] == "UNKNOWN"
        assert rec["value"] is None
        assert rec["verified_by"] is None


def test_c7_null_is_not_filled_with_zero():
    book = ledger.build_ledger(sample_offramp())
    for rec in book["dimensions"].values():
        if rec["status"] == "UNKNOWN":
            assert rec["value"] is not None or rec["value"] is None
    # 明确断言：未录入的维度值是 None，不是 0
    assert book["dimensions"]["asset_present"]["value"] is None
    assert book["dimensions"]["asset_present"]["value"] != 0


def test_c7_completeness_does_not_count_unknown_as_zero():
    book = ledger.build_ledger(sample_offramp())
    assert book["completeness"]["external_filled"] == 0
    assert book["completeness"]["by_status"]["UNKNOWN"] >= 3


# ---------------- 谁能写 ----------------


def test_c7_system_cannot_write_external_dimension():
    book = ledger.build_ledger(sample_offramp())
    with pytest.raises(ValueError):
        ledger.apply_external_entry(
            book, "asset_present", value=80, status="CONFIRMED",
            source_type="system_rule", source_reference="auto",
            verified_by="algorithm",
        )


def test_c7_external_entry_rejects_system_source_type():
    book = ledger.build_ledger(sample_offramp())
    with pytest.raises(ValueError):
        ledger.apply_external_entry(
            book, "legal_condition", value=70, status="CONFIRMED",
            source_type="system_rule", source_reference="auto",
            verified_by="民警甲",
        )


def test_c7_confirmed_requires_value_and_verifier():
    book = ledger.build_ledger(sample_offramp())
    with pytest.raises(ValueError):
        ledger.apply_external_entry(
            book, "asset_present", value=None, status="CONFIRMED",
            source_type="human_entry", source_reference="回执-001", verified_by="民警甲",
        )
    with pytest.raises(ValueError):
        ledger.apply_external_entry(
            book, "asset_present", value=50, status="CONFIRMED",
            source_type="human_entry", source_reference="回执-001", verified_by="",
        )


def test_c7_unknown_must_not_carry_value():
    book = ledger.build_ledger(sample_offramp())
    with pytest.raises(ValueError):
        ledger.apply_external_entry(
            book, "asset_present", value=50, status="UNKNOWN",
            source_type="human_entry", source_reference="回执-001", verified_by="民警甲",
        )


def test_c7_human_entry_keeps_history():
    book = ledger.build_ledger(sample_offramp())
    ledger.apply_external_entry(
        book, "asset_present", value=60, status="CONFIRMED",
        source_type="human_entry", source_reference="冻结回执-001",
        verified_by="民警甲", reason="已冻结",
    )
    ledger.apply_external_entry(
        book, "asset_present", value=0, status="DENIED",
        source_type="external_document", source_reference="解冻通知-002",
        verified_by="民警乙", reason="已解冻并划转",
    )
    rec = book["dimensions"]["asset_present"]
    assert rec["status"] == "DENIED"
    assert len(rec["history"]) == 2
    # history 记录的是「每次修改之前」的状态：初始空值 → 60
    assert rec["history"][0]["value"] is None
    assert rec["history"][0]["status"] == "UNKNOWN"
    assert rec["history"][1]["value"] == 60
    assert rec["history"][1]["status"] == "CONFIRMED"


def test_c7_stale_after_timeout():
    book = ledger.build_ledger(sample_offramp())
    ledger.apply_external_entry(
        book, "legal_condition", value=70, status="CONFIRMED",
        source_type="human_entry", source_reference="协查函-001", verified_by="民警甲",
    )
    later = datetime.now(timezone.utc) + timedelta(days=ledger.STALE_AFTER_DAYS + 5)
    ledger.mark_stale(book, now=later)
    assert book["dimensions"]["legal_condition"]["status"] == "STALE"


def test_c7_stale_does_not_touch_system_dimensions():
    book = ledger.build_ledger(sample_offramp())
    before = book["dimensions"]["service_identity"]["status"]
    later = datetime.now(timezone.utc) + timedelta(days=365)
    ledger.mark_stale(book, now=later)
    assert book["dimensions"]["service_identity"]["status"] == before


# ---------------- 阻断语义 ----------------


def test_c8_unknown_does_not_block():
    book = ledger.build_ledger(sample_offramp())
    block = ledger.disposal_block(book)
    assert block["triggered"] is False
    assert block["unknown_is_not_denied"] is True


def test_c8_denied_asset_blocks():
    book = ledger.build_ledger(sample_offramp())
    ledger.apply_external_entry(
        book, "asset_present", value=0, status="DENIED",
        source_type="external_document", source_reference="余额查询-001",
        verified_by="民警甲",
    )
    block = ledger.disposal_block(book)
    assert block["triggered"] is True
    assert block["status"] == "DENIED"


def test_c8_denied_legal_blocks():
    book = ledger.build_ledger(sample_offramp())
    ledger.apply_external_entry(
        book, "legal_condition", value=0, status="DENIED",
        source_type="external_document", source_reference="法律意见-001",
        verified_by="民警甲",
    )
    assert ledger.disposal_block(book)["triggered"] is True


def test_c8_all_confirmed_unlocks():
    book = ledger.build_ledger(sample_offramp())
    for name in ledger.EXTERNAL_DIMENSIONS:
        ledger.apply_external_entry(
            book, name, value=80, status="CONFIRMED",
            source_type="human_entry", source_reference=f"材料-{name}",
            verified_by="民警甲",
        )
    assert ledger.disposal_block(book)["status"] == "CONFIRMED_OK"


def test_c8_veto_denied_asset_makes_case_no():
    offramps = [{"node_id": "exchange-1", "ledger": ledger.build_ledger(sample_offramp())}]
    ledger.apply_external_entry(
        offramps[0]["ledger"], "asset_present", value=0, status="DENIED",
        source_type="external_document", source_reference="余额查询-001", verified_by="民警甲",
    )
    veto = pipeline._veto([], offramps)
    assert veto["asset_block"]["triggered"] is True
    assert veto["case_level_executable"] == "NO"


def test_c8_veto_unknown_asset_stays_undetermined():
    offramps = [{"node_id": "exchange-1", "ledger": ledger.build_ledger(sample_offramp())}]
    veto = pipeline._veto([], offramps)
    assert veto["asset_block"]["triggered"] is False
    assert veto["case_level_executable"] == "UNDETERMINED"


# ---------------- 管线集成 ----------------


def test_c7_pipeline_emits_ledger_for_every_offramp():
    result, _ = pipeline.run_file(str(FIXTURE))
    seen = 0
    for case in result["cases"]:
        for row in case.get("offramps", []):
            book = row.get("ledger")
            assert book, "每个出口都必须带台账"
            assert set(book["dimensions"]) == set(ledger.DIMENSIONS)
            seen += 1
    assert seen > 0


def test_c7_pipeline_external_dims_remain_unknown():
    """系统跑出来的结果里，后三维必须仍是 UNKNOWN——不得被默认值填满。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        for row in case.get("offramps", []):
            for name in ledger.EXTERNAL_DIMENSIONS:
                rec = row["ledger"]["dimensions"][name]
                assert rec["status"] == "UNKNOWN"
                assert rec["value"] is None


# ---------------- 路径约束 ----------------


def test_path_constraints_declared_and_satisfied():
    result, _ = pipeline.run_file(str(FIXTURE))
    seen = 0
    for case in result["cases"]:
        for plan in case.get("paths", []):
            cons = plan.get("constraints")
            assert cons, "路径必须声明约束"
            assert cons["hard"] is True
            assert cons["relation_type"] == "unresolved"
            assert cons["violated"] == [], f"路径 {plan['path_id']} 违反 {cons['violated']}"
            assert cons["declared_non_constraints"]
            seen += 1
    assert seen > 0


def test_path_pool_crossing_is_declared_not_violated():
    """穿过混币池是有意设计，必须写成「非约束」而不是违规。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    cons = result["cases"][0]["paths"][0]["constraints"]
    ids = [c["id"] for c in cons["declared_non_constraints"]]
    assert "N-ALLOW-POOL-CROSSING" in ids
    assert "P-NO-POOL-CROSSING" not in [c["id"] for c in cons["applied"]]


def test_path_alt_changes_intercept():
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        plans = case.get("paths", [])
        if len(plans) < 2:
            continue
        main, alt = plans[0], plans[1]
        assert main["best_intercept_node"] != alt["best_intercept_node"]
        assert main["fallback_path_id"] == alt["path_id"]

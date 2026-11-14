# -*- coding: utf-8 -*-
"""v0.2.1 第二批新增测试：C1 可识别性 / C4 双重门槛 / C6 机制分型与反证 / C8 veto / C9 证据。

这些测试用纯函数 + 合成输入构造边界情形，不依赖具体分数，
因此权重或数据变化不会把它们变成假绿灯。
"""
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

from algo2 import attribution, mixer_detect, pipeline  # noqa: E402

FIXTURE = ALGO_DIR / "fixtures" / "mixer_eval_cases.json"
NEG_FIXTURE = ALGO_DIR / "experiments" / "fixtures_negative_pools.json"


def gate_ok() -> dict:
    return {
        "effective_dims": 6, "coverage": 1.0, "missing_ratio": 0.0,
        "collapsed_pairs": [["amount", "fee_fit"]], "independent_evidence": 5,
        "layer1_pass": True, "layer2_pass": True, "params_status": "UNVERIFIED",
    }


def gate_fail() -> dict:
    g = gate_ok()
    g.update({"effective_dims": 1, "coverage": 0.30, "independent_evidence": 1,
              "layer1_pass": False, "layer2_pass": False})
    return g


def cand(wid: str, rank: int = 1, score: float = 80.0) -> dict:
    return {"withdrawal_edge": wid, "rank": rank, "score": score}


# ============ C1：规格 §1.4 的五个反例 ============


def test_c1_two_candidates_identical():
    """反例 1：两名候选完全不同分但不可辨 → 必须 WITHHELD/CONDITIONAL，不得给唯一结论。"""
    groups = [["w1", "w2"]]
    cands = [cand("w1", 1, 90.0), cand("w2", 1, 90.0)]
    out = attribution.identifiability_of(groups, cands, gate_ok())
    assert out["top_class_size"] == 2
    assert out["decision_status"] == "CONDITIONAL"
    assert out["structurally_identifiable"] is False


def test_c1_many_candidates_top_clearly_ahead():
    """反例 2：候选很多但第一名自成一类 → ALLOWED（候选多不等于不可识别）。"""
    groups = [["w1"], ["w2"], ["w3"], ["w4"], ["w5"]]
    cands = [cand("w1", 1, 95.0)] + [cand(f"w{i}", i, 40.0) for i in range(2, 6)]
    out = attribution.identifiability_of(groups, cands, gate_ok())
    assert out["top_class_size"] == 1
    assert out["decision_status"] == "ALLOWED"


def test_c1_few_candidates_but_identical():
    """反例 3：候选很少但证据完全相同 → 仍不可辨（候选少不等于可识别）。"""
    groups = [["w1", "w2"]]
    cands = [cand("w1", 1, 70.0), cand("w2", 1, 70.0)]
    out = attribution.identifiability_of(groups, cands, gate_ok())
    assert out["decision_status"] == "CONDITIONAL"


def test_c1_quality_gate_failure_forces_withheld():
    """反例 4：数据不足时，即便第一名自成一类也必须 WITHHELD。"""
    groups = [["w1"], ["w2"]]
    cands = [cand("w1", 1, 90.0), cand("w2", 2, 30.0)]
    out = attribution.identifiability_of(groups, cands, gate_fail())
    assert out["decision_status"] == "WITHHELD"


def test_c1_new_evidence_changes_status():
    """反例 5：新增独立证据使两候选可区分后，状态必须由 CONDITIONAL 升为 ALLOWED。"""
    before = attribution.identifiability_of(
        [["w1", "w2"]], [cand("w1", 1, 80.0), cand("w2", 1, 80.0)], gate_ok())
    after = attribution.identifiability_of(
        [["w1"], ["w2"]], [cand("w1", 1, 80.0), cand("w2", 2, 20.0)], gate_ok())
    assert before["decision_status"] == "CONDITIONAL"
    assert after["decision_status"] == "ALLOWED"


def test_c1_partition_does_not_use_truth():
    """划等价类不得依赖真值：只看候选集合自身。"""
    from algo2 import graph
    ds = graph.load_dataset(json.loads(FIXTURE.read_text(encoding="utf-8")))
    params = pipeline.default_parameters()
    case = ds.cases[0]
    pools = mixer_detect.detect_case(case, params["mixer"])
    w = pools[0]["_withdrawals"]
    tol = attribution.Decimal(str(params["mixer"]["equal_output_tolerance"]))
    a = attribution.partition_by_amount(w, tol)
    b = attribution.partition_by_amount(list(reversed(w)), tol)
    assert sorted(map(sorted, a)) == sorted(map(sorted, b))


# ============ C4：双重门槛 ============


def make_features(**overrides) -> dict:
    feats = {k: {"value": 50.0, "weight": w, "detail": "x"}
             for k, w in attribution.default_params()["weights"].items()}
    for k, v in overrides.items():
        feats[k] = {"value": v, "weight": feats[k]["weight"], "detail": "x"}
    return feats


def test_c4_full_data_passes_both_layers():
    out = attribution.quality_gate(make_features(), attribution.default_params()["weights"],
                                   attribution.default_params()["gate"])
    assert out["layer1_pass"] and out["layer2_pass"]
    assert out["independent_evidence"] == 5   # 六维里 amount 与 fee_fit 合并


def test_c4_missing_data_fails_layer_one():
    feats = make_features(amount=None, time=None, denomination=None,
                          path_rarity=None, cooccurrence=None)
    feats["fee_fit"]["weight"] = 8
    out = attribution.quality_gate(feats, attribution.default_params()["weights"],
                                   attribution.default_params()["gate"])
    assert out["layer1_pass"] is False
    assert out["coverage"] < 0.45


def test_c4_three_dims_but_one_evidence_fails_layer_two():
    """三维有效数据不等于三份独立证据：amount + fee_fit + time 只算两份。"""
    feats = make_features(denomination=None, path_rarity=None, cooccurrence=None)
    gate = dict(attribution.default_params()["gate"])
    gate["min_independent"] = 3
    out = attribution.quality_gate(feats, attribution.default_params()["weights"], gate)
    assert out["effective_dims"] == 3
    assert out["independent_evidence"] == 2   # amount/fee_fit 合并
    assert out["layer2_pass"] is False


# ============ C6：机制分型 ============


def signals_with(**hits) -> list[dict]:
    ids = [f"M{i}" for i in range(1, 8)]
    return [{"id": i, "hit": bool(hits.get(i)), "value": 1, "threshold": "x",
             "weight": 10, "label": i, "detail": "x"} for i in ids]


class DummyEdge:
    def __init__(self, i, src="s", amount="1"):
        self.id = i
        self.src = src
        self.amount = attribution.Decimal(amount)


def test_c6_coinjoin_requires_multi_input():
    """单输入 + 等额输出（工资/空投形态）不得判为 coinjoin。"""
    sig = signals_with(M2=True, M3=True, M4=True, M5=True)
    mech, satisfied, _ = mixer_detect.classify_mechanism(
        sig, [DummyEdge("d1")], [DummyEdge(f"w{i}") for i in range(5)])
    assert mech != "coinjoin"
    assert "coinjoin" not in satisfied


def test_c6_coinjoin_when_both_definitional_conditions_hold():
    sig = signals_with(M1=True, M2=True, M3=True)
    mech, _, _ = mixer_detect.classify_mechanism(
        sig, [DummyEdge(f"d{i}", src=f"a{i}") for i in range(4)],
        [DummyEdge(f"w{i}") for i in range(5)])
    assert mech == "coinjoin"


def test_c6_no_auto_fallback_to_coinjoin():
    """无法归类时禁止回退为 coinjoin。"""
    mech, satisfied, _ = mixer_detect.classify_mechanism(
        signals_with(M3=True, M4=True), [DummyEdge("d1")], [DummyEdge("w1")])
    assert mech == "unknown"
    assert satisfied == []


def test_c6_insufficient_evidence_state():
    mech, _, _ = mixer_detect.classify_mechanism(
        signals_with(), [DummyEdge("d1")], [DummyEdge("w1")])
    assert mech == "insufficient_evidence"


def test_c6_ambiguous_state():
    mech, satisfied, _ = mixer_detect.classify_mechanism(
        signals_with(M1=True, M2=True, M7=True),
        [DummyEdge(f"d{i}", src=f"a{i}") for i in range(4)],
        [DummyEdge(f"w{i}") for i in range(5)])
    assert mech == "ambiguous"
    assert len(satisfied) >= 2


def test_c6_generic_counter_evidence_is_hint_only():
    """通用反证只能作提示，不得带扣分权重。"""
    sig = signals_with(M3=True, M4=True)
    hints = mixer_detect.counter_evidence(
        sig, [DummyEdge("d1", amount="5")], [DummyEdge("w1", amount="5")])
    generic = [h for h in hints if h["scope"] == "generic"]
    assert generic, "应至少给出一条通用反证提示"
    for h in generic:
        assert h["effect"] == "hint_only"
        assert "weight" not in h and "penalty" not in h


def test_c6_negative_fixtures_never_labelled_mixer_mechanism():
    """六个正常业务负样本不得被判为任何混币机制。"""
    data = json.loads(NEG_FIXTURE.read_text(encoding="utf-8"))
    from algo2 import graph
    ds = graph.load_dataset(data)
    params = mixer_detect.default_params()
    bad = ("coinjoin", "fixed_denomination", "custodial", "tornado_like", "equal_output")
    for case in ds.cases:
        outdeg = {}
        for e in case.edges:
            outdeg[e.src] = outdeg.get(e.src, 0) + 1
        if not outdeg:
            continue
        node = max(outdeg, key=outdeg.get)
        pool = mixer_detect.analyse_pool(case, node, params)
        assert pool["mechanism"] not in bad, f"{case.case_id} 被误判为 {pool['mechanism']}"


def test_c6_eval_set_pools_report_basis():
    """评估集的每个池都必须给出判定依据，不能只有一个标签。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        for pool in case.get("mixer_pools", []):
            assert pool["mechanism_basis"], f"{pool['pool_id']} 缺少机制判定依据"
            assert isinstance(pool["counter_hints"], list)


# ============ C8：veto ============


def test_c8_evidence_block_never_makes_case_unexecutable():
    """B 类阻断只能导致 UNDETERMINED，绝不能是 NO。"""
    links = [{"link_id": "LK-001", "identifiability": {"decision_status": "WITHHELD"}}]
    veto = pipeline._veto(links)
    assert veto["evidence_block"]["triggered"] is True
    assert veto["evidence_block"]["propagates_to_case"] is False
    assert veto["case_level_executable"] != "NO"


def test_c8_asset_block_not_evaluated_without_ledger():
    """C7 未实施时，资产阻断必须显式 NOT_EVALUATED，不得当作已通过。"""
    veto = pipeline._veto([])
    assert veto["asset_block"]["status"] == "NOT_EVALUATED"
    assert veto["asset_block"]["triggered"] is False
    assert veto["case_level_executable"] == "UNDETERMINED"


def test_c8_unknown_is_not_denied():
    veto = pipeline._veto([])
    assert veto["asset_block"]["status"] != "DENIED"


# ============ C9：证据 ============


def test_c9_every_link_has_traceable_evidence():
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        for link in case.get("links", []):
            ev = link.get("evidence")
            assert ev and len(ev) == len(attribution.FEATURES)
            for rec in ev:
                assert rec["source_type"] == "algorithm_inferred"
                assert rec["directness"] == "inferred"
                assert rec["verification_status"] == "unverified"
                assert rec["reproducible"] is True
                assert rec["source_reference"]


def test_c9_no_numeric_reliability():
    """证据里不得出现无依据的人工可靠性概率。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        for link in case.get("links", []):
            for rec in link.get("evidence", []):
                for key, value in rec.items():
                    assert not isinstance(value, float), f"{link['link_id']}.{key} 是浮点数"


def test_c9_evidence_ids_unique():
    result, _ = pipeline.run_file(str(FIXTURE))
    ids = [rec["evidence_id"] for case in result["cases"]
           for link in case.get("links", []) for rec in link.get("evidence", [])]
    assert len(ids) == len(set(ids))

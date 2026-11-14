# -*- coding: utf-8 -*-
"""v0.2.1 第一批新增测试：C2 分离 / C3 切断环路 / C11 冲突检测 / C5 原始值保留。

这些测试锁住规格里的「不允许发生」的事，而不是锁住具体分数——
分数会随权重与数据变，规则不该跟着变。
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ALGO_DIR = HERE.parent
DESKTOP = ALGO_DIR.parent
sys.path.insert(0, str(DESKTOP))

from algo2 import attribution, graph, mixer_detect, pipeline  # noqa: E402

FIXTURE = ALGO_DIR / "fixtures" / "mixer_eval_cases.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------- C3：切断循环依赖 ----------------


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 中找不到函数 {name}")


def test_path_feature_does_not_reference_offramp_labels():
    """C3：path 维度只能使用 A 域链上结构，不得引用出口类型或 _reaches_offramp。"""
    func = _function_node(ALGO_DIR / "attribution.py", "_path_rarity_feature")
    names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
    forbidden = {"OFFRAMP_TYPES", "_reaches_offramp", "offramps", "reached_node"}
    assert not (names | attrs) & forbidden, f"path 维度引用了 B 域符号：{(names | attrs) & forbidden}"


def test_offramp_reach_is_marked_label_dependent_and_unscored():
    """C3：出口可达性单独记录，且明确标注不参与打分。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    link = result["cases"][0]["links"][0]
    aux = link["auxiliary"]["offramp_reach"]
    assert aux["evidence_domain"] == "B"
    assert aux["label_dependent"] is True
    assert aux["used_in_scoring"] is False


def test_dependency_between_attribution_and_offramp_is_one_way():
    """C3：只允许 offramp → attribution 的单向消费，不允许反向。

    出口识别可以用归因分数（它在流水线里排在归因之后，属正常下游消费）；
    但归因**不得**读取出口类型/出口分数，否则 C3 要切断的环路会重新形成。
    """
    attr_tree = ast.parse((ALGO_DIR / "attribution.py").read_text(encoding="utf-8"))
    attr_names = {n.id for n in ast.walk(attr_tree) if isinstance(n, ast.Name)}
    attr_attrs = {n.attr for n in ast.walk(attr_tree) if isinstance(n, ast.Attribute)}
    attr_strs = {
        n.value for n in ast.walk(attr_tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "OFFRAMP_TYPES" not in (attr_names | attr_attrs | attr_strs)
    assert "offramps" not in attr_names
    # 反向：offramp 依赖 attribution 是允许的，但必须在文档里可见
    offramp_src = (ALGO_DIR / "offramp.py").read_text(encoding="utf-8")
    assert "_best_link_score" in offramp_src, "若删除该依赖，请同步更新规格 C3 的依赖图"


# ---------------- C2：判别分与归因分分离 ----------------


def test_attribution_module_never_reads_detection_score():
    """C2：归因模块的源码里不得出现 detection_score。"""
    src = (ALGO_DIR / "attribution.py").read_text(encoding="utf-8")
    assert "detection_score" not in src


def test_same_structure_different_pool_score_yields_identical_attribution():
    """C2：池判别分不同、结构相同时，归因分必须逐条相同。"""
    raw = load_fixture()
    ds = graph.load_dataset(copy.deepcopy(raw))
    params = pipeline.default_parameters()
    case = ds.cases[0]

    pools = mixer_detect.detect_case(case, params["mixer"])
    assert pools, "夹具首个案件应至少有一个池"
    base = attribution.attribute_case(case, pools, params)

    mutated_pools = copy.deepcopy(pools)
    for pool in mutated_pools:
        pool["detection_score"] = 1.0   # 人为把判别分压到最低
        pool["mechanism"] = "unknown"
    mutated = attribution.attribute_case(case, mutated_pools, params)

    assert [r["score"] for r in base] == [r["score"] for r in mutated]


def test_every_link_declares_pool_hypothesis():
    """C2：每条候选都必须带 conditional_on_pool_hypothesis。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        for link in case.get("links", []):
            hyp = link.get("conditional_on_pool_hypothesis")
            assert hyp is not None, f"{link['link_id']} 缺少池假设声明"
            assert hyp["pool_score_used_in_scoring"] is False


# ---------------- C11：全局冲突检测 ----------------


def test_conflicts_are_detected_and_marked():
    """C11：同一出金被多笔入金同时选为首选，必须被检出并标 contested。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    total = 0
    for case in result["cases"]:
        conflicts = case.get("conflicts") or {}
        for item in conflicts.get("contested_withdrawals", []):
            assert item["claimant_count"] >= 2
            total += 1
        marked = [l for l in case["links"] if l.get("contested")]
        # 每个被标记的候选，其出金必须出现在冲突清单里
        listed = {i["withdrawal_edge"] for i in conflicts.get("contested_withdrawals", [])}
        for link in marked:
            assert link["withdrawal_edge"] in listed
            assert link.get("contested_by"), "contested 候选必须给出其他指认方"
    assert total >= 1, "评估集应至少检出 1 处冲突（审计基线为 8 笔出金）"


def test_conflict_detection_preserves_original_ranking():
    """C11：冲突检测不得改动 score / rank / tier。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        by_deposit: dict[str, list[dict]] = {}
        for link in case["links"]:
            by_deposit.setdefault(link["deposit_edge"], []).append(link)
        for links in by_deposit.values():
            ordered = sorted(links, key=lambda x: (-x["score"], x["withdrawal_edge"]))
            first = ordered[0]
            # 同分同名次：第一名必须是 rank 1，且分数不低于任何其他候选
            assert first["rank"] == 1
            assert all(first["score"] >= l["score"] - 1e-9 for l in links)


def test_mark_conflicts_is_pure_and_idempotent():
    """C11：冲突标记可重复调用，不改变分数。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    links = result["cases"][0]["links"]
    before = [(l["link_id"], l["score"], l["rank"]) for l in links]
    attribution.mark_conflicts(links)
    attribution.mark_conflicts(links)
    after = [(l["link_id"], l["score"], l["rank"]) for l in links]
    assert before == after


# ---------------- C5：保留原始计算值 ----------------


def test_fee_fit_keeps_raw_ratio():
    """C5：fee_fit 必须保留原始比值，便于以后去冗余而不丢信息。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    seen = 0
    for case in result["cases"]:
        for link in case.get("links", []):
            fee = link["features"]["fee_fit"]
            assert "raw" in fee, f"{link['link_id']} 缺少 fee_fit.raw"
            if fee["raw"] is not None:
                seen += 1
    assert seen > 0


def test_score_is_still_weighted_sum():
    """兼容性：score 必须仍等于六维加权和，不因新增字段而变。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    for case in result["cases"]:
        for link in case.get("links", []):
            total = sum(
                link["features"][k]["value"] * link["features"][k]["weight"]
                for k in attribution.FEATURES
            )
            assert abs(round(total / 100.0, 2) - link["score"]) < 1e-9


# ---------------- 兼容性：旧结果文件仍须通过校验 ----------------


def test_legacy_result_still_validates():
    """向后兼容：v0.2.1 之前生成的结���文件必须仍能通过自检。"""
    legacy = DESKTOP / "离线原型" / "样例" / "混币出口识别结果.json"
    if not legacy.exists():
        pytest.skip("旧样例不存在")
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    problems = pipeline.validate_result(payload)
    assert problems == [], f"旧结果文件校验失败：{problems[:3]}"


def test_validate_catches_contested_inconsistency():
    """新增校验必须真的能抓到冲突清单与候选标记的不一致。"""
    result, _ = pipeline.run_file(str(FIXTURE))
    mutated = copy.deepcopy(result)
    target = next(
        (c for c in mutated["cases"] if (c.get("conflicts") or {}).get("contested_count")),
        None,
    )
    assert target is not None, "评估集应至少有一个含冲突的案件"
    # 人为删掉冲突清单里的第一项，但保留候选上的 contested 标记
    target["conflicts"]["contested_withdrawals"] = target["conflicts"]["contested_withdrawals"][1:]
    problems = pipeline.validate_result(mutated)
    assert problems, "校验未发现冲突清单与候选标记不一致"

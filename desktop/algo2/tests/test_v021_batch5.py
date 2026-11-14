# -*- coding: utf-8 -*-
"""v0.2.1 整改批 2 测试：C11 第二步（关系类型，只建立不推断）。"""
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

from algo2 import attribution, graph, pipeline  # noqa: E402

FIXTURE = ALGO_DIR / "fixtures" / "mixer_eval_cases.json"
RAW = json.loads(FIXTURE.read_text(encoding="utf-8"))


def run():
    return pipeline.run_dataset(graph.load_dataset(copy.deepcopy(RAW)))


def test_relation_enum_matches_spec():
    assert set(attribution.RELATION_TYPES) == {
        "one_to_one", "one_to_many", "many_to_one", "many_to_many", "unresolved"
    }
    assert attribution.DEFAULT_RELATION == "unresolved"


def test_all_links_default_to_unresolved():
    """没有人工标注时，一律 unresolved——不得凭"这是一个池"推出一对一。"""
    result = run()
    for case in result["cases"]:
        for link in case.get("links", []):
            assert link["relation_type"] == "unresolved"
            assert link["relation_resolved_by"] is None
            assert "未证明一对一约束" in link["relation_basis"]


def test_no_link_claims_one_to_one_without_evidence():
    result = run()
    claimed = [l for c in result["cases"] for l in c.get("links", [])
               if l["relation_type"] != "unresolved"]
    assert claimed == [], "未引入任何推断，不应出现非 unresolved 的关系"


def test_relation_helper_rejects_unknown_value():
    out = attribution.relation_of({"relation_type": "猜的", "relation_resolved_by": "民警甲"})
    assert out["relation_type"] == "unresolved"


def test_relation_helper_accepts_human_annotation():
    out = attribution.relation_of({
        "relation_type": "one_to_one",
        "relation_resolved_by": "民警甲-2026-11-14",
    })
    assert out["relation_type"] == "one_to_one"
    assert out["relation_basis"] == "人工标注"


def test_validate_rejects_unexplained_relation():
    """校验必须能抓到"给了具体关系却没人作判断"的情况。"""
    result = run()
    mutated = copy.deepcopy(result)
    mutated["cases"][0]["links"][0]["relation_type"] = "one_to_one"
    mutated["cases"][0]["links"][0]["relation_resolved_by"] = None
    problems = pipeline.validate_result(mutated)
    assert problems, "校验未报出未解释的关系类型"
    assert any("one_to_one" in p and "判断的来源" in p for p in problems), problems


def test_validate_rejects_unknown_relation_value():
    result = run()
    mutated = copy.deepcopy(result)
    mutated["cases"][0]["links"][0]["relation_type"] = "猜的"
    problems = pipeline.validate_result(mutated)
    assert any("relation_type 不在枚举内" in p for p in problems), problems


def test_relation_does_not_change_ranking_or_score():
    result = run()
    before = [(l["link_id"], l["score"], l["rank"]) for c in result["cases"] for l in c["links"]]
    # 关系标记是幂等的，重复打不会改动分数与名次
    for case in result["cases"]:
        attribution.mark_relations(case["links"])
        attribution.mark_relations(case["links"])
    after = [(l["link_id"], l["score"], l["rank"]) for c in result["cases"] for l in c["links"]]
    assert before == after

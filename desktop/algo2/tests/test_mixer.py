"""algo2 的单元测试。

覆盖：结构校验、判别分可复算、归因权重与分数区间、分档边界、信号灯边界、
金额精度、构造真值不参与打分、输出结构自检、基准文件措辞。
"""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[2]
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

from algo2 import attribution, benchmark, controllability, graph, mixer_detect, pipeline  # noqa: E402

SAMPLE = DESKTOP / "离线原型" / "样例" / "混币出口虚拟样例.json"
FIXTURE = DESKTOP / "algo2" / "fixtures" / "mixer_eval_cases.json"


# —————————————————— 测试用最小数据集 ——————————————————


def tiny_dataset(amounts: list[str], outputs: list[str]) -> dict:
    """构造一个最小合法数据集：一个混币池、若干入金与出金。"""
    nodes = [
        {"id": "src", "label": "源", "type": "wallet", "entity": None, "risk": "high", "tags": [], "first_seen": "2026-09-01T00:00:00Z", "note": ""},
        {"id": "pool", "label": "池", "type": "mixer", "entity": "SIM_POOL", "risk": "high", "tags": [], "first_seen": "2026-09-01T00:00:00Z", "note": ""},
        {"id": "ex", "label": "交易所", "type": "exchange", "entity": "SIM_EX", "risk": "medium", "tags": [], "first_seen": "2026-09-01T00:00:00Z", "note": ""},
    ]
    edges = []
    for index, amount in enumerate(amounts):
        nodes.append(
            {"id": f"in{index}", "label": f"入金{index}", "type": "wallet", "entity": None, "risk": "high",
             "tags": [], "first_seen": "2026-09-01T00:00:00Z", "note": ""}
        )
        edges.append(
            {"id": f"d{index}", "from": f"in{index}", "to": "pool", "amount": amount, "asset": "BTC",
             "time": f"2026-09-01T0{index}:00:00Z", "txid": f"T{index}", "pattern": "mixer_in", "fee": "0.00010000"}
        )
    for index, amount in enumerate(outputs):
        nodes.append(
            {"id": f"out{index}", "label": f"出金{index}", "type": "wallet", "entity": None, "risk": "medium",
             "tags": [], "first_seen": "2026-09-01T00:00:00Z", "note": ""}
        )
        edges.append(
            {"id": f"w{index}", "from": "pool", "to": f"out{index}", "amount": amount, "asset": "BTC",
             "time": f"2026-09-02T0{index}:00:00Z", "txid": f"W{index}", "pattern": "mixer_out", "fee": "0.00010000"}
        )
        edges.append(
            {"id": f"f{index}", "from": f"out{index}", "to": "ex", "amount": amount, "asset": "BTC",
             "time": f"2026-09-02T1{index}:00:00Z", "txid": f"F{index}", "pattern": "to_fiat", "fee": None}
        )
    return {
        "name": "测试集",
        "schema_version": "mixer_offramp/v1",
        "is_simulated": True,
        "notice": "测试",
        "cases": [
            {
                "case_id": "T-01",
                "title": "测试案",
                "network": "BTC",
                "asset": "BTC",
                "window": {"from": "2026-09-01T00:00:00Z", "to": "2026-09-05T00:00:00Z"},
                "nodes": nodes,
                "edges": edges,
            }
        ],
    }


# —————————————————— 1. 结构校验 ——————————————————


def test_rejects_not_simulated():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["is_simulated"] = False
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "模拟数据" in str(exc.value)


def test_rejects_unknown_edge_endpoint():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["cases"][0]["edges"][0]["to"] = "not-exist"
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "不存在的终点" in str(exc.value)


def test_rejects_numeric_amount():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["cases"][0]["edges"][0]["amount"] = 1.0
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "十进制字符串" in str(exc.value)


def test_rejects_time_without_timezone():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["cases"][0]["edges"][0]["time"] = "2026-09-01T00:00:00"
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "时区" in str(exc.value)


def test_rejects_duplicate_node_id():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["cases"][0]["nodes"].append(copy.deepcopy(data["cases"][0]["nodes"][0]))
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "节点标识重复" in str(exc.value)


def test_rejects_unknown_pattern():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["cases"][0]["edges"][0]["pattern"] = "teleport"
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "pattern" in str(exc.value)


def test_rejects_broken_ground_truth_reference():
    data = tiny_dataset(["1.0"], ["0.99"])
    data["cases"][0]["ground_truth"] = {
        "links": [{"deposit_edge": "d0", "withdrawal_edge": "not-exist"}],
        "offramp_nodes": [],
        "noise_edges": [],
    }
    with pytest.raises(graph.InputError) as exc:
        graph.load_dataset(data)
    assert "不存在的边" in str(exc.value)


# —————————————————— 2. 判别分可复算 ——————————————————


def test_detection_score_is_recomputable():
    dataset = graph.load_dataset(tiny_dataset(["1.0", "2.0", "3.0"], ["0.99", "1.98", "2.97"]))
    pools = mixer_detect.detect_case(dataset.cases[0])
    assert pools
    for pool in pools:
        expected = min(100, sum(s["weight"] for s in pool["signals"] if s["hit"]))
        assert pool["detection_score"] == expected
        # 命中与未命中都必须带 detail，供界面解释"为何不是它"
        for signal in pool["signals"]:
            assert signal["detail"]
            assert isinstance(signal["hit"], bool)


def test_pool_signals_cover_seven_checks():
    dataset = graph.load_dataset(tiny_dataset(["1.0", "2.0", "3.0"], ["0.99", "1.98", "2.97"]))
    pool = mixer_detect.detect_case(dataset.cases[0])[0]
    assert {s["id"] for s in pool["signals"]} == {"M1", "M2", "M3", "M4", "M5", "M6", "M7"}


# —————————————————— 3. 归因权重与分数 ——————————————————


def test_weights_sum_to_100_and_scores_in_range():
    result, _ = pipeline.run_file(SAMPLE)
    params = result["parameters"]
    weights = params["attribution"]["weights"]
    assert sum(weights.values()) == 100
    assert set(weights) == set(attribution.FEATURES)
    for case in result["cases"]:
        for link in case["links"]:
            assert 0.0 <= link["score"] <= 100.0
            assert set(link["features"]) == set(attribution.FEATURES)
            recomputed = round(
                sum(link["features"][k]["value"] * link["features"][k]["weight"] for k in attribution.FEATURES) / 100,
                2,
            )
            assert abs(recomputed - link["score"]) < 0.011


def test_rank_uses_competition_ranking():
    """同分同名次（标准竞赛排名）：名次 = 1 + 分数严格高于本行的人数。"""
    result, _ = pipeline.run_file(SAMPLE)
    for case in result["cases"]:
        grouped: dict[str, list[dict]] = {}
        for link in case["links"]:
            grouped.setdefault(link["deposit_edge"], []).append(link)
        for deposit_id, rows in grouped.items():
            assert min(row["rank"] for row in rows) == 1, deposit_id
            for row in rows:
                expected = 1 + len([o for o in rows if round(o["score"], 2) > round(row["score"], 2)])
                assert row["rank"] == expected, (deposit_id, row["link_id"], row["rank"], expected)
            # 同分的行必须同名次
            by_score: dict[float, set[int]] = {}
            for row in rows:
                by_score.setdefault(round(row["score"], 2), set()).add(row["rank"])
            assert all(len(v) == 1 for v in by_score.values())


# —————————————————— 4. 分档边界 ——————————————————


@pytest.mark.parametrize(
    "score,expected",
    [(69.9, "MEDIUM"), (70.0, "HIGH"), (44.9, "LOW"), (45.0, "MEDIUM"), (100.0, "HIGH"), (0.0, "LOW")],
)
def test_tier_boundaries(score, expected):
    assert attribution.tier_of(score, {"high": 70, "medium": 45}) == expected


# —————————————————— 5. 信号灯边界 ——————————————————


@pytest.mark.parametrize(
    "score,expected",
    [(59.9, "RED"), (60.0, "YELLOW"), (74.9, "YELLOW"), (75.0, "GREEN"), (100.0, "GREEN"), (0.0, "RED")],
)
def test_signal_light_boundaries(score, expected):
    assert controllability.level_of(score, {"green": 75, "yellow": 60}) == expected


# —————————————————— 6. 金额精度 ——————————————————


def test_amounts_use_decimal_strings_without_precision_loss():
    raw = tiny_dataset(["0.1", "0.2"], ["0.29970000"])
    dataset = graph.load_dataset(raw)
    case = dataset.cases[0]
    total = sum((edge.amount for edge in case.out_edges("pool")), Decimal(0))
    assert total == Decimal("0.2997")
    assert graph.money_text(Decimal("0.1") + Decimal("0.2")) == "0.30000000"
    # 反向确认：float 累加确实会丢精度，所以这条路径不能走
    assert 0.1 + 0.2 != 0.3


def test_rejects_float_that_would_lose_precision():
    raw = tiny_dataset(["0.1"], ["0.0995"])
    raw["cases"][0]["edges"][0]["amount"] = 0.30000000000000004
    with pytest.raises(graph.InputError):
        graph.load_dataset(raw)


def test_money_fields_are_eight_decimal_strings():
    result, _ = pipeline.run_file(SAMPLE)
    for case in result["cases"]:
        for pool in case["mixer_pools"]:
            assert graph.MONEY_RE.match(pool["inflow"])
            assert graph.MONEY_RE.match(pool["outflow"])
        for off in case["offramps"]:
            assert graph.MONEY_RE.match(off["inflow_total"])
            assert graph.MONEY_RE.match(off["from_mixer"])


# —————————————————— 7. 构造真值不参与打分 ——————————————————


def test_ground_truth_does_not_affect_scores():
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    with_truth = pipeline.run(raw)

    stripped = copy.deepcopy(raw)
    for case in stripped["cases"]:
        case.pop("ground_truth", None)
    without_truth = pipeline.run(stripped)

    assert len(with_truth["cases"]) == len(without_truth["cases"])
    for left, right in zip(with_truth["cases"], without_truth["cases"]):
        assert [l["score"] for l in left["links"]] == [l["score"] for l in right["links"]]
        assert [p["detection_score"] for p in left["mixer_pools"]] == [
            p["detection_score"] for p in right["mixer_pools"]
        ]
        assert [o["s_off"] for o in left["offramps"]] == [o["s_off"] for o in right["offramps"]]
        # 真值只影响 ground_truth_hit 与 metrics 的取舍
        assert [l["deposit_edge"] for l in left["links"]] == [l["deposit_edge"] for l in right["links"]]
        assert right["metrics"]["attribution"] is None


# —————————————————— 8. 输出结构自检 ——————————————————


def test_sample_result_passes_validation():
    result, raw = pipeline.run_file(SAMPLE)
    dataset = graph.load_dataset(raw)
    assert pipeline.validate_result(result, dataset) == []


def test_validation_catches_tampered_score():
    result, raw = pipeline.run_file(SAMPLE)
    dataset = graph.load_dataset(raw)
    broken = copy.deepcopy(result)
    broken["cases"][0]["links"][0]["score"] = 12.34
    problems = pipeline.validate_result(broken, dataset)
    assert any("加权不一致" in p for p in problems)


def test_validation_catches_missing_mixer_fields():
    result, _ = pipeline.run_file(SAMPLE)
    broken = copy.deepcopy(result)
    broken["cases"][0]["mixer_pools"][0]["signals"] = broken["cases"][0]["mixer_pools"][0]["signals"][:2]
    problems = pipeline.validate_result(broken)
    assert any("信号少于" in p for p in problems)


# —————————————————— 8b. 两个哈希字段 ——————————————————

def test_source_file_sha256_is_bytes_hash_of_input_file():
    import hashlib

    result, raw = pipeline.run_file(SAMPLE)
    expected = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    assert result["source_file_sha256"] == expected
    # 对象哈希与字节哈希是两个东西，不能混用
    assert result["input_sha256"] == graph.sha256(raw)
    assert result["input_sha256"] != result["source_file_sha256"]


def test_source_file_sha256_is_null_for_object_input():
    raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
    result = pipeline.run(raw)
    assert result["source_file_sha256"] is None
    assert pipeline.validate_result(result) == []


# —————————————————— 8c. 追缴路径只走本案的钱 ——————————————————


def test_paths_never_use_noise_edges_or_foreign_addresses():
    """路径不许出现 ground_truth.noise_edges，也不许从外部用户地址起步。"""
    result, raw = pipeline.run_file(SAMPLE)
    dataset = graph.load_dataset(raw)
    for case, ds_case in zip(result["cases"], dataset.cases):
        noise = {str(x) for x in ((ds_case.ground_truth or {}).get("noise_edges") or [])}
        sources = {n.id for n in ds_case.nodes.values() if "SIM_SOURCE" in n.tags}
        for plan in case["paths"]:
            used = {step["edge_id"] for step in plan["steps"] if step["edge_id"]}
            assert not (used & noise), (case["case_id"], plan["path_id"], used & noise)
            assert plan["steps"][0]["node_id"] in sources
            # 外部用户地址（tag SIM_NOISE）不得出现在路径节点里
            for step in plan["steps"]:
                assert "SIM_NOISE" not in ds_case.nodes[step["node_id"]].tags, (case["case_id"], step["node_id"])


def test_fallback_path_has_different_intercept():
    result, _ = pipeline.run_file(SAMPLE)
    for case in result["cases"]:
        paths = case["paths"]
        if len(paths) == 2:
            assert paths[0]["best_intercept_node"] != paths[1]["best_intercept_node"]
            assert paths[0]["score"] != paths[1]["score"]
            assert paths[0]["fallback_path_id"] == paths[1]["path_id"]
            assert paths[1]["fallback_path_id"] is None
            assert paths[1]["differentiator"]
        elif len(paths) == 1:
            assert paths[0]["fallback_path_id"] is None
        else:
            assert paths == []


def test_path_validation_rejects_noise_edge():
    result, raw = pipeline.run_file(SAMPLE)
    dataset = graph.load_dataset(raw)
    broken = copy.deepcopy(result)
    target = next(c for c in broken["cases"] if c["paths"])
    ds_case = next(c for c in dataset.cases if c.case_id == target["case_id"])
    noise = list((ds_case.ground_truth or {}).get("noise_edges") or [])
    if not noise:
        pytest.skip("该样例没有噪声边")
    target["paths"][0]["steps"].insert(1, {"order": 2, "node_id": "x", "edge_id": noise[0], "action": "伪造"})
    for index, step in enumerate(target["paths"][0]["steps"], start=1):
        step["order"] = index
    problems = pipeline.validate_result(broken, dataset)
    assert any("噪声边" in p for p in problems)


# —————————————————— 9. 基准文件 ——————————————————


def test_benchmark_wording_and_shape():
    if not FIXTURE.exists():
        pytest.skip("评估集尚未生成，先运行 python -m algo2.generate_fixture")
    payload = benchmark.run()
    assert payload["is_simulated"] is True
    assert payload["overall"]["data"] == "合成数据"
    for word in benchmark.BANNED:
        assert word not in json.dumps(payload, ensure_ascii=False)
    assert payload["dataset"]["edges"] >= 250
    assert payload["overall"]["method"]["p_at_1"] is not None
    names = [row["name"] for row in payload["overall"]["baselines"]]
    assert names == ["仅金额匹配", "仅时间最近"]


def test_fixture_covers_competition_and_negative():
    if not FIXTURE.exists():
        pytest.skip("评估集尚未生成，先运行 python -m algo2.generate_fixture")
    dataset, raw = graph.load_file(FIXTURE)
    truth = sum(len(case.truth_links()) for case in dataset.cases)
    assert len(dataset.cases) >= 6
    assert truth >= 20
    # 至少一个案件没有真值（负例），确认不会凭空识别出混币池
    assert any(not case.ground_truth.get("links") for case in dataset.cases)

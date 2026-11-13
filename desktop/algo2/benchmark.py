"""在合成评估集上度量归因一致性，并与两个单特征基线对比。

指标口径（写死在这里，防止各处口径漂移）：
    p@1  —— 给定一笔入金，正确出金是否排在候选第 1；并列时按 1/并列数折算
    p@3  —— 正确出金是否落在前 3
    MRR  —— 首个正确候选的排名倒数

基线只有两条，都是真实办案中最常见的"凭单一特征对号"：
    仅金额匹配  —— 按 |入金 − 出金| 排序
    仅时间最近  —— 按 |出金时间 − 入金时间| 排序

表述铁律：本文件的任何指标都只在合成数据、且注明竞争条件的前提下成立；
只能写成「归因一致性 p@k（含竞争条件 · 合成数据）」，不得表述成实战效果或案件结论。
benchmark.json 落盘前会跑一遍自检，命中敏感词就直接失败。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from . import graph, pipeline

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mixer_eval_cases.json"
OUTPUT = Path(__file__).resolve().parent / "outputs" / "benchmark.json"

# 敏感表述：只用于落盘前自检，不写进产物。
# 这里刻意拆成两段拼，避免全仓 grep 把"检查用的字符串"当成"用了这个词"。
BANNED = ("识别" + "准确率", "实战" + "命中率", "真实案件" + "准确率")


def _merge(rows: list[dict]) -> dict:
    """按样本数加权合并多组指标。"""
    rows = [r for r in rows if r.get("n")]
    if not rows:
        return {"p_at_1": None, "p_at_3": None, "mrr": None, "n": 0}
    total = sum(int(r["n"]) for r in rows)
    return {
        "p_at_1": round(sum(float(r["p_at_1"]) * int(r["n"]) for r in rows) / total, 4),
        "p_at_3": round(sum(float(r["p_at_3"]) * int(r["n"]) for r in rows) / total, 4),
        "mrr": round(sum(float(r["mrr"]) * int(r["n"]) for r in rows) / total, 4),
        "n": total,
    }


def _baseline_map(case_metrics: dict) -> dict:
    return {row["name"]: row for row in case_metrics.get("baselines", [])}


def run(path: str | Path | None = None) -> dict:
    target = Path(path) if path else FIXTURE
    digest = graph.file_sha256(target)
    dataset, raw = graph.load_file(target)
    result = pipeline.run_dataset(dataset, source_file_sha256=digest)

    per_case: list[dict] = []
    for case in result["cases"]:
        metrics = case["metrics"]
        attr = metrics.get("attribution")
        if not attr:
            per_case.append(
                {
                    "case_id": case["case_id"],
                    "competition": "零竞争条件",
                    "note": "该案无构造真值（负例），不计入归因指标。",
                    "n": 0,
                }
            )
            continue
        baselines = _baseline_map(metrics)
        per_case.append(
            {
                "case_id": case["case_id"],
                "competition": attr["competition"],
                "n": attr["n"],
                "method": {"p_at_1": attr["p_at_1"], "p_at_3": attr["p_at_3"], "mrr": attr["mrr"]},
                "baselines": {
                    name: {"p_at_1": row["p_at_1"], "p_at_3": row["p_at_3"], "mrr": row["mrr"]}
                    for name, row in baselines.items()
                },
                "pools": case["summary"]["mixer_pools"],
                "links": case["summary"]["links"],
            }
        )

    scored = [row for row in per_case if row.get("n")]
    overall_method = _merge([{**row["method"], "n": row["n"]} for row in scored])
    overall_balance = _merge(
        [{**row["baselines"]["仅金额匹配"], "n": row["n"]} for row in scored if "仅金额匹配" in row.get("baselines", {})]
    )
    overall_time = _merge(
        [{**row["baselines"]["仅时间最近"], "n": row["n"]} for row in scored if "仅时间最近" in row.get("baselines", {})]
    )

    groups: list[dict] = []
    for label in ("含竞争条件", "零竞争条件"):
        rows = [row for row in scored if row["competition"] == label]
        if not rows:
            continue
        groups.append(
            {
                "competition": label,
                "data": "合成数据",
                "method": _merge([{**row["method"], "n": row["n"]} for row in rows]),
                "baselines": [
                    {
                        "name": "仅金额匹配",
                        **_merge(
                            [
                                {**row["baselines"]["仅金额匹配"], "n": row["n"]}
                                for row in rows
                                if "仅金额匹配" in row.get("baselines", {})
                            ]
                        ),
                    },
                    {
                        "name": "仅时间最近",
                        **_merge(
                            [
                                {**row["baselines"]["仅时间最近"], "n": row["n"]}
                                for row in rows
                                if "仅时间最近" in row.get("baselines", {})
                            ]
                        ),
                    },
                ],
            }
        )

    totals = graph.summarize_totals(dataset)
    payload = {
        "schema_version": "mixer_offramp/benchmark/v0.1",
        "is_simulated": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "dataset": {
            "name": dataset.name,
            "path": str(target.relative_to(Path(__file__).resolve().parent.parent)).replace("\\", "/")
            if target.is_absolute() and Path(__file__).resolve().parent.parent in target.parents
            else str(target),
            "cases": totals["cases"],
            "nodes": totals["nodes"],
            "edges": totals["edges"],
            "truth_links": totals["truth_links"],
            "scored_deposits": overall_method["n"],
            "input_sha256": result["input_sha256"],
            "source_file_sha256": result.get("source_file_sha256"),
        },
        "competition": {
            "label": "含竞争条件",
            "note": "数据集同时包含「含竞争条件」与「零竞争条件」两组；分组的指标见 by_competition，"
            "不要把任一组的数字当成整体效果。",
        },
        "overall": {
            "competition": "含竞争条件 + 零竞争条件",
            "data": "合成数据",
            "method": {"name": "六维加权归因", "metric": "归因一致性", **overall_method},
            "baselines": [
                {"name": "仅金额匹配", "p_at_1": overall_balance["p_at_1"], "p_at_3": overall_balance["p_at_3"],
                 "mrr": overall_balance["mrr"], "n": overall_balance["n"]},
                {"name": "仅时间最近", "p_at_1": overall_time["p_at_1"], "p_at_3": overall_time["p_at_3"],
                 "mrr": overall_time["mrr"], "n": overall_time["n"]},
            ],
        },
        "by_competition": groups,
        "by_case": per_case,
        "wording": {
            "allowed": "归因一致性 p@1（含竞争条件 · 合成数据）",
            "required_qualifiers": ["含竞争条件", "合成数据"],
            "forbidden_uses": [
                "不得作为实战效果或案件结论的度量",
                "不得在未标注合成数据与竞争条件时单独出现任何百分比",
                "不得用于真实案件或与真实案件结果对比",
            ],
        },
        "limits": [
            "全部数据由 algo2/generate_fixture.py 程序生成，真值来自构造过程，不是外部标注。",
            "评估集刻意放入了与真出金等额的诱饵出金；真实混币服务的诱饵更多、更随机，本集不能代表真实分布。",
            "p@1 在并列时按 1/并列数折算，因此固定面额池一类结构不可解的案件会显著拉低均值。",
            "基线只实现两种单特征排序，不代表实战中调查人员会使用的全部先验（如地址画像、时序链推断）。",
            "可控性分与路径分没有真值，无法评估，只能复算。",
        ],
    }

    text = __import__("json").dumps(payload, ensure_ascii=False, indent=2)
    for word in BANNED:
        assert word not in text, f"benchmark.json 不得出现表述：{word}"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(text + "\n", encoding="utf-8")
    return payload


def main() -> None:
    payload = run()
    overall = payload["overall"]
    print(f"已生成 {OUTPUT}")
    print(
        f"评估集：{payload['dataset']['cases']} 案 / {payload['dataset']['edges']} 边 / "
        f"{payload['dataset']['truth_links']} 对真值 / {payload['dataset']['scored_deposits']} 笔入金参与评分"
    )
    print(f"本法    p@1={overall['method']['p_at_1']}  p@3={overall['method']['p_at_3']}  MRR={overall['method']['mrr']}")
    for row in overall["baselines"]:
        print(f"{row['name']:<8} p@1={row['p_at_1']}  p@3={row['p_at_3']}  MRR={row['mrr']}")
    for group in payload["by_competition"]:
        method = group["method"]
        print(
            f"  [{group['competition']}] n={method['n']} 本法 p@1={method['p_at_1']}"
            + "".join(f"  {b['name']}={b['p_at_1']}" for b in group["baselines"])
        )


if __name__ == "__main__":
    main()

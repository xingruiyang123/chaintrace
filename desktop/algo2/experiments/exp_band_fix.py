# -*- coding: utf-8 -*-
"""按官方口径（仅含有真值的入金、并列按 1/k 折算）重做「池判别分 × 归因正确率」分档。

先前一版把没有真值的入金也算进分母（42 行 vs 官方 31 笔），导致分档数字失真。
本脚本只用 case_metrics 认定的 31 笔。
"""
import collections
import json
import sys
from pathlib import Path

DESKTOP = Path(r"D:\本：公安应用科创\_归档_20261114_v0.2.1应用\OUTPUT_20261113_混币法币识别与可视化增强_BLOCKED\desktop")
sys.path.insert(0, str(DESKTOP))
sys.path.insert(0, str(DESKTOP / "离线原型"))

from algo2 import pipeline  # noqa: E402

FIX = DESKTOP / "algo2" / "fixtures" / "mixer_eval_cases.json"
result, _ = pipeline.run_file(str(FIX))

rows = []
for case in result["cases"]:
    scores = {p["pool_id"]: p["detection_score"] for p in case.get("mixer_pools", [])}
    by = collections.defaultdict(list)
    for link in case.get("links", []):
        by[(link["pool_id"], link["deposit_edge"])].append(link)
    for (pid, dep), links in by.items():
        truth = [l for l in links if l.get("ground_truth_hit")]
        if not truth:
            # 该入金在真值表里没有对应出金 —— 不计入官方 31 笔
            continue
        top = max(l["score"] for l in links)
        top_set = [l for l in links if abs(l["score"] - top) < 1e-9]
        credit = 0.0
        if any(l.get("ground_truth_hit") for l in top_set):
            credit = 1.0 / len(top_set)          # 并列按 1/并列数折算（官方口径）
        rows.append(dict(case=case["case_id"], pool=pid, dep=dep,
                         pool_score=scores.get(pid, 0.0), credit=credit,
                         n_cand=len(links), gap=(sorted((l["score"] for l in links), reverse=True) + [0, 0])[0]
                         - (sorted((l["score"] for l in links), reverse=True) + [0, 0])[1]))

print("有真值的入金数：", len(rows), "（官方 scored_deposits 应为 31）")
total = sum(r["credit"] for r in rows)
print("整体 p@1（并列折算）= %.4f" % (total / len(rows)))

print("\n按池判别分分档：")
band = collections.defaultdict(list)
for r in rows:
    s = r["pool_score"]
    key = "≥60" if s >= 60 else ("45–60" if s >= 45 else "<45")
    band[key].append(r)
print("%-8s %5s %10s %12s %10s" % ("池分档", "入金", "平均并列数", "中位分数间隔", "p@1(折算)"))
for k in ("≥60", "45–60", "<45"):
    v = band[k]
    if not v:
        continue
    p = sum(x["credit"] for x in v) / len(v)
    ties = sum(x["n_cand"] for x in v) / len(v)
    gaps = sorted(x["gap"] for x in v)
    med = gaps[len(gaps) // 2]
    print("%-8s %5d %10.1f %12.1f %10.3f" % (k, len(v), ties, med, p))

# -*- coding: utf-8 -*-
"""量化「池判别分」与「归因置信档」的脱节：低分池产出了多少 HIGH 候选。"""
import collections
import json
import sys
from pathlib import Path

DESKTOP = Path(r"D:\本：公安应用科创\_归档_20261114_v0.2.1应用\OUTPUT_20261113_混币法币识别与可视化增强_BLOCKED\desktop")
sys.path.insert(0, str(DESKTOP))
sys.path.insert(0, str(DESKTOP / "离线原型"))

from algo2 import pipeline  # noqa: E402

FIX = DESKTOP / "algo2" / "fixtures" / "mixer_eval_cases.json"
result, _log = pipeline.run_file(str(FIX))

def pool_tier(score: float) -> str:
    if score >= 70:
        return "池HIGH"
    if score >= 45:
        return "池MEDIUM"
    return "池LOW"


cross = collections.Counter()
detail = []
for case in result["cases"]:
    pools = {p["pool_id"]: p for p in case.get("mixer_pools", [])}
    for link in case.get("links", []):
        p = pools.get(link["pool_id"], {})
        pt = pool_tier(p.get("detection_score", 0) or 0)
        cross[(pt, link["tier"])] += 1
        if pt == "池LOW" and link["tier"] == "HIGH":
            feats = link["features"]
            detail.append(dict(
                case=case["case_id"], link=link["link_id"],
                pool=link["pool_id"], pool_score=p.get("detection_score"),
                link_score=link["score"],
                amount=feats["amount"]["value"], time=feats["time"]["value"],
                path_rarity=feats["path_rarity"]["value"],
                cooccurrence=feats["cooccurrence"]["value"],
            ))

print("=== 池判别档 × 归因档 交叉表 ===")
tiers = ["HIGH", "MEDIUM", "LOW"]
print("%-10s %s" % ("", "  ".join("%8s" % t for t in tiers)))
for pt in ["池HIGH", "池MEDIUM", "池LOW"]:
    row = "  ".join("%8d" % cross.get((pt, t), 0) for t in tiers)
    print("%-10s %s" % (pt, row))

total = sum(cross.values())
bad = sum(v for (pt, t), v in cross.items() if pt == "池LOW" and t == "HIGH")
print("\n候选总数 %d；其中「低分池(潜在非混币)却判 HIGH」%d 条，占 %.1f%%" % (total, bad, 100 * bad / total))

print("\n=== 低分池里的 HIGH 候选明细（前 10）===")
for d in detail[:10]:
    print("  %-12s %-7s 池分 %5.1f → 归因 %5.1f | amount %5.1f time %5.1f rarity %5.1f cooc %5.1f"
          % (d["case"], d["link"], d["pool_score"], d["link_score"],
             d["amount"], d["time"], d["path_rarity"], d["cooccurrence"]))

out = DESKTOP / "algo2" / "experiments" / "out" / "propagation.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "cross_tab": {"%s|%s" % k: v for k, v in cross.items()},
    "total_links": total,
    "low_pool_high_link": bad,
    "detail": detail,
}, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n已写入", out)

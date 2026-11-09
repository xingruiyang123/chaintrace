"""Deterministic scale and ranking experiment on fabricated transaction records only."""
import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid5

from chaintrace_matcher import Engine, UUID_NS, sha256


def fabricate(seed=20261104, n_events=400, n_claims=200):
    rng = random.Random(seed)
    case = str(uuid5(UUID_NS, "SYNTHETIC-BENCH-1"))
    base = datetime(2026, 10, 1, tzinfo=timezone.utc)
    ds = {"name":"synthetic-ranking-benchmark", "is_simulated":True,
          "notice":"All entries fabricated; not representative of police case distributions.",
          "records":[], "events":[], "claims":[], "verified_person_accounts":[]}
    def add_src(name):
        payload = {"name":name, "synthetic":True}
        ds["records"].append({"id":f"src-{name}","case_id":case,"payload":payload,
                              "raw_sha256":sha256(payload),"is_simulated":True})
        return f"src-{name}"

    targets = {}
    for i in range(n_events):
        t = base + timedelta(seconds=rng.randrange(24 * 60 * 60))
        asset_num = rng.randrange(5)
        amount = rng.randrange(10000, 1000000) / 100
        eid = f"benchmark-event-{i}"
        ext = f"fake-benchmark-hash-{i}" if i % 2 == 0 else None
        ds["events"].append({"id":eid,"case_id":case,"event_type":"ONCHAIN_TRANSFER",
                             "asset_id":f"ASSET-{asset_num}","network_id":"BENCHNET",
                             "amount":str(amount),"event_time":t.isoformat(),
                             "from_account":f"wallet_s_{i}","to_account":f"wallet_r_{i}",
                             "external_id_type":"TX_HASH" if ext else None,"external_id":ext,
                             "source_ids":[add_src(eid)]})
        targets[i] = (t, amount, asset_num, ext)

    expected: dict[str,str|None] = {}
    for j in range(n_claims):
        # 75% known positives; 25% intentionally unmatched claims.
        pos = j < int(n_claims * .75)
        idx = j if pos else None
        if pos:
            t, amount, asset, ext = targets[idx]
            t = t + timedelta(seconds=rng.randrange(-600,600))
            subj, obj = f"wallet_s_{idx}", f"wallet_r_{idx}"
        else:
            # Hard negatives deliberately mimic all available non-ID observations
            # but have no corroborated true payment. They expose ambiguity:
            # metadata similarity alone cannot prove a real-world assertion.
            if j < int(n_claims * .75) + max(1, n_claims // 20):
                k = j % n_events
                t, amount, asset, _ = targets[k]
                subj, obj = f"wallet_s_{k}", f"wallet_r_{k}"
            else:
                t = base + timedelta(seconds=rng.randrange(24*60*60))
                amount = rng.randrange(10000,1000000)/100
                asset = rng.randrange(5)
                subj, obj = f"nonexistent_sender_{j}", f"nonexistent_receiver_{j}"
            ext = None
        claim_id = f"benchmark-claim-{j}"
        expected[claim_id] = f"benchmark-event-{idx}" if pos else None
        ds["claims"].append({"id":claim_id,"case_id":case,"predicate":"PAID_TO",
                             "subject_type":"ACCOUNT","subject_id":subj,
                             "object_type":"ACCOUNT","object_id":obj,
                             "asset_id":f"ASSET-{asset}","network_id":"BENCHNET",
                             "amount":str(amount),"time_from":t.isoformat(),"time_to":t.isoformat(),
                             "external_id_type":"TX_HASH" if ext else None,"external_id":ext,
                             "source_ids":[add_src(claim_id)]})
    return ds, expected


def benchmark(seed=20261104, n_events=400, n_claims=200):
    ds, truth = fabricate(seed, n_events, n_claims)
    start = perf_counter()
    engine = Engine(ds)
    matches, metrics = engine.candidates()
    elapsed = perf_counter() - start
    by_claim = defaultdict(list)
    for m in matches:
        if m.match_class not in {"CONFLICT", "INSUFFICIENT"}:
            by_claim[m.claim_id].append(m)
    for c in by_claim:
        by_claim[c].sort(key=lambda m:(-(m.score or 0),m.event_id))
    positives = {k:v for k,v in truth.items() if v is not None}
    negatives = {k for k,v in truth.items() if v is None}
    top1 = sum(bool(by_claim[k]) and by_claim[k][0].event_id == truth[k] for k in positives)
    top5 = sum(any(x.event_id == truth[k] for x in by_claim[k][:5]) for k in positives)
    false_alarm_claims = sum(bool(by_claim[k]) for k in negatives)
    admissible = sum(len(x) for x in by_claim.values())
    return {
        "synthetic_only":True,"seed":seed,"event_count":n_events,"claim_count":n_claims,
        "positive_claim_count":len(positives),"negative_claim_count":len(negatives),
        "top1_hit_rate_over_known_positive_claims":round(top1 / len(positives),4) if positives else None,
        "top5_recall_over_known_positive_claims":round(top5 / len(positives),4) if positives else None,
        "negative_claim_false_alarm_rate":round(false_alarm_claims / len(negatives),4) if negatives else None,
        "negative_claims_with_candidate":false_alarm_claims,
        "non_conflict_candidate_count":admissible,
        "within_case_cartesian_combinations":metrics["within_case_cartesian_combinations"],
        "evaluated_pairs":metrics["evaluated_pairs"],
        "candidate_compression_ratio":round(1-admissible/max(metrics["within_case_cartesian_combinations"],1),4),
        "elapsed_seconds":round(elapsed,4),
        "limitations":"Not a field validation. Labels, times and funds are artificially generated.",
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--events",type=int,default=400)
    p.add_argument("--claims",type=int,default=200)
    p.add_argument("--seed",type=int,default=20261104)
    p.add_argument("--output",default="outputs/benchmark_metrics.json")
    args=p.parse_args()
    if args.claims > args.events:
        p.error("--claims must be <= --events")
    r=benchmark(args.seed,args.events,args.claims)
    dest=Path(args.output); dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(json.dumps(r,indent=2,ensure_ascii=False)+"\n",encoding="utf8")
    print(json.dumps(r,indent=2,ensure_ascii=False))

if __name__=="__main__": main()

"""Deterministically generate synthetic A01-A10 cases. No real identities, wallets or txs."""
from pathlib import Path
from uuid import uuid5
import json
from chaintrace_matcher import UUID_NS, sha256

ROOT = Path(__file__).parent


def cid(name):
    return str(uuid5(UUID_NS, f"synthetic-case:{name}"))


def main():
    ds = {"name": "ChainTrace-A01-A10-offline-synthetic-v0.1", "is_simulated": True,
          "notice": "全量模拟。地址、编号、金额和时间均为实验构造，不对应真实案件。",
          "records": [], "events": [], "claims": [], "verified_person_accounts": []}

    def add_src(case, label, kind="SIMULATION"):
        s = {"id": f"src-{label}", "case_id": cid(case), "is_simulated": True,
             "payload": {"tag": label, "origin": kind, "simulated": True}}
        s["raw_sha256"] = sha256(s["payload"])
        ds["records"].append(s)
        return s["id"]

    def add_event(case, code, *, asset="TRON:USDT", network="TRON", amount="100", at="2026-10-01T12:00:00Z",
                  from_="wallet_A", to="wallet_B", ext_type=None, ext=None,
                  kind="ONCHAIN_TRANSFER", fee="0", sources=None):
        sources = sources or [add_src(case, code + "-event", "BLOCKCHAIN_FAKE")]
        ds["events"].append({"id": f"evt-{code}", "case_id": cid(case), "event_type": kind,
                              "network_id": network, "asset_id": asset, "amount": amount,
                              "event_time": at, "from_account": from_, "to_account": to,
                              "external_id_type": ext_type, "external_id": ext,
                              "fee": fee, "source_ids": sources})
        return f"evt-{code}"

    def add_claim(case, code, *, asset="TRON:USDT", network="TRON", amount="100",
                  start="2026-10-01T12:00:00Z", end="2026-10-01T12:00:00Z",
                  subject_type="ACCOUNT", subject="wallet_A", object_type="ACCOUNT", object="wallet_B",
                  predicate="PAID_TO", ext_type=None, ext=None, expected_type=None):
        ds["claims"].append({"id": f"claim-{code}", "case_id": cid(case), "predicate": predicate,
                             "subject_type": subject_type, "subject_id": subject,
                             "object_type": object_type, "object_id": object,
                             "asset_id": asset, "network_id": network, "amount": amount,
                             "time_from": start, "time_to": end,
                             "external_id_type": ext_type, "external_id": ext,
                             "expected_event_type": expected_type,
                             "source_ids": [add_src(case, code + "-claim", "TESTIMONY_FAKE")]})
        return f"claim-{code}"

    # A01. Same chain transaction identifier. Exact match remains PENDING.
    add_event("A01", "A01", ext_type="TX_HASH", ext="FAKE-TX-A01")
    add_claim("A01", "A01", ext_type="TX_HASH", ext="FAKE-TX-A01")
    # A02. Only a documented on-chain fee can explain the amount difference.
    add_event("A02", "A02", amount="100", fee="0.1")
    add_claim("A02", "A02", amount="99.9")
    # A03. One hour between reported payment and observed transfer.
    add_event("A03", "A03")
    add_claim("A03", "A03", start="2026-10-01T13:00:00Z", end="2026-10-01T13:00:00Z")
    # A04. Identifier matches, network + asset differs => conflict diagnostic.
    add_event("A04", "A04", ext_type="TX_HASH", ext="FAKE-TX-A04")
    add_claim("A04", "A04", asset="ETH:USDT", network="ETHEREUM", ext_type="TX_HASH", ext="FAKE-TX-A04")
    # A05. On-chain payment and platform deposit must remain two events.
    add_event("A05", "A05-chain", from_="wallet_A", to="platform_deposit_address", ext_type="TX_HASH", ext="FAKE-TX-A05")
    add_event("A05", "A05-deposit", from_="platform_deposit_address", to="platform_internal_uid", kind="EXCHANGE_DEPOSIT",
              ext_type="TX_HASH", ext="FAKE-TX-A05", at="2026-10-01T12:00:03Z")
    add_claim("A05", "A05", object="platform_deposit_address", ext_type="TX_HASH", ext="FAKE-TX-A05", expected_type="ONCHAIN_TRANSFER")
    # A06. One event, two independent source records.
    s1 = add_src("A06", "A06-chain", "BLOCKCHAIN_FAKE")
    s2 = add_src("A06", "A06-secondary", "NODE_FAKE")
    add_event("A06", "A06", sources=[s1,s2])
    add_claim("A06", "A06")
    # A07 duplicate event is deliberately NOT inserted: the negative check is in tests.
    add_event("A07", "A07", ext_type="TX_HASH", ext="FAKE-TX-A07")
    add_claim("A07", "A07", ext_type="TX_HASH", ext="FAKE-TX-A07")
    # A08. Missing time and account identifiers, no unique external reference.
    add_event("A08", "A08")
    add_claim("A08", "A08", start=None, end=None, subject=None, object=None)
    # A09. Claimed direction contradicts direct sender/recipient accounts.
    add_event("A09", "A09", ext_type="TX_HASH", ext="FAKE-TX-A09")
    add_claim("A09", "A09", subject="wallet_B", object="wallet_A", ext_type="TX_HASH", ext="FAKE-TX-A09")
    # A10. Simulation indicator survives in output and provenance.
    add_event("A10", "A10", ext_type="TX_HASH", ext="FAKE-TX-A10")
    add_claim("A10", "A10", ext_type="TX_HASH", ext="FAKE-TX-A10")
    # A11. Person-to-wallet relation is eligible only when reviewed with evidence.
    add_event("A11", "A11")
    add_claim("A11", "A11", subject_type="PERSON", subject="person_X")
    ds["verified_person_accounts"].append({"case_id":cid("A11"), "person_id":"person_X",
             "account_id":"wallet_A", "reviewed":True, "supporting_evidence_id":"synthetic-evidence-A11", "disputed":False})
    # A12. A person name alone must never be used as proof of account ownership.
    add_event("A12", "A12")
    add_claim("A12", "A12", subject_type="PERSON", subject="person_unverified")

    out = ROOT / "fixtures" / "synthetic_cases.json"
    out.write_text(json.dumps(ds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out} ({len(ds['events'])} events, {len(ds['claims'])} claims, {len(ds['records'])} sources)")


if __name__ == "__main__":
    main()

import copy
import json
from pathlib import Path
from decimal import Decimal
from uuid import UUID

import pytest

from chaintrace_matcher import Engine, Config, load_fixture, sha256, VERSION

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fixture():
    return load_fixture(str(ROOT / "fixtures" / "synthetic_cases.json"))


@pytest.fixture
def engine(fixture):
    return Engine(fixture)


def pair(engine, case):
    return [m for m in engine.candidates()[0] if m.event_id == f"evt-{case}" and m.claim_id == f"claim-{case}"][0]


def test_a01_exact_transaction_id_is_not_automatically_accepted(engine):
    m = pair(engine, "A01")
    assert m.match_class == "EXACT"
    assert m.review_status == "PENDING"
    assert m.score == 1


def test_a02_fee_adjustment_requires_documented_fee(engine):
    m = pair(Engine(load_fixture(str(ROOT / "fixtures" / "synthetic_cases.json")), Config(relative_tolerance=Decimal("0.0001"))), "A02")
    assert not m.conflict_items
    assert m.explanation["fee_explains_difference"] is True
    assert m.review_status == "PENDING"


def test_fee_without_documentation_is_conflict(fixture):
    altered = copy.deepcopy(fixture)
    e = next(x for x in altered["events"] if x["id"] == "evt-A02")
    e["fee"] = "0"
    m = pair(Engine(altered, Config(relative_tolerance=Decimal("0.0001"))), "A02")
    assert "AMOUNT_OUT_OF_TOLERANCE" in m.conflict_items


def test_a03_one_hour_time_shift_is_candidate(engine):
    m = pair(engine, "A03")
    assert m.match_class == "CANDIDATE_STRONG"
    assert m.explanation["time_distance_seconds"] == 3600


def test_a04_same_symbol_other_network_conflicts(engine):
    m = pair(engine, "A04")
    assert m.review_status == "DISPUTED"
    assert "ASSET_MISMATCH" in m.conflict_items


def test_a05_separate_events_reconcile_but_do_not_merge(engine):
    ref = next(e for e in engine.events if e.id == "evt-A05-chain")
    events = [e for e in engine.events if e.case_id == ref.case_id]
    assert any(e.event_type == "EXCHANGE_DEPOSIT" for e in events)
    assert any(e.event_type == "ONCHAIN_TRANSFER" for e in events)
    links = engine.reconcile_event_pairs()
    assert len(links) == 1
    assert links[0]["explanation"]["separate_events_not_merged"] is True
    assert links[0]["review_status"] == "PENDING"


def test_a06_multiple_provenance_sources_one_event(engine):
    e = next(e for e in engine.events if e.id == "evt-A06")
    assert len(e.source_ids) == 2
    assert sum(x.id == e.id for x in engine.events) == 1
    assert pair(engine, "A06").explanation["source_integrity_ok"]


def test_a07_duplicate_chain_import_rejected(fixture):
    altered = copy.deepcopy(fixture)
    e = next(x for x in altered["events"] if x["id"] == "evt-A07")
    dupe = copy.deepcopy(e)
    dupe["id"] = "evt-A07-dup"
    altered["events"].append(dupe)
    with pytest.raises(ValueError, match="Duplicate on-chain event business key"):
        Engine(altered)


def test_a08_missing_time_and_accounts_are_not_contradictions(engine):
    m = pair(engine, "A08")
    assert m.match_class == "INSUFFICIENT"
    assert not m.conflict_items
    assert "claim_time_unknown" in m.missing_items
    assert "subject_account_unknown" in m.missing_items


def test_a09_reverse_account_direction_conflict(engine):
    m = pair(engine, "A09")
    assert m.review_status == "DISPUTED"
    assert "SUBJECT_ACCOUNT_CONFLICT" in m.conflict_items


def test_a10_all_fixtures_and_output_marked_simulated(engine):
    assert all(x.is_simulated for x in engine.records.values())
    bundle = engine.run_bundle()
    assert all(x["explanation"]["is_simulated"] for x in bundle["candidate_matches"])
    assert all(x["is_simulated"] for x in bundle["analysis_runs"])
    assert all(x["review_status"] in {"PENDING", "DISPUTED"} for x in bundle["candidate_matches"])


def test_a11_person_wallet_relation_requires_reviewed_evidence(engine):
    m = pair(engine, "A11")
    assert m.explanation["feature_scores"]["direction_accounts"] == "1"
    assert "subject_person_account_not_verified" not in m.missing_items


def test_a12_person_wallet_not_verified_downgrades_result(engine):
    m = pair(engine, "A12")
    assert m.match_class == "CANDIDATE_REVIEW"
    assert "subject_person_account_not_verified" in m.missing_items


def test_cross_case_sources_rejected(fixture):
    altered = copy.deepcopy(fixture)
    src = next(x for x in altered["events"] if x["id"] == "evt-A02")
    src["source_ids"] = [next(x for x in altered["events"] if x["id"] == "evt-A01")["source_ids"][0]]
    with pytest.raises(ValueError, match="Cross-case source"):
        Engine(altered)


def test_modified_source_payload_without_digest_update_is_disputed(fixture):
    altered = copy.deepcopy(fixture)
    s = next(s for s in altered["records"] if s["id"] == "src-A01-event")
    s["payload"]["tamper"] = 1
    m = pair(Engine(altered), "A01")
    assert m.match_class == "CONFLICT"
    assert any(x.startswith("SOURCE_HASH_MISMATCH") for x in m.conflict_items)


def test_duplicate_source_record_id_rejected(fixture):
    altered = copy.deepcopy(fixture)
    altered["records"].append(copy.deepcopy(altered["records"][0]))
    with pytest.raises(ValueError, match="Duplicate records id"):
        Engine(altered)


def test_timestamp_must_have_explicit_offset(fixture):
    altered = copy.deepcopy(fixture)
    altered["events"][0]["event_time"] = "2026-10-01T12:00:00"
    with pytest.raises(ValueError, match="timezone"):
        Engine(altered)


def test_each_case_has_own_database_run_and_stable_snapshot(engine):
    one = engine.run_bundle()
    two = engine.run_bundle()
    assert one == two
    by_id = {r["run_id"]: r["case_id"] for r in one["analysis_runs"]}
    assert len(by_id) == 12
    assert all(row["run_id"] in by_id and by_id[row["run_id"]] == row["case_id"]
               for row in one["candidate_matches"])
    assert all(len(r["input_snapshot_hash"]) == 64 for r in one["analysis_runs"])
    assert all(UUID(x["left_node_id"]) and UUID(x["right_node_id"]) for x in one["candidate_matches"])


def test_no_cross_case_comparison(engine):
    with pytest.raises(ValueError, match="Cross-case"):
        engine.compare(engine.events[0], engine.claims[1])


def test_hard_time_conflict_with_same_external_id(fixture):
    altered = copy.deepcopy(fixture)
    c = next(x for x in altered["claims"] if x["id"] == "claim-A01")
    c["time_from"] = "2026-11-01T12:00:00Z"
    c["time_to"] = "2026-11-01T12:00:00Z"
    m = pair(Engine(altered), "A01")
    assert "TIME_OUT_OF_WINDOW" in m.conflict_items
    assert m.review_status == "DISPUTED"


def test_provenance_digest_uses_canonical_json_not_pg_jsonb_text(fixture):
    s = fixture["records"][0]
    assert sha256(s["payload"]) == s["raw_sha256"]


def test_missing_true_source_rejected(fixture):
    altered = copy.deepcopy(fixture)
    altered["events"][0]["source_ids"] = ["unknown-source"]
    with pytest.raises(ValueError, match="Missing source"):
        Engine(altered)


def test_non_simulated_provenance_rejected(fixture):
    altered = copy.deepcopy(fixture)
    altered["records"][0]["is_simulated"] = False
    with pytest.raises(ValueError, match="Non-simulated"):
        Engine(altered)

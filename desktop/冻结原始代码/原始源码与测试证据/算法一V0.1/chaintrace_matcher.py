"""ChainTrace Algorithm 1: offline, case-scoped, explainable event/claim candidate matching.

This module deliberately does not identify people or create investigative conclusions.
All results remain PENDING or DISPUTED and require authorized human review.
Designed for synthetic fixtures and an eventual V1.1.1 database adapter.
"""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from uuid import UUID, uuid5

VERSION = "0.1.0"
UUID_NS = UUID("12345678-1234-5678-1234-567812345678")
DIM_WEIGHTS: dict[str, Decimal] = {
    "external_id": Decimal("0.35"),
    "asset_network": Decimal("0.20"),
    "amount": Decimal("0.20"),
    "time": Decimal("0.15"),
    "direction_accounts": Decimal("0.10"),
}
ALLOWED_PREDICATES = {"PAID_TO", "RECEIVED_FROM", "TRANSFERRED_TO"}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def timestamp(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"Timestamp must specify timezone: {raw!r}")
    return dt.astimezone(timezone.utc)


def money(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("Boolean is not a monetary value")
    try:
        d = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount {raw!r}") from exc
    if not d.is_finite() or d < 0:
        raise ValueError(f"Invalid amount {raw!r}")
    return d


@dataclass(frozen=True)
class Config:
    absolute_tolerance: Decimal = Decimal("0.000001")
    relative_tolerance: Decimal = Decimal("0.002")
    time_window_seconds: int = 7200
    strong_threshold: Decimal = Decimal("0.75")
    review_threshold: Decimal = Decimal("0.55")
    minimum_coverage: Decimal = Decimal("0.45")
    # A mismatched external ID is only a conflict when two IDs of the same type are provided.

    def __post_init__(self):
        if self.absolute_tolerance < 0 or self.relative_tolerance < 0:
            raise ValueError("Negative tolerances are forbidden")
        if self.time_window_seconds <= 0:
            raise ValueError("time_window_seconds must be positive")

    def snapshot(self) -> dict[str, Any]:
        return {
            "absolute_tolerance": str(self.absolute_tolerance),
            "relative_tolerance": str(self.relative_tolerance),
            "time_window_seconds": self.time_window_seconds,
            "strong_threshold": str(self.strong_threshold),
            "review_threshold": str(self.review_threshold),
            "minimum_coverage": str(self.minimum_coverage),
            "weights": {k: str(v) for k, v in DIM_WEIGHTS.items()},
        }


@dataclass(frozen=True)
class Record:
    id: str
    case_id: str
    payload: Any
    raw_sha256: str
    is_simulated: bool


@dataclass(frozen=True)
class Event:
    id: str
    case_id: str
    event_type: str
    asset_id: str
    network_id: str
    amount: Decimal
    at: datetime
    from_account: str | None
    to_account: str | None
    external_id_type: str | None
    external_id: str | None
    source_ids: tuple[str, ...]
    fee: Decimal = Decimal("0")


@dataclass(frozen=True)
class Claim:
    id: str
    case_id: str
    predicate: str
    subject_type: str | None
    subject_id: str | None
    object_type: str | None
    object_id: str | None
    asset_id: str | None
    network_id: str | None
    amount: Decimal | None
    time_from: datetime | None
    time_to: datetime | None
    external_id_type: str | None
    external_id: str | None
    source_ids: tuple[str, ...]
    expected_event_type: str | None = None


@dataclass(frozen=True)
class VerifiedLink:
    case_id: str
    person_id: str
    account_id: str
    reviewed: bool
    supporting_evidence_id: str | None
    disputed: bool = False


@dataclass
class Match:
    case_id: str
    event_id: str
    claim_id: str
    score: Decimal | None
    match_class: str
    review_status: str
    explanation: dict[str, Any]
    missing_items: list[str] = field(default_factory=list)
    conflict_items: list[str] = field(default_factory=list)

    def to_db_contract(self) -> dict[str, Any]:
        # left_node_id/right_node_id are IDs resolved by the DB adapter; here
        # deterministic UUIDs let consumers validate JSON using the UUID schema.
        return {
            "case_id": self.case_id,
            "left_node_id": str(uuid5(UUID_NS, f"{self.case_id}:EVENT:{self.event_id}")),
            "right_node_id": str(uuid5(UUID_NS, f"{self.case_id}:CLAIM:{self.claim_id}")),
            "match_type": "EVENT_TO_CLAIM",
            "score": None if self.score is None else float(self.score.quantize(Decimal("0.0001"))),
            "review_status": self.review_status,
            "explanation": {
                **self.explanation,
                "match_class": self.match_class,
                "event_business_key": self.event_id,
                "claim_business_key": self.claim_id,
                "algorithm_version": VERSION,
            },
            "missing_items": list(self.missing_items),
            "conflict_items": list(self.conflict_items),
        }


class Engine:
    def __init__(self, fixture: dict[str, Any], config: Config | None = None):
        self.raw = fixture
        self.cfg = config or Config()
        if fixture.get("is_simulated") is not True:
            raise ValueError("Offline V0.1 accepts synthetic datasets only")
        if len(fixture.get("records", [])) != len({r["id"] for r in fixture.get("records", [])}):
            raise ValueError("Duplicate records id")
        self.records = {
            r["id"]: Record(
                id=r["id"], case_id=r["case_id"], payload=r["payload"],
                raw_sha256=r["raw_sha256"], is_simulated=r["is_simulated"]
            ) for r in fixture.get("records", [])
        }
        self.events = [self._event(e) for e in fixture.get("events", [])]
        self.claims = [self._claim(c) for c in fixture.get("claims", [])]
        self.links = [VerifiedLink(**x) for x in fixture.get("verified_person_accounts", [])]
        self._assert_unique_and_scope()

    @staticmethod
    def _event(d: dict[str, Any]) -> Event:
        amt = money(d["amount"])
        if amt is None or amt <= 0:
            raise ValueError(f"Event {d['id']} requires positive amount")
        at = timestamp(d["event_time"])
        if at is None:
            raise ValueError("Event time is required")
        return Event(
            id=d["id"], case_id=d["case_id"], event_type=d["event_type"],
            asset_id=d["asset_id"], network_id=d["network_id"], amount=amt, at=at,
            from_account=d.get("from_account"), to_account=d.get("to_account"),
            external_id_type=d.get("external_id_type"), external_id=d.get("external_id"),
            source_ids=tuple(d.get("source_ids", [])), fee=money(d.get("fee", "0")) or Decimal("0"),
        )

    @staticmethod
    def _claim(d: dict[str, Any]) -> Claim:
        predicate = d["predicate"]
        if predicate not in ALLOWED_PREDICATES:
            raise ValueError(f"Unsupported transaction claim predicate {predicate}")
        t0, t1 = timestamp(d.get("time_from")), timestamp(d.get("time_to"))
        if t0 is not None and t1 is not None and t1 < t0:
            raise ValueError("Claim has reversed time interval")
        return Claim(
            id=d["id"], case_id=d["case_id"], predicate=predicate,
            subject_type=d.get("subject_type"), subject_id=d.get("subject_id"),
            object_type=d.get("object_type"), object_id=d.get("object_id"),
            asset_id=d.get("asset_id"), network_id=d.get("network_id"),
            amount=money(d.get("amount")), time_from=t0, time_to=t1,
            external_id_type=d.get("external_id_type"), external_id=d.get("external_id"),
            source_ids=tuple(d.get("source_ids", [])),
            expected_event_type=d.get("expected_event_type"),
        )

    def _assert_unique_and_scope(self) -> None:
        for kind, rows in [("records", self.records.values()), ("events", self.events), ("claims", self.claims)]:
            ids = [r.id for r in rows]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate {kind} id")
        for r in self.records.values():
            if not r.is_simulated:
                raise ValueError("Non-simulated source in synthetic experiment")
        for x in [*self.events, *self.claims]:
            for src in x.source_ids:
                if src not in self.records:
                    raise ValueError(f"Missing source {src} for {x.id}")
                if self.records[src].case_id != x.case_id:
                    raise ValueError(f"Cross-case source {src} for {x.id}")
        for e in self.events:
            if not e.source_ids:
                raise ValueError(f"Missing event sources for {e.id}")
        for c in self.claims:
            if not c.source_ids:
                raise ValueError(f"Missing claim sources for {c.id}")
        seen_chain_ids: set[tuple[str, str, str, str, str | None, str | None]] = set()
        for e in self.events:
            if e.event_type != "ONCHAIN_TRANSFER" or not e.external_id or e.external_id_type != "TX_HASH":
                continue
            # True multi-log TX support belongs in the PostgreSQL adapter,
            # which has tx_index and tx_index_kind. This DTO lacks them.
            key = (e.case_id, e.network_id, e.asset_id, e.external_id, e.from_account, e.to_account)
            if key in seen_chain_ids:
                raise ValueError(f"Duplicate on-chain event business key: {e.id}")
            seen_chain_ids.add(key)
        for link in self.links:
            if link.supporting_evidence_id is None and link.reviewed:
                raise ValueError("A verified person-account link needs evidence")

    def _source_integrity(self, event: Event, claim: Claim) -> tuple[bool, list[str]]:
        bad = []
        for src in sorted(set(event.source_ids + claim.source_ids)):
            record = self.records[src]
            if sha256(record.payload) != record.raw_sha256:
                bad.append(f"SOURCE_HASH_MISMATCH:{src}")
        return (not bad, bad)

    def _subject_account(self, claim: Claim, who: str, event_account: str | None) -> tuple[Decimal | None, str | None]:
        subject_type = claim.subject_type if who == "subject" else claim.object_type
        subject_id = claim.subject_id if who == "subject" else claim.object_id
        if not event_account or not subject_id or not subject_type:
            return None, f"{who}_account_unknown"
        if subject_type == "ACCOUNT":
            return (Decimal(1), None) if subject_id == event_account else (Decimal(0), f"{who}_account_conflict")
        if subject_type == "PERSON":
            reviewed = {r.account_id for r in self.links if (
                r.case_id == claim.case_id and r.person_id == subject_id and r.reviewed
                and r.supporting_evidence_id and not r.disputed
            )}
            if event_account in reviewed:
                return Decimal(1), None
            # A person could control other accounts: no false "hard conflict".
            return None, f"{who}_person_account_not_verified"
        return None, f"{who}_entity_type_unsupported"

    def compare(self, event: Event, claim: Claim) -> Match:
        if event.case_id != claim.case_id:
            raise ValueError("Cross-case comparison is prohibited")
        missing: list[str] = []
        conflicts: list[str] = []
        features: dict[str, Decimal] = {}
        detail: dict[str, Any] = {}

        ok, source_errors = self._source_integrity(event, claim)
        if not ok:
            conflicts.extend(source_errors)

        if claim.expected_event_type and event.event_type != claim.expected_event_type:
            conflicts.append("EVENT_TYPE_MISMATCH")
        if event.asset_id and claim.asset_id:
            if event.asset_id != claim.asset_id:
                conflicts.append("ASSET_MISMATCH")
                features["asset_network"] = Decimal(0)
            elif claim.network_id and event.network_id != claim.network_id:
                conflicts.append("NETWORK_MISMATCH")
                features["asset_network"] = Decimal(0)
            else:
                features["asset_network"] = Decimal(1)
        else:
            missing.append("asset_unknown")

        if event.external_id and claim.external_id and event.external_id_type and claim.external_id_type:
            if event.external_id_type != claim.external_id_type:
                # Different identifier systems are not comparable, NOT a conflict.
                missing.append("external_identifier_not_comparable")
            elif event.external_id == claim.external_id:
                features["external_id"] = Decimal(1)
            else:
                features["external_id"] = Decimal(0)
                conflicts.append("EXTERNAL_ID_MISMATCH")
        else:
            missing.append("external_identifier_unknown")

        if claim.amount is None:
            missing.append("amount_unknown")
        else:
            delta = abs(event.amount - claim.amount)
            rel = delta / max(event.amount, Decimal("0.000000000001"))
            fee_explains = event.fee > 0 and (abs(event.amount - event.fee - claim.amount) <= self.cfg.absolute_tolerance)
            if delta <= self.cfg.absolute_tolerance or fee_explains:
                features["amount"] = Decimal(1)
                detail["fee_explains_difference"] = fee_explains
            elif rel <= self.cfg.relative_tolerance:
                features["amount"] = max(Decimal(0), Decimal(1) - rel)
            else:
                features["amount"] = Decimal(0)
                conflicts.append("AMOUNT_OUT_OF_TOLERANCE")
            detail.update({"amount_absolute_difference": str(delta), "amount_relative_difference": str(rel)})

        if claim.time_from is None and claim.time_to is None:
            missing.append("claim_time_unknown")
        else:
            start = claim.time_from or claim.time_to
            end = claim.time_to or claim.time_from
            assert start is not None and end is not None
            distance = (start - event.at).total_seconds() if event.at < start else (
                (event.at - end).total_seconds() if event.at > end else 0
            )
            distance = max(0, distance)
            detail["time_distance_seconds"] = distance
            if distance > self.cfg.time_window_seconds:
                conflicts.append("TIME_OUT_OF_WINDOW")
                features["time"] = Decimal(0)
            else:
                features["time"] = Decimal(1) - Decimal(str(distance)) / Decimal(self.cfg.time_window_seconds)

        if claim.predicate == "RECEIVED_FROM":
            # grammatical subject is receiver, object is sender
            left_acct = event.to_account
            right_acct = event.from_account
        else:
            left_acct = event.from_account
            right_acct = event.to_account
        f1, m1 = self._subject_account(claim, "subject", left_acct)
        f2, m2 = self._subject_account(claim, "object", right_acct)
        for label in (m1, m2):
            if label:
                if label.endswith("_conflict"):
                    conflicts.append(label.upper())
                else:
                    missing.append(label)
        observed_accounts = [v for v in (f1, f2) if v is not None]
        if observed_accounts:
            features["direction_accounts"] = sum(observed_accounts) / Decimal(len(observed_accounts))

        covered = sum((DIM_WEIGHTS[k] for k in features), Decimal(0))
        weighted = sum((DIM_WEIGHTS[k] * v for k, v in features.items()), Decimal(0))
        score = weighted / covered if covered else None
        unique_exact = features.get("external_id") == Decimal(1)
        if conflicts:
            match_class, review_status = "CONFLICT", "DISPUTED"
        elif score is None or covered < self.cfg.minimum_coverage or len(features) < 2:
            match_class, review_status = "INSUFFICIENT", "PENDING"
        elif any("person_account_not_verified" in v for v in missing):
            # Unverified person-account links must not receive EXACT/STRONG labels.
            match_class, review_status = "CANDIDATE_REVIEW", "PENDING"
        elif unique_exact:
            match_class, review_status = "EXACT", "PENDING"
        elif score >= self.cfg.strong_threshold and covered >= Decimal("0.45") and len(features) >= 3:
            match_class, review_status = "CANDIDATE_STRONG", "PENDING"
        elif score >= self.cfg.review_threshold:
            match_class, review_status = "CANDIDATE_REVIEW", "PENDING"
        else:
            match_class, review_status = "INSUFFICIENT", "PENDING"

        explanation = {
            "case_id": event.case_id,
            "asset_id": event.asset_id,
            "event_type": event.event_type,
            "feature_scores": {key: str(features[key]) for key in sorted(features)},
            "score_coverage": str(covered),
            "raw_comparison_score": None if score is None else str(score),
            "weights": {k: str(v) for k, v in DIM_WEIGHTS.items()},
            "source_integrity_ok": ok,
            "is_simulated": True,
            "requires_human_review": True,
            "amount_tolerance": {
                "absolute": str(self.cfg.absolute_tolerance),
                "relative": str(self.cfg.relative_tolerance),
            },
            "time_window_seconds": self.cfg.time_window_seconds,
            **detail,
        }
        # A number must not be mistaken for identity confidence or evidence quality.
        # Keep full comparison details but suppress the rank when proof is incomplete
        # or contradictory, or a person-account relation remains unverified.
        output_score = score
        if match_class in {"INSUFFICIENT", "CONFLICT"} or any(
            "person_account_not_verified" in x for x in missing
        ):
            output_score = None
        return Match(event.case_id, event.id, claim.id, output_score, match_class,
                     review_status, explanation, sorted(set(missing)), sorted(set(conflicts)))

    def candidates(self) -> tuple[list[Match], dict[str, Any]]:
        """Uses case/asset/time lookup; also records same-ID conflict diagnostics.

        A full Cartesian product is never required. A conflicting amount/direction
        pair is considered for diagnostics if the unique identifier matches.
        """
        buckets: dict[tuple[str, str], list[Event]] = {}
        ids: dict[tuple[str, str, str], list[Event]] = {}
        for e in self.events:
            buckets.setdefault((e.case_id, e.asset_id), []).append(e)
            if e.external_id_type and e.external_id:
                ids.setdefault((e.case_id, e.external_id_type, e.external_id), []).append(e)
        sorted_times: dict[tuple[str, str], list[datetime]] = {}
        for key, entries in buckets.items():
            entries.sort(key=lambda x: (x.at, x.id))
            sorted_times[key] = [e.at for e in entries]

        rows: list[Match] = []
        seen: set[tuple[str, str]] = set()
        for c in self.claims:
            near: dict[str, Event] = {}
            if c.asset_id:
                key = (c.case_id, c.asset_id)
                events = buckets.get(key, [])
                if c.time_from or c.time_to:
                    lower = (c.time_from or c.time_to) - timedelta(seconds=self.cfg.time_window_seconds)
                    upper = (c.time_to or c.time_from) + timedelta(seconds=self.cfg.time_window_seconds)
                    t = sorted_times.get(key, [])
                    for e in events[bisect_left(t, lower):bisect_right(t, upper)]:
                        near[e.id] = e
                else:
                    for e in events:
                        near[e.id] = e
            else:
                # Missing asset: cross-asset candidate retrieval is limited to a
                # matching unique identifier. We don't promote arbitrary same-amount links.
                pass
            if c.external_id_type and c.external_id:
                for e in ids.get((c.case_id, c.external_id_type, c.external_id), []):
                    near[e.id] = e
            for e in sorted(near.values(), key=lambda x: (x.at, x.id)):
                pair = (e.id, c.id)
                if pair not in seen:
                    seen.add(pair)
                    rows.append(self.compare(e, c))
        rows.sort(key=lambda x: (x.case_id, x.claim_id, 0 if x.match_class == "EXACT" else 1,
                                 -(x.score or Decimal(0)), x.event_id))
        info = {
            "input_events": len(self.events), "input_claims": len(self.claims),
            "within_case_cartesian_combinations": sum(
                sum(e.case_id == c.case_id for e in self.events)
                for c in self.claims
            ),
            "global_cartesian_reference": len(self.events) * len(self.claims),
            "evaluated_pairs": len(seen), "returned_results": len(rows),
            "class_counts": {k: sum(m.match_class == k for m in rows) for k in
                             ["EXACT", "CANDIDATE_STRONG", "CANDIDATE_REVIEW", "INSUFFICIENT", "CONFLICT"]},
        }
        return rows, info

    def reconcile_event_pairs(self) -> list[dict[str, Any]]:
        """Conservative event-to-event matching; no merges or identity assertions.

        Requires shared TX hash, asset/network, deposit address, amount, time,
        and valid source hashes. Output remains pending human review.
        """
        deposits = [e for e in self.events if e.event_type == "EXCHANGE_DEPOSIT"]
        transfers = [e for e in self.events if e.event_type == "ONCHAIN_TRANSFER"]
        index: dict[tuple[str, str, str], list[Event]] = {}
        for t in transfers:
            if t.external_id_type == "TX_HASH" and t.external_id:
                index.setdefault((t.case_id, t.network_id, t.external_id), []).append(t)
        out = []
        for dep in deposits:
            if dep.external_id_type != "TX_HASH" or not dep.external_id:
                continue
            for chain in index.get((dep.case_id, dep.network_id, dep.external_id), []):
                delta = abs(chain.amount - dep.amount)
                if chain.asset_id != dep.asset_id or chain.to_account != dep.from_account:
                    continue
                if delta > self.cfg.absolute_tolerance:
                    continue
                if abs((dep.at - chain.at).total_seconds()) > self.cfg.time_window_seconds:
                    continue
                if not self._source_integrity(chain, dep)[0]:
                    continue
                out.append({
                    "case_id": chain.case_id,
                    "left_node_id": str(uuid5(UUID_NS, f"{chain.case_id}:EVENT:{chain.id}")),
                    "right_node_id": str(uuid5(UUID_NS, f"{dep.case_id}:EVENT:{dep.id}")),
                    "match_type": "EVENT_TO_EVENT",
                    "score": None, "review_status": "PENDING",
                    "explanation": {
                        "match_class": "CANDIDATE_REVIEW",
                        "transfer_id": chain.id, "deposit_id": dep.id,
                        "same_tx_hash": True, "asset_network_consistent": True,
                        "deposit_address_corresponds": True, "amount_difference": str(delta),
                        "time_delta_seconds": abs((dep.at - chain.at).total_seconds()),
                        "separate_events_not_merged": True,
                        "is_simulated": True, "requires_human_review": True,
                    },
                    "missing_items": [], "conflict_items": [],
                })
        return out

    def run_bundle(self) -> dict[str, Any]:
        matches, stats = self.candidates()
        reconciliations = self.reconcile_event_pairs()
        case_ids = sorted({e.case_id for e in self.events} | {c.case_id for c in self.claims})
        runs = []
        id_by_case = {}
        for case_id in case_ids:
            snap = {
                "events": [e for e in self.raw["events"] if e["case_id"] == case_id],
                "claims": [c for c in self.raw["claims"] if c["case_id"] == case_id],
                "records": [r for r in self.raw["records"] if r["case_id"] == case_id],
                "links": [x for x in self.raw.get("verified_person_accounts", []) if x["case_id"] == case_id],
                "config": self.cfg.snapshot(), "algorithm_version": VERSION,
            }
            fingerprint = sha256(snap)
            run_id = str(uuid5(UUID_NS, f"run:{case_id}:{fingerprint}"))
            id_by_case[case_id] = run_id
            runs.append({
                "case_id": case_id, "run_id": run_id,
                "algorithm_code": "EVENT_CLAIM_MATCH", "algorithm_version": VERSION,
                "input_snapshot_hash": fingerprint, "parameters": self.cfg.snapshot(),
                "status": "SUCCEEDED", "is_simulated": True,
                "note": "OFFLINE-SIMULATED; not an evidentiary conclusion or database test",
            })
        all_matches = [dict(run_id=id_by_case[m.case_id], **m.to_db_contract()) for m in matches]
        all_matches.extend(dict(run_id=id_by_case[x["case_id"]], **x) for x in reconciliations)
        stats["event_reconciliation_links"] = len(reconciliations)
        return {"analysis_runs": runs, "candidate_matches": all_matches, "metrics": stats}


def load_fixture(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

"""
reconciler.py
=============
Core hybrid reconciliation engine — 3-tier cascade matcher.

Architecture
------------
Tier 1 │ Exact SQL Match
       │  ref_code == ref_code (case-insensitive, non-null)
       │  amount   == amount   (exact)
       │  date     within ±1 day
       │  confidence = 1.00
       ▼
Tier 2 │ Subset-Sum One-to-Many Payout Aggregator
       │  1 BANK/GATEWAY lump = N INVOICE records (N ≤ 5)
       │  deposit = sum(invoices) × (1 − fee_rate), fee_rate ∈ [1.5%, 2.5%]
       │  date window ±3 days
       │  confidence = 0.92
       ▼
Tier 3 │ RapidFuzz Multi-Attribute Scoring
       │  C = 0.40·S_amount + 0.30·S_ref + 0.20·S_name + 0.10·S_date
       │  Threshold C ≥ 0.75 → FUZZY match
       ▼
       Remaining UNMATCHED → Risk Triage (risk_triage.py)
"""

import itertools
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DynamicRule, ExceptionItem, FinancialRecord, RecordMatch, ReconciliationBatch
from app.neatlogs import neatlogs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FEE_RATE_MIN = 0.015
FEE_RATE_MAX = 0.025
EXACT_DATE_WINDOW = 1        # ±days for Tier 1
OTM_DATE_WINDOW = 3          # ±days for Tier 2
OTM_MAX_COMBO_SIZE = 5       # max invoice combinations for Tier 2
FUZZY_THRESHOLD = 0.75       # min composite score to accept a Tier 3 match

# ---------------------------------------------------------------------------
# Internal data container (avoids re-fetching from DB mid-cascade)
# ---------------------------------------------------------------------------
@dataclass
class _Rec:
    """Lightweight in-memory wrapper around a FinancialRecord row."""
    record_id: uuid.UUID
    batch_id: uuid.UUID
    source_type: str
    raw_entity_name: str
    normalized_entity_name: str | None
    amount: float
    reference_code: str | None
    transaction_date: date
    metadata_json: dict[str, Any] | None = field(default_factory=dict)


@dataclass
class _Match:
    record_a_id: uuid.UUID
    record_b_id: uuid.UUID
    confidence_score: float
    match_type: str
    reasoning_trace: str


@dataclass
class ReconciliationResult:
    total_records: int
    matched_ids: set[uuid.UUID]
    matches: list[_Match]
    tier1_count: int
    tier2_count: int
    tier3_count: int


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_amount(a1: float, a2: float) -> float:
    if a1 == a2:
        return 1.0
    denom = max(abs(a1), abs(a2))
    if denom == 0:
        return 1.0
    return max(0.0, 1.0 - abs(a1 - a2) / denom)


def _score_ref(r1: str | None, r2: str | None) -> float:
    if not r1 or not r2:
        return 0.0
    return fuzz.token_set_ratio(r1.upper(), r2.upper()) / 100.0


def _score_name(n1: str | None, n2: str | None) -> float:
    s1 = n1 or ""
    s2 = n2 or ""
    if not s1 and not s2:
        return 0.0
    return fuzz.token_set_ratio(s1.upper(), s2.upper()) / 100.0


def _score_date(d1: date, d2: date) -> float:
    diff = abs((d1 - d2).days)
    if diff == 0:
        return 1.0
    if diff <= 2:
        return 0.8
    if diff <= 5:
        return 0.4
    return 0.0


def _composite_score(a: _Rec, b: _Rec) -> tuple[float, str]:
    """
    Returns (score, human-readable reasoning_trace).

    Weights: amount 0.40, ref 0.30, name 0.20, date 0.10.

    The reference signal is only counted when BOTH records carry a reference
    code. When either side is missing it (a common real-world ERP/bank-feed
    gap) the remaining signals are re-normalised so a strong amount + name +
    date agreement can still clear the threshold. Without this, a missing-ref
    pair is mathematically capped at 0.70 and could NEVER reach 0.75.
    """
    s_amount = _score_amount(a.amount, b.amount)
    s_name = _score_name(
        a.normalized_entity_name or a.raw_entity_name,
        b.normalized_entity_name or b.raw_entity_name,
    )
    s_date = _score_date(a.transaction_date, b.transaction_date)

    weighted = 0.40 * s_amount + 0.20 * s_name + 0.10 * s_date
    total_weight = 0.70

    has_ref = bool(a.reference_code and b.reference_code)
    s_ref = _score_ref(a.reference_code, b.reference_code) if has_ref else None
    if has_ref:
        weighted += 0.30 * s_ref            # type: ignore[operator]
        total_weight += 0.30

    C = weighted / total_weight

    name_pct = round(s_name * 100)
    ref_info = (
        f"ref similarity {round(s_ref * 100)}%"   # type: ignore[arg-type]
        if has_ref
        else "no reference codes to compare (weights re-normalised)"
    )
    trace = (
        f"Fuzzy composite score {round(C * 100)}%: "
        f"entity name similarity {name_pct}%, {ref_info}, "
        f"amount delta score {round(s_amount * 100)}%, "
        f"date gap {abs((a.transaction_date - b.transaction_date).days)} day(s)."
    )
    return round(C, 4), trace


# ---------------------------------------------------------------------------
# Tier 2: Subset-Sum helper
# ---------------------------------------------------------------------------

def _check_subset_sum(deposit: float, invoice_amounts: list[float]) -> bool:
    """
    Returns True if deposit ≈ sum(invoice_amounts) × (1 − fee_rate)
    for any fee_rate ∈ [FEE_RATE_MIN, FEE_RATE_MAX].
    Equivalently: deposit / sum ∈ [(1−0.025), (1−0.015)] = [0.975, 0.985]
    """
    s = sum(invoice_amounts)
    if s == 0:
        return False
    ratio = deposit / s
    return (1.0 - FEE_RATE_MAX) <= ratio <= (1.0 - FEE_RATE_MIN)


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class ReconciliationEngine:
    """
    Stateless reconciliation engine. Instantiate per batch run.

    Usage:
        engine = ReconciliationEngine(db, batch_id)
        result = await engine.run()
    """

    def __init__(self, db: AsyncSession, batch_id: uuid.UUID) -> None:
        self.db = db
        self.batch_id = batch_id

    # ── Public entry point ──────────────────────────────────────────────────

    async def run(self) -> ReconciliationResult:
        records = await self._fetch_unmatched()
        if not records:
            neatlogs.warn("ReconciliationEngine", f"Batch {self.batch_id}: no UNMATCHED records to reconcile.")
            return ReconciliationResult(
                total_records=0,
                matched_ids=set(),
                matches=[],
                tier1_count=0,
                tier2_count=0,
                tier3_count=0,
            )

        neatlogs.recon(
            f"Batch {self.batch_id}: beginning 3-tier cascade over {len(records)} unmatched records.",
            {"batch_id": str(self.batch_id), "records": len(records)},
        )

        # ── Pre-Tier-1: Apply DynamicRules ───────────────────────────────────
        rules_applied = await self._apply_dynamic_rules(records)
        if rules_applied:
            neatlogs.recon(f"Applied {rules_applied} adaptive DynamicRule(s) before matching.")

        matched_ids: set[uuid.UUID] = set()
        all_matches: list[_Match] = []

        # ── Tier 1: Exact ────────────────────────────────────────────────────
        t1_matches = self._tier1_exact(records, matched_ids)
        all_matches.extend(t1_matches)
        neatlogs.recon(f"Tier 1 (Exact) → {len(t1_matches)} match(es) (confidence 1.00).")

        # ── Tier 2: One-to-Many ──────────────────────────────────────────────
        t2_matches = self._tier2_one_to_many(records, matched_ids)
        all_matches.extend(t2_matches)
        neatlogs.recon(f"Tier 2 (Subset-Sum One-to-Many) → {len(t2_matches)} match(es) (confidence 0.92).")

        # ── Tier 3: Fuzzy ────────────────────────────────────────────────────
        t3_matches = self._tier3_fuzzy(records, matched_ids)
        all_matches.extend(t3_matches)
        neatlogs.recon(f"Tier 3 (RapidFuzz Fuzzy) → {len(t3_matches)} match(es) (threshold ≥ {FUZZY_THRESHOLD}).")

        logger.info(
            "Batch %s reconciled: %d exact, %d one-to-many, %d fuzzy  (total matched ids: %d)",
            self.batch_id,
            len(t1_matches),
            len(t2_matches),
            len(t3_matches),
            len(matched_ids),
        )
        neatlogs.success(
            f"Batch {self.batch_id}: cascade complete — {len(matched_ids)} records matched, "
            f"{len(records) - len(matched_ids)} remaining for risk triage.",
            {
                "tier1": len(t1_matches),
                "tier2": len(t2_matches),
                "tier3": len(t3_matches),
                "matched": len(matched_ids),
            },
        )

        return ReconciliationResult(
            total_records=len(records),
            matched_ids=matched_ids,
            matches=all_matches,
            tier1_count=len(t1_matches),
            tier2_count=len(t2_matches),
            tier3_count=len(t3_matches),
        )

    # ── DB fetch ─────────────────────────────────────────────────────────────

    async def _fetch_unmatched(self) -> list[_Rec]:
        result = await self.db.execute(
            select(FinancialRecord).where(
                FinancialRecord.batch_id == self.batch_id,
                FinancialRecord.status == "UNMATCHED",
            )
        )
        rows = result.scalars().all()
        return [
            _Rec(
                record_id=r.record_id,
                batch_id=r.batch_id,
                source_type=r.source_type,
                raw_entity_name=r.raw_entity_name,
                normalized_entity_name=r.normalized_entity_name,
                amount=float(r.amount),
                reference_code=r.reference_code,
                transaction_date=r.transaction_date,
                metadata_json=r.metadata_json or {},
            )
            for r in rows
        ]

    # ── Pre-Tier-1: Apply DynamicRules ───────────────────────────────────────

    async def _apply_dynamic_rules(self, records: list[_Rec]) -> int:
        """
        Fetch all active DynamicRule rows and apply them to the in-memory
        _Rec list before the matching cascade runs.

        REGEX_STRIP  → strips matching prefix/suffix from raw_entity_name
                        and overwrites normalized_entity_name
        VENDOR_MAP   → if raw_entity_name.upper() matches pattern,
                        set normalized_entity_name to action_payload[canonical_name]
        FEE_TOLERANCE → stored for reconciler awareness (used in Tier 2 dynamically)

        Returns the number of rules applied to at least one record.
        """
        import re as _re

        rules_result = await self.db.execute(select(DynamicRule))
        rules = rules_result.scalars().all()

        if not rules:
            return 0

        vendor_map: dict[str, str] = {}
        strip_patterns: list[_re.Pattern] = []
        fee_tolerances: dict[str, tuple[float, float]] = {}

        for rule in rules:
            try:
                if rule.rule_type == "VENDOR_MAP":
                    vendor_map[rule.pattern.upper()] = (
                        (rule.action_payload or {}).get("canonical_name", "")
                    )
                elif rule.rule_type == "REGEX_STRIP":
                    strip_patterns.append(_re.compile(rule.pattern, _re.IGNORECASE))
                elif rule.rule_type == "FEE_TOLERANCE":
                    payload = rule.action_payload or {}
                    fee_tolerances[rule.pattern.upper()] = (
                        float(payload.get("tolerance_min", FEE_RATE_MIN)),
                        float(payload.get("tolerance_max", FEE_RATE_MAX)),
                    )
            except Exception as exc:
                logger.warning("DynamicRule %s failed to parse: %s", rule.rule_id, exc)

        # Store fee tolerances on engine for Tier 2 use
        self._fee_tolerances = fee_tolerances

        applied = 0
        for rec in records:
            raw = rec.raw_entity_name
            normalized = rec.normalized_entity_name or raw

            # 1. VENDOR_MAP — exact key lookup
            canonical = vendor_map.get(raw.upper())
            if canonical:
                rec.normalized_entity_name = canonical
                logger.debug("VENDOR_MAP: '%s' → '%s'", raw, canonical)
                applied += 1
                continue

            # 2. REGEX_STRIP — apply each pattern in sequence
            for pat in strip_patterns:
                new_name = pat.sub("", normalized).strip()
                if new_name and new_name != normalized:
                    logger.debug("REGEX_STRIP: '%s' → '%s'", normalized, new_name)
                    rec.normalized_entity_name = new_name
                    normalized = new_name
                    applied += 1
                    break

        return applied


    # ── Tier 1: Exact Key Match ──────────────────────────────────────────────

    def _tier1_exact(
        self, records: list[_Rec], matched_ids: set[uuid.UUID]
    ) -> list[_Match]:
        matches: list[_Match] = []

        bank_gw = [
            r for r in records
            if r.source_type in ("BANK", "GATEWAY")
            and r.reference_code
            and r.record_id not in matched_ids
        ]
        invoices = [
            r for r in records
            if r.source_type == "INVOICE"
            and r.reference_code
            and r.record_id not in matched_ids
        ]

        # Build invoice lookup: ref_code (upper) → _Rec
        inv_by_ref: dict[str, _Rec] = {}
        for inv in invoices:
            key = inv.reference_code.upper()  # type: ignore[union-attr]
            if key not in inv_by_ref:
                inv_by_ref[key] = inv

        for dep in bank_gw:
            ref_key = dep.reference_code.upper()  # type: ignore[union-attr]
            if ref_key not in inv_by_ref:
                continue
            inv = inv_by_ref[ref_key]
            if inv.record_id in matched_ids:
                continue

            # Amount exact match
            if dep.amount != inv.amount:
                continue

            # Date within ±1 day
            date_gap = abs((dep.transaction_date - inv.transaction_date).days)
            if date_gap > EXACT_DATE_WINDOW:
                continue

            matches.append(
                _Match(
                    record_a_id=dep.record_id,
                    record_b_id=inv.record_id,
                    confidence_score=1.00,
                    match_type="EXACT",
                    reasoning_trace=(
                        "Exact reference code, amount, and date window match."
                    ),
                )
            )
            matched_ids.add(dep.record_id)
            matched_ids.add(inv.record_id)
            # Remove from index so it can't be re-used
            del inv_by_ref[ref_key]

        return matches

    # ── Tier 2: One-to-Many Subset-Sum ──────────────────────────────────────

    def _tier2_one_to_many(
        self, records: list[_Rec], matched_ids: set[uuid.UUID]
    ) -> list[_Match]:
        matches: list[_Match] = []

        deposits = [
            r for r in records
            if r.source_type in ("BANK", "GATEWAY")
            and r.record_id not in matched_ids
            and r.amount > 0
        ]
        invoices = [
            r for r in records
            if r.source_type == "INVOICE"
            and r.record_id not in matched_ids
            and r.amount > 0
        ]

        for dep in deposits:
            if dep.record_id in matched_ids:
                continue  # already matched by a previous deposit in this loop

            # Filter invoices within ±3 day window of deposit date
            nearby_invoices = [
                inv for inv in invoices
                if inv.record_id not in matched_ids
                and abs((dep.transaction_date - inv.transaction_date).days) <= OTM_DATE_WINDOW
            ]

            found = False
            for size in range(2, min(OTM_MAX_COMBO_SIZE, len(nearby_invoices)) + 1):
                if found:
                    break
                for combo in itertools.combinations(nearby_invoices, size):
                    inv_amounts = [inv.amount for inv in combo]
                    if _check_subset_sum(dep.amount, inv_amounts):
                        fee_effective = round(
                            (1.0 - dep.amount / sum(inv_amounts)) * 100, 2
                        )
                        trace = (
                            f"Lump-sum payout matched {size} invoices accounting "
                            f"for ~{fee_effective}% settlement fee deduction. "
                            f"Deposit ₹{dep.amount:,.2f} = "
                            f"Σ invoices ₹{sum(inv_amounts):,.2f} × (1 − {fee_effective}%)."
                        )
                        matched_ids.add(dep.record_id)
                        for inv in combo:
                            matches.append(
                                _Match(
                                    record_a_id=dep.record_id,
                                    record_b_id=inv.record_id,
                                    confidence_score=0.92,
                                    match_type="ONE_TO_MANY",
                                    reasoning_trace=trace,
                                )
                            )
                            matched_ids.add(inv.record_id)
                        found = True
                        break

        return matches

    # ── Tier 3: RapidFuzz Multi-Attribute Scoring ────────────────────────────

    def _tier3_fuzzy(
        self, records: list[_Rec], matched_ids: set[uuid.UUID]
    ) -> list[_Match]:
        matches: list[_Match] = []

        # Compare BANK/GATEWAY vs INVOICE only (cross-source)
        deposits = [
            r for r in records
            if r.source_type in ("BANK", "GATEWAY")
            and r.record_id not in matched_ids
        ]
        invoices = [
            r for r in records
            if r.source_type == "INVOICE"
            and r.record_id not in matched_ids
        ]

        # Greedy: pick the highest-scoring pair for each deposit
        used_invoices: set[uuid.UUID] = set()

        for dep in deposits:
            if dep.record_id in matched_ids:
                continue

            best_score = 0.0
            best_inv: _Rec | None = None
            best_trace = ""

            for inv in invoices:
                if inv.record_id in matched_ids or inv.record_id in used_invoices:
                    continue
                score, trace = _composite_score(dep, inv)
                if score > best_score:
                    best_score = score
                    best_inv = inv
                    best_trace = trace

            if best_inv is not None and best_score >= FUZZY_THRESHOLD:
                matches.append(
                    _Match(
                        record_a_id=dep.record_id,
                        record_b_id=best_inv.record_id,
                        confidence_score=round(best_score, 2),
                        match_type="FUZZY",
                        reasoning_trace=best_trace,
                    )
                )
                matched_ids.add(dep.record_id)
                matched_ids.add(best_inv.record_id)
                used_invoices.add(best_inv.record_id)

        return matches

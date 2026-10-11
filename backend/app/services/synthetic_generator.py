"""
synthetic_generator.py
======================
Generates a realistic multi-source financial batch across three sources:

  • BANK    (20 records) – raw deposit logs, lump transfers, periodic fees
  • GATEWAY (10 records) – Razorpay settlement payouts (gross minus 2% fee, T+1 lag)
  • INVOICE (35 records) – ERP invoices with dirty names / typos, missing refs,
                           split-payment groupings

Intended match distribution stored in metadata_json.intended_match_type, aligned
to the 3-tier reconciler cascade:

  • EXACT        – 10 BANK + INVOICE pairs with identical ref + amount + date
                   (Tier 1 exact SQL match, confidence 1.00)
  • ONE_TO_MANY  –  5 GATEWAY net payouts each splitting into 3 INVOICE records
                   (Tier 2 subset-sum, deposit ≈ Σ invoices × (1 − fee))
  • FUZZY        –  5 BANK + INVOICE pairs with intentional name typos / missing
                   invoice ref (Tier 3 RapidFuzz composite score ≥ 0.75)
  • EXCEPTION    – 15 orphan/anomalous records that MUST NOT match
"""

import random
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from faker import Faker

fake = Faker("en_IN")
random.seed(42)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GATEWAY_FEE_RATE = Decimal("0.02")  # 2% Razorpay processing fee
SETTLEMENT_LAG = 1                  # T+1 settlement day

# Clean merchant names used as ground truth
MERCHANTS = [
    "Acme Tech Pvt Ltd",
    "BlueStar Logistics",
    "Zeta Finserv",
    "NovaPay Solutions",
    "Greenfield Exports",
    "Helix Digital",
    "Orbit Retail",
    "Prime Components",
    "Falcon Aero Industries",
    "Cascade Software",
]

# Dirty / typo variants of the same merchants (simulates ERP / bank feed noise)
DIRTY_VARIANTS: dict[str, list[str]] = {
    "Acme Tech Pvt Ltd": [
        "ACME TECH CORP",
        "Acme-Tech BLR",
        "RZP*ACME-TECH-BLR",
        "ACME TECHNOLOGIES LTD",
    ],
    "BlueStar Logistics": [
        "BLUE STAR LOGSTCS",
        "BlueStar Log.",
        "RZP*BLUESTAR-LOG",
        "BLUESTAR LOGISTICS PVT",
    ],
    "Zeta Finserv": [
        "ZETA FIN SERV",
        "ZetaFinancial",
        "RZP*ZETA-FINSERV",
        "ZETA FINANCIAL SERVICES",
    ],
    "NovaPay Solutions": [
        "NOVAPAY SOLN",
        "Nova Pay Ltd",
        "RZP*NOVAPAY-SOL",
        "NOVA PAY SOLUTIONS",
    ],
    "Greenfield Exports": [
        "GRN FIELD EXPORTS",
        "Greenfield Exp.",
        "RZP*GRNFLD-EXP",
        "GREENFIELD EXPORT CORP",
    ],
    "Helix Digital": [
        "HELIX DIG PVT",
        "Helix-Digital",
        "RZP*HELIX-DGTL",
        "HELIX DIGITAL INDIA",
    ],
    "Orbit Retail": [
        "ORBIT RETL IND",
        "Orbit Retails",
        "RZP*ORBIT-RETL",
        "ORBIT RETAIL INDIA",
    ],
    "Prime Components": [
        "PRIME COMP LTD",
        "PrimeComponents",
        "RZP*PRIME-COMP",
        "PRIME COMPONENT WORKS",
    ],
    "Falcon Aero Industries": [
        "FALCON AERO IND",
        "FalconAero",
        "RZP*FALCON-AERO",
        "FALCON AEROSPACE",
    ],
    "Cascade Software": [
        "CASCADE SOFTWARES",
        "CascadeSoft",
        "RZP*CASCADE-SW",
        "CASCADE SOFTWARE INDIA",
    ],
}

BASE_DATE = date(2024, 3, 1)


def _rand_date(offset_days: int = 0, jitter: int = 5) -> date:
    return BASE_DATE + timedelta(days=offset_days + random.randint(0, jitter))


def _round2(val: float) -> float:
    return round(val, 2)


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _make_bank_record(
    batch_id: uuid.UUID,
    merchant: str,
    amount: float,
    ref: str | None,
    txn_date: date,
    match_type: str,
    group_id: str | None = None,
    extra_meta: dict | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "intended_match_type": match_type,
        "source_system": "BANK_FEED",
    }
    if group_id:
        meta["group_id"] = group_id
    if extra_meta:
        meta.update(extra_meta)

    return {
        "record_id": uuid.uuid4(),
        "batch_id": batch_id,
        "source_type": "BANK",
        "raw_entity_name": merchant,
        "normalized_entity_name": None,  # filled by normalizer
        "amount": _round2(amount),
        "reference_code": ref,
        "transaction_date": txn_date,
        "status": "UNMATCHED",
        "metadata_json": meta,
    }


def _make_gateway_record(
    batch_id: uuid.UUID,
    merchant: str,
    gross_amount: float,
    ref: str,
    txn_date: date,
    match_type: str,
    group_id: str | None = None,
) -> dict[str, Any]:
    net = _round2(gross_amount * (1 - float(GATEWAY_FEE_RATE)))
    meta: dict[str, Any] = {
        "intended_match_type": match_type,
        "source_system": "RAZORPAY_SETTLEMENT",
        "gross_amount": gross_amount,
        "gateway_fee": _round2(gross_amount * float(GATEWAY_FEE_RATE)),
        "net_amount": net,
        "settlement_lag_days": SETTLEMENT_LAG,
    }
    if group_id:
        meta["group_id"] = group_id

    return {
        "record_id": uuid.uuid4(),
        "batch_id": batch_id,
        "source_type": "GATEWAY",
        "raw_entity_name": f"RZP*{merchant.upper().replace(' ', '-')[:20]}",
        "normalized_entity_name": None,
        "amount": net,
        "reference_code": ref,
        "transaction_date": txn_date + timedelta(days=SETTLEMENT_LAG),
        "status": "UNMATCHED",
        "metadata_json": meta,
    }


def _make_invoice_record(
    batch_id: uuid.UUID,
    merchant: str,
    amount: float,
    ref: str | None,
    txn_date: date,
    match_type: str,
    group_id: str | None = None,
    dirty: bool = False,
) -> dict[str, Any]:
    raw_name = (
        random.choice(DIRTY_VARIANTS.get(merchant, [merchant])) if dirty else merchant
    )
    meta: dict[str, Any] = {
        "intended_match_type": match_type,
        "source_system": "ERP_INVOICE",
        "clean_merchant": merchant,
    }
    if group_id:
        meta["group_id"] = group_id

    return {
        "record_id": uuid.uuid4(),
        "batch_id": batch_id,
        "source_type": "INVOICE",
        "raw_entity_name": raw_name,
        "normalized_entity_name": None,
        "amount": _round2(amount),
        "reference_code": ref,
        "transaction_date": txn_date,
        "status": "UNMATCHED",
        "metadata_json": meta,
    }


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_synthetic_batch(batch_id: uuid.UUID) -> list[dict[str, Any]]:
    """
    Returns a list of raw record dicts ready for ORM insertion.
    Total: 65 records (20 BANK + 10 GATEWAY + 35 INVOICE).
    """
    records: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # GROUP A: 10 EXACT match pairs  →  BANK (10) + INVOICE (10)
    # Each pair shares an identical reference, amount and date (±1 day),
    # so the Tier 1 exact SQL matcher resolves them at confidence 1.00.
    # ------------------------------------------------------------------
    for i in range(10):
        merchant = MERCHANTS[i % len(MERCHANTS)]
        amount = _round2(random.uniform(5_000, 80_000))
        ref = f"RCV-{1000 + i:04d}"
        txn_date = _rand_date(i * 2)
        group = f"exact-{i}"

        records.append(
            _make_bank_record(batch_id, merchant, amount, ref, txn_date, "EXACT", group)
        )
        records.append(
            _make_invoice_record(batch_id, merchant, amount, ref, txn_date, "EXACT", group)
        )

    # ------------------------------------------------------------------
    # GROUP B: 5 ONE-TO-MANY  →  GATEWAY payout (5) + INVOICE splits (15)
    # Each gateway net payout = Σ 3 invoices × (1 − 2% fee), so the Tier 2
    # subset-sum matcher resolves the lump against its invoice cluster.
    # ------------------------------------------------------------------
    for i in range(5):
        merchant = MERCHANTS[(5 + i) % len(MERCHANTS)]
        group = f"otm-{i}"
        invoice_amounts = [
            _round2(random.uniform(3_000, 15_000)) for _ in range(3)
        ]
        gross_total = _round2(sum(invoice_amounts))
        net_lump = _round2(gross_total * (1 - float(GATEWAY_FEE_RATE)))
        lump_date = _rand_date(20 + i * 3)
        ref = f"LUMP-{2000 + i:04d}"

        # Gateway settlement: net of the 2% processing fee
        gw_rec = _make_gateway_record(
            batch_id, merchant, gross_total, ref, lump_date, "ONE_TO_MANY", group
        )
        gw_rec["amount"] = net_lump      # pin the exact net payout
        records.append(gw_rec)

        # Three invoices forming the gross total, dated within ±3 days
        for j, inv_amount in enumerate(invoice_amounts):
            inv_ref = f"INV-{3000 + i * 10 + j:04d}" if j % 2 == 0 else None
            inv_rec = _make_invoice_record(
                batch_id,
                merchant,
                inv_amount,
                inv_ref,
                lump_date - timedelta(days=random.randint(0, 2)),
                "ONE_TO_MANY",
                group,
                dirty=True,
            )
            records.append(inv_rec)

    # ------------------------------------------------------------------
    # GROUP C: 5 FUZZY match pairs  →  BANK (5) + INVOICE (5) dirty names
    # Same amount, close date, but merchant name has typos
    # ------------------------------------------------------------------
    for i in range(5):
        merchant = MERCHANTS[i % len(MERCHANTS)]
        amount = _round2(random.uniform(2_000, 30_000))
        ref = f"FUZ-{4000 + i:04d}"
        txn_date = _rand_date(35 + i * 2)
        group = f"fuzzy-{i}"

        bank_rec = _make_bank_record(
            batch_id, merchant, amount, ref, txn_date, "FUZZY", group
        )
        records.append(bank_rec)

        # Invoice with dirty name and missing ref
        inv_rec = _make_invoice_record(
            batch_id,
            merchant,
            amount,
            None,  # intentionally missing ref
            txn_date + timedelta(days=random.randint(0, 2)),
            "FUZZY",
            group,
            dirty=True,
        )
        records.append(inv_rec)

    # ------------------------------------------------------------------
    # GROUP D: 15 EXCEPTION records (no intended match)
    # Mix of BANK (5), GATEWAY (5), INVOICE (5)
    # Anomaly types: missing ref, amount > ₹25K aging > 7 days, fee discrepancy,
    #                duplicate ref, rounding error, orphan invoice
    # ------------------------------------------------------------------
    exception_scenarios = [
        # BANK exceptions
        {
            "source": "BANK",
            "merchant": "Unknown Vendor BLR",
            "amount": 52_300.00,
            "ref": None,                  # missing ref
            "anomaly": "missing_reference",
            "risk": "HIGH",
        },
        {
            "source": "BANK",
            "merchant": "SUSPENSE ACCT DR",
            "amount": 1_250.75,
            "ref": "TXN-9999",
            "anomaly": "suspense_debit",
            "risk": "MEDIUM",
        },
        {
            "source": "BANK",
            "merchant": "Bank Service Fee",
            "amount": 118.00,
            "ref": "FEE-2024-03",
            "anomaly": "periodic_bank_fee",
            "risk": "LOW",
        },
        {
            "source": "BANK",
            "merchant": "NEFT RETURN - ACME",
            "amount": 45_000.00,
            "ref": "NEFT-RTN-001",
            "anomaly": "returned_payment_high_value",
            "risk": "HIGH",
        },
        {
            "source": "BANK",
            "merchant": "Duplicate TXN - NovaPay",
            "amount": 8_750.00,
            "ref": "TXN-1000",      # duplicate of an EXACT match ref
            "anomaly": "duplicate_reference",
            "risk": "HIGH",
        },
        # GATEWAY exceptions
        {
            "source": "GATEWAY",
            "merchant": "RZP*UNKNOWN-MERCH",
            "amount": 3_499.50,
            "ref": "RZP-EXC-001",
            "anomaly": "unregistered_merchant",
            "risk": "HIGH",
        },
        {
            "source": "GATEWAY",
            "merchant": "RZP*STALE-SETTL",
            "amount": 12_000.00,
            "ref": "RZP-EXC-002",
            "anomaly": "stale_settlement_over_7d",
            "risk": "HIGH",
        },
        {
            "source": "GATEWAY",
            "merchant": "RZP*FEE-DISCREPANCY",
            "amount": 9_800.00,      # should be 9_800 after 2% but gross was wrong
            "ref": "RZP-EXC-003",
            "anomaly": "fee_discrepancy_gt_2pct",
            "risk": "MEDIUM",
        },
        {
            "source": "GATEWAY",
            "merchant": "RZP*PARTIAL-REFUND",
            "amount": -500.00,       # negative: partial refund
            "ref": "RZP-EXC-004",
            "anomaly": "partial_refund",
            "risk": "MEDIUM",
        },
        {
            "source": "GATEWAY",
            "merchant": "RZP*CHARGEBACK",
            "amount": -28_000.00,    # chargeback
            "ref": "RZP-EXC-005",
            "anomaly": "chargeback_high_value",
            "risk": "HIGH",
        },
        # INVOICE exceptions
        {
            "source": "INVOICE",
            "merchant": "GHOST VENDOR PVT",
            "amount": 6_500.00,
            "ref": "INV-9001",
            "anomaly": "orphan_invoice_no_payment",
            "risk": "MEDIUM",
        },
        {
            "source": "INVOICE",
            "merchant": "Acme Tech Pvt Ltd",   # known merchant, wrong amount
            "amount": 99_999.99,
            "ref": "INV-9002",
            "anomaly": "suspiciously_large_invoice",
            "risk": "HIGH",
        },
        {
            "source": "INVOICE",
            "merchant": "Helix Digital",
            "amount": 0.01,            # rounding error invoice
            "ref": "INV-9003",
            "anomaly": "rounding_discrepancy",
            "risk": "LOW",
        },
        {
            "source": "INVOICE",
            "merchant": "MISSING REF CORP",
            "amount": 7_800.00,
            "ref": None,              # missing reference
            "anomaly": "missing_ref_invoice",
            "risk": "MEDIUM",
        },
        {
            "source": "INVOICE",
            "merchant": "Split Ghost Ltd",
            "amount": 14_200.00,
            "ref": "INV-9005",
            "anomaly": "unmatched_split_payment",
            "risk": "MEDIUM",
        },
    ]

    for idx, scenario in enumerate(exception_scenarios):
        offset = 45 + idx * 2
        txn_date = _rand_date(offset)
        # Stale settlement: force date > 7 days ago
        if scenario["anomaly"] == "stale_settlement_over_7d":
            txn_date = BASE_DATE + timedelta(days=-(random.randint(8, 20)))

        meta: dict[str, Any] = {
            "intended_match_type": "EXCEPTION",
            "anomaly_type": scenario["anomaly"],
            "risk_hint": scenario["risk"],
        }
        if scenario["source"] == "GATEWAY":
            meta["source_system"] = "RAZORPAY_SETTLEMENT"
        elif scenario["source"] == "INVOICE":
            meta["source_system"] = "ERP_INVOICE"
        else:
            meta["source_system"] = "BANK_FEED"

        records.append(
            {
                "record_id": uuid.uuid4(),
                "batch_id": batch_id,
                "source_type": scenario["source"],
                "raw_entity_name": scenario["merchant"],
                "normalized_entity_name": None,
                "amount": _round2(float(scenario["amount"])),
                "reference_code": scenario["ref"],
                "transaction_date": txn_date,
                "status": "UNMATCHED",
                "metadata_json": meta,
            }
        )

    # Shuffle so records aren't ordered by match type
    random.shuffle(records)
    return records


def get_batch_stats(records: list[dict]) -> dict[str, int]:
    """Quick count breakdown for the API response."""
    return {
        "total": len(records),
        "bank": sum(1 for r in records if r["source_type"] == "BANK"),
        "gateway": sum(1 for r in records if r["source_type"] == "GATEWAY"),
        "invoice": sum(1 for r in records if r["source_type"] == "INVOICE"),
        "exact": sum(
            1
            for r in records
            if r["metadata_json"].get("intended_match_type") == "EXACT"
        ),
        "one_to_many": sum(
            1
            for r in records
            if r["metadata_json"].get("intended_match_type") == "ONE_TO_MANY"
        ),
        "fuzzy": sum(
            1
            for r in records
            if r["metadata_json"].get("intended_match_type") == "FUZZY"
        ),
        "exception": sum(
            1
            for r in records
            if r["metadata_json"].get("intended_match_type") == "EXCEPTION"
        ),
    }

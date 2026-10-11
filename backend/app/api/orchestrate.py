"""
api/orchestrate.py
==================
Autonomous agentic pipeline orchestration.

  POST /api/agent/orchestrate/{batch_id}
      Walks a batch through the full agent stack in one call:
        Stage 1  RECONCILE   → 3-tier matching cascade + risk triage (SYSTEM_RECONCILER)
        Stage 2  DIAGNOSE    → kimi-k2 diagnostic agent on every PENDING exception (DIAGNOSTIC_AGENT)
        Stage 3  FORECAST    → 30-day cash runway + liquidity posture (FORECASTER)

Each stage runs in the SAME transaction as any handler would, emits immutable
AuditLog rows, and records a NeatLogs pipeline trace.
"""

import time
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.exceptions import diagnose_exceptions
from app.api.reconciliation import run_reconciliation
from app.db.models import AuditLog, ReconciliationBatch
from app.db.session import get_db
from app.neatlogs import neatlogs
from app.schemas.reconciliation import ReconciliationRunOut
from app.services import cash_forecaster
from app.services.audit_logger import AuditLogger

router = APIRouter(prefix="/api/agent", tags=["Agent Orchestration"])


class OrchestrationStage(BaseModel):
    name: str
    status: str            # 'SUCCESS' | 'SKIPPED' | 'FAILED'
    detail: str
    duration_ms: float = 0.0
    metrics: dict[str, Any] = {}


class OrchestrationOut(BaseModel):
    batch_id: uuid.UUID
    status: str            # 'SUCCESS' | 'PARTIAL' | 'FAILED'
    stages: list[OrchestrationStage]
    overall_duration_ms: float
    match_rate: float = 0.0
    risk_level: str = "HEALTHY"
    diagnosis_count: int = 0
    audit_entries: int = 0


@router.post(
    "/orchestrate/{batch_id}",
    response_model=OrchestrationOut,
    summary="Run the full autonomous agent pipeline on a batch",
    description=(
        "Stage 1 reconciles via the 3-tier cascade + risk triage, stage 2 runs "
        "the kimi diagnostic agent over PENDING exceptions, stage 3 produces the "
        "30-day cash runway. All writes ride one DB transaction and land in the "
        "immutable audit trail and NeatLogs."
    ),
)
async def orchestrate(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> OrchestrationOut:
    started = time.perf_counter()
    stages: list[OrchestrationStage] = []

    # ── Validate batch ───────────────────────────────────────────────────────
    check = await db.execute(
        select(ReconciliationBatch).where(ReconciliationBatch.batch_id == batch_id)
    )
    if check.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Batch {batch_id} not found.",
        )

    neatlogs.info(
        "Orchestrator",
        f"Autonomous pipeline started for batch {batch_id}.",
        {"batch_id": str(batch_id)},
    )

    overall_status = "SUCCESS"
    match_rate = 0.0
    risk_level = "HEALTHY"
    diagnosis_count = 0

    # ── Stage 1: Reconcile ───────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        recon: ReconciliationRunOut = await run_reconciliation(batch_id, db)
        stages.append(
            OrchestrationStage(
                name="RECONCILE",
                status="SUCCESS",
                detail=f"matched={recon.matched_count} exception={recon.exception_count}",
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                metrics={
                    "matched_count": recon.matched_count,
                    "exception_count": recon.exception_count,
                    "match_rate": recon.match_rate,
                    "tier1": recon.tier1_exact,
                    "tier2": recon.tier2_one_to_many,
                    "tier3": recon.tier3_fuzzy,
                },
            )
        )
        match_rate = recon.match_rate
    except Exception as exc:  # surface failures honestly
        stages.append(
            OrchestrationStage(
                name="RECONCILE",
                status="FAILED",
                detail=str(exc),
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        )
        overall_status = "FAILED"

    # ── Stage 2: Diagnose exceptions ─────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        diag = await diagnose_exceptions(batch_id, db)
        diagnosis_count = diag.diagnosed_count
        stages.append(
            OrchestrationStage(
                name="DIAGNOSE",
                status="SUCCESS",
                detail=f"diagnosed={diag.diagnosed_count} exceptions",
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                metrics={"diagnosed_count": diag.diagnosed_count},
            )
        )
    except Exception as exc:
        stages.append(
            OrchestrationStage(
                name="DIAGNOSE",
                status="FAILED",
                detail=str(exc),
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        )
        overall_status = "PARTIAL" if overall_status != "FAILED" else "FAILED"

    # ── Stage 3: Forecast ────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        runway = await cash_forecaster.compute_runway(db, batch_id, date.today())
        risk_level = runway.liquidity_risk.risk_level
        stages.append(
            OrchestrationStage(
                name="FORECAST",
                status="SUCCESS",
                detail=(
                    f"C0=₹{runway.baseline_cash:,.2f} "
                    f"days_to_zero={runway.liquidity_risk.days_to_zero or 'never'} "
                    f"({risk_level})"
                ),
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                metrics={
                    "baseline_cash": round(runway.baseline_cash, 2),
                    "days_to_zero": runway.liquidity_risk.days_to_zero,
                    "risk_level": risk_level,
                },
            )
        )
    except Exception as exc:
        stages.append(
            OrchestrationStage(
                name="FORECAST",
                status="FAILED",
                detail=str(exc),
                duration_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
        )
        overall_status = "PARTIAL" if overall_status != "FAILED" else "FAILED"

    # ── Audit + NeatLogs trace ───────────────────────────────────────────────
    try:
        await AuditLogger.log_event(
            db=db,
            batch_id=batch_id,
            action_type="PIPELINE_RUN",
            actor="AGENT_ORCHESTRATOR",
            reasoning_snapshot=(
                f"Autonomous pipeline: {sum(1 for s in stages if s.status == 'SUCCESS')}/{len(stages)} "
                f"stages succeeded. match_rate={match_rate}% risk={risk_level}."
            ),
            changes_payload={
                "stages": [s.name for s in stages],
                "statuses": [s.status for s in stages],
                "overall": overall_status,
            },
        )
    except Exception:
        pass

    overall_ms = round((time.perf_counter() - started) * 1000, 2)

    # Audit slice size: total rows for the batch including this pipeline run.
    # Same transaction → autoflush surfaces the rows written above.
    audit_count_result = await db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.batch_id == batch_id)
    )
    audit_entries = audit_count_result.scalar_one()

    neatlogs.success(
        "Orchestrator",
        f"Autonomous pipeline for batch {batch_id} → {overall_status} "
        f"({sum(1 for s in stages if s.status == 'SUCCESS')}/{len(stages)} stages, "
        f"{overall_ms}ms).",
        {"batch_id": str(batch_id), "status": overall_status, "duration_ms": overall_ms},
    )

    return OrchestrationOut(
        batch_id=batch_id,
        status=overall_status,
        stages=stages,
        overall_duration_ms=overall_ms,
        match_rate=match_rate,
        risk_level=risk_level,
        diagnosis_count=diagnosis_count,
        audit_entries=audit_entries,
    )
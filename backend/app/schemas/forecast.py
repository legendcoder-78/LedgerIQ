"""
schemas/forecast.py
====================
Pydantic v2 schemas for cash forecasting and what-if simulation endpoints.
"""

import uuid
from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Daily forecast data point
# ---------------------------------------------------------------------------
class ForecastDataPoint(BaseModel):
    date: date
    projected_balance: float
    settled_cash: float        # confirmed inflows from MATCHED records
    at_risk_cash: float        # risk-weighted pending receivables


# ---------------------------------------------------------------------------
# GET /api/forecast/runway/{batch_id}
# ---------------------------------------------------------------------------
class LiquidityRiskMetric(BaseModel):
    days_to_zero: Optional[int]     # None if balance stays positive for 30d
    minimum_balance: float
    minimum_balance_date: Optional[date]
    total_at_risk: float
    total_settled: float
    high_risk_exposure: float
    medium_risk_exposure: float
    low_risk_exposure: float

    # UI-facing convenience fields
    min_projected_balance: float = 0.0          # alias of minimum_balance
    risk_level: str = "HEALTHY"                 # HEALTHY | CAUTION | CRITICAL


class RunwayOut(BaseModel):
    batch_id: uuid.UUID
    baseline_cash: float           # C0: starting verified bank position
    forecast_horizon_days: int
    projection: list[ForecastDataPoint]
    liquidity_risk: LiquidityRiskMetric

    # UI-facing convenience alias for `projection`
    daily_projection: list[ForecastDataPoint] = []


# ---------------------------------------------------------------------------
# POST /api/forecast/simulate
# ---------------------------------------------------------------------------
class SimulationRequest(BaseModel):
    batch_id: uuid.UUID
    delay_days: int = Field(default=0, ge=0, le=90, description="Shift pending collection dates forward by N days")
    default_rate: float = Field(default=0.0, ge=0.0, le=1.0, description="Reduce pending receivable values by (1 - default_rate)")
    gateway_fee_surge: float = Field(default=0.0, ge=0.0, le=0.5, description="Additional fee rate applied to gateway settlements")

    @field_validator("default_rate", "gateway_fee_surge", mode="before")
    @classmethod
    def round_rate(cls, v: float) -> float:
        return round(float(v), 4)


class RunwayDelta(BaseModel):
    day_index: int
    date: date
    baseline_balance: float
    simulated_balance: float
    delta: float
    delta_pct: float


class SimulationDeltaSummary(BaseModel):
    """Headline impact of a what-if scenario (UI-facing)."""
    net_cash_variance: float = 0.0      # signed worst/bottom-line delta
    min_cash: float = 0.0               # lowest simulated balance
    days_to_zero: Optional[int] = None  # first stressed day ≤ 0, else None
    summary: str = ""


class SimulationOut(BaseModel):
    batch_id: uuid.UUID
    parameters: SimulationRequest
    baseline: list[ForecastDataPoint]
    simulated: list[ForecastDataPoint]
    deltas: list[RunwayDelta]
    summary: str    # e.g. "Cash position reduced by ₹45,000 at Day 15"
    max_negative_impact_day: Optional[int]
    max_negative_impact_amount: float

    # UI-facing convenience shapes
    time_series: list[ForecastDataPoint] = []          # alias of `simulated`
    delta: SimulationDeltaSummary = SimulationDeltaSummary()
    simulated_summary: SimulationDeltaSummary = SimulationDeltaSummary()

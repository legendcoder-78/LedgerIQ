"""
app/main.py
===========
FastAPI application entry point for LedgerIQ | CFO.AI backend.
"""

from contextlib import asynccontextmanager
import sys
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.ingestion import router as ingestion_router
from app.api.reconciliation import router as reconciliation_router
from app.api.copilot import router as copilot_router
from app.api.exceptions import router as exceptions_router
from app.api.forecasting import router as forecasting_router
from app.api.audit import router as audit_router
from app.api.neatlogs import router as neatlogs_router
from app.api.orchestrate import router as orchestrate_router
from app.neatlogs import neatlogs

# Windows: ProactorEventLoop (default in 3.8+) is incompatible with
# asyncpg's SSL transports when the loop closes. Prefer Selector for the
# runtime so DB teardown is clean (spawns clean in reload child processes too).
if sys.platform == "win32":
    import asyncio

    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan — startup and shutdown handlers."""
    neatlogs.info("System", "🚀 LedgerIQ | CFO.AI backend starting up...")
    yield
    neatlogs.info("System", "🛑 LedgerIQ | CFO.AI backend shutting down...")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="LedgerIQ | CFO.AI — Autonomous Finance Controller",
    description=(
        "Razorpay Buildathon Track 04 — Autonomous multi-source reconciliation, "
        "exception triaging, cash forecasting, and NeatLogs telemetry engine."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# NeatLogs Request Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def neatlogs_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
    
    if not request.url.path.startswith("/api/neatlogs"):
        neatlogs.info(
            "HTTP",
            f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms}ms)",
            {"status_code": response.status_code, "duration_ms": duration_ms}
        )
    return response


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(ingestion_router)
app.include_router(reconciliation_router)
app.include_router(copilot_router)
app.include_router(exceptions_router)
app.include_router(forecasting_router)
app.include_router(audit_router)
app.include_router(neatlogs_router)
app.include_router(orchestrate_router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Health"], summary="Liveness + readiness probe")
async def health_check() -> dict:
    from app.db.session import AsyncSessionLocal
    from app.config import settings

    t0 = time.perf_counter()
    db_status = "unknown"
    db_error = None
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as exc:
        db_status = "error"
        db_error = str(exc)

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    llm_configured = bool(settings.KIMI_API_KEY)

    return {
        "status": "ok" if db_status == "connected" else "degraded",
        "service": "ledgeriq-cfo-ai",
        "brand": "CFO.AI",
        "logging_engine": "NeatLogs",
        "db": db_status,
        "db_error": db_error,
        "llm_configured": llm_configured,
        "llm_model": settings.KIMI_MODEL if llm_configured else None,
        "db_latency_ms": elapsed_ms,
    }

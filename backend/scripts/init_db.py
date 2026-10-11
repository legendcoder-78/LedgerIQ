"""
scripts/init_db.py
==================
One-time database initialisation script for LedgerIQ | CFO.AI.
"""

import asyncio
import sys
from pathlib import Path

# Make sure `backend/` is on sys.path when running as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db.models import Base
from app.db.session import _sanitize_database_url
from app.neatlogs import neatlogs


async def init_db() -> None:
    db_url = _sanitize_database_url(settings.DATABASE_URL)
    neatlogs.info("Database", f"Connecting to: {db_url[:60]}...")

    try:
        engine = create_async_engine(db_url, echo=False, pool_pre_ping=True)
        async with engine.begin() as conn:
            # Check dialect
            if engine.dialect.name == "postgresql":
                neatlogs.info("Database", "Creating PostgreSQL extensions: uuid-ossp, pg_trgm...")
                try:
                    await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
                except Exception as ext_err:
                    neatlogs.warn("Database", f"Extension creation warning (safe to ignore if non-superuser): {ext_err}")

            neatlogs.info("Database", "Creating all tables from SQLAlchemy models...")
            await conn.run_sync(Base.metadata.create_all)

        await engine.dispose()
        neatlogs.success("Database", "Database initialized successfully!")

    except Exception as exc:
        neatlogs.warn("Database", f"Primary database connection failed ({exc}). Initializing local SQLite database...")
        sqlite_url = "sqlite+aiosqlite:///./ledgeriq.db"
        engine = create_async_engine(sqlite_url, echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
        neatlogs.success("Database", "Local SQLite database initialized successfully at ledgeriq.db!")


if __name__ == "__main__":
    asyncio.run(init_db())

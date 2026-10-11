"""
run.py
======
Uvicorn startup entry point for the AI Finance Controller backend.

Usage:
    python run.py
    # or
    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

import sys

import uvicorn

if sys.platform == "win32":
    import asyncio

    # Selector loop avoids asyncpg SSL teardown noise (Proactor + SSL transports)
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except ValueError:
        pass

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )

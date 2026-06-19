"""FastAPI app wrapping the Solver service.

Run with:
    uvicorn solver.app:app --host 0.0.0.0 --port 7311 --workers 1

(workers=1 because the SolveStats and locks are per-process state. For real
multi-worker production, swap AsyncTTLCache for Redis and KeyedLocks for
Redis distributed locks.)

Auth:
    Set SOLVER_API_KEY in the environment. Then /solve and /stats require
    header X-Solver-Key: <key>. /health stays open for uptime monitoring.
    If SOLVER_API_KEY is empty/unset, the gate is disabled (open mode) and
    a WARNING is logged at startup — useful for tests, not for production.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .service import Solver


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sn101.app")

_solver: Solver | None = None
_REQUIRED_KEY = os.environ.get("SOLVER_API_KEY", "").strip()


async def require_solver_key(x_solver_key: str = Header(default="")) -> None:
    """Reject requests without the configured X-Solver-Key header."""
    if not _REQUIRED_KEY:
        return  # Open mode — gate disabled
    if x_solver_key != _REQUIRED_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-Solver-Key")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _solver
    if _REQUIRED_KEY:
        logger.info("auth gate ENABLED (X-Solver-Key required for /solve and /stats)")
    else:
        logger.warning(
            "auth gate DISABLED — SOLVER_API_KEY env var not set. "
            "This is acceptable for tests but NOT for production."
        )
    logger.info("loading solver…")
    _solver = Solver()
    logger.info("solver ready (vocab=%d)", len(_solver.vocab))
    try:
        yield
    finally:
        if _solver is not None:
            await _solver.aclose()


app = FastAPI(title="SN101 Solver", lifespan=lifespan)


class SolveRequest(BaseModel):
    tweet: str = Field(min_length=1, max_length=10_000)


class SolveResponse(BaseModel):
    tags: list[str]
    path: str
    latency_ms: float


@app.post(
    "/solve",
    response_model=SolveResponse,
    dependencies=[Depends(require_solver_key)],
)
async def solve(req: SolveRequest) -> SolveResponse:
    if _solver is None:
        raise HTTPException(503, "solver not ready")
    result = await _solver.solve(req.tweet)
    return SolveResponse(
        tags=result.tags,
        path=result.path,
        latency_ms=result.latency_ms,
    )


@app.get("/health")
async def health() -> dict:
    """Health stays open — no key required, so uptime monitors and OVH probes can hit it."""
    return {"ok": _solver is not None}


@app.get(
    "/stats",
    dependencies=[Depends(require_solver_key)],
)
async def stats() -> dict:
    if _solver is None:
        return {"ready": False}
    return {"ready": True, "stats": _solver.stats.snapshot()}

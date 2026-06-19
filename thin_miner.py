"""Thin miner — replaces the LLM-calling reference miner with a solver client.

Drop-in replacement for tag101.tasks.sn101.solve_problem. To wire it into
the SN101 miner, set the env var:

    TASK_MINER_MODULE=thin_miner

The miner will import handler() from this module instead of the OpenAI-backed
sn101 reference handler.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

import httpx


logger = logging.getLogger("sn101.thin_miner")


SOLVER_URL = os.environ.get("SN101_SOLVER_URL", "http://127.0.0.1:7311")
SOLVER_TIMEOUT_S = float(os.environ.get("SN101_SOLVER_TIMEOUT_S", "5.0"))
SOLVER_API_KEY = os.environ.get("SN101_SOLVER_API_KEY", "").strip()
SAFE_DEFAULT_TAGS = ["ai", "tech", "release"]


class SolverClient:
    """Synchronous HTTP client for the central solver.

    The Tag101 miner's forward() is async but calls solve_problem()
    synchronously, so the client must NOT spin up its own event loop —
    blocking HTTP is the right shape.

    Sends X-Solver-Key header when SN101_SOLVER_API_KEY env var is set so
    the solver's auth gate accepts the request.
    """

    def __init__(
        self,
        base_url: str = SOLVER_URL,
        timeout: float = SOLVER_TIMEOUT_S,
        api_key: str = SOLVER_API_KEY,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            headers: dict[str, str] = {}
            if self.api_key:
                headers["X-Solver-Key"] = self.api_key
            self._client = httpx.Client(timeout=self.timeout, headers=headers)
        return self._client

    def solve(self, tweet: str) -> list[str]:
        client = self._get_client()
        r = client.post(f"{self.base_url}/solve", json={"tweet": tweet})
        r.raise_for_status()
        body = r.json()
        tags = body.get("tags", [])
        if not isinstance(tags, list):
            return []
        return [str(t) for t in tags if isinstance(t, str)]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# Module-level client so we don't reconnect per request.
_client = SolverClient()


def solve_problem(envelope: Any, _runtime: Any) -> dict[str, Any]:
    """Drop-in replacement for tag101.tasks.sn101.solve_problem.

    The reference signature is (envelope, chain_runtime) -> dict.
    We ignore chain_runtime — all work delegates to the central solver.
    """
    payload = dict(getattr(envelope, "payload", {}) or {})
    tweet = str(payload.get("text", ""))
    if not tweet:
        return {"tags": list(SAFE_DEFAULT_TAGS)}

    try:
        tags = _client.solve(tweet)
    except Exception as exc:
        logger.warning("solver call failed: %s", exc)
        tags = list(SAFE_DEFAULT_TAGS)

    if not tags:
        tags = list(SAFE_DEFAULT_TAGS)
    return {"tags": tags[:3]}


def score_answers(
    payload: Mapping[str, Any],
    scoring: Mapping[str, Any],
    answers: Sequence[Mapping[str, Any]],
):
    """Re-export the real SN101 scorer so the miner-side registry still works."""
    from tag101.tasks.sn101 import score_answers as _real

    return _real(payload, scoring, answers)


def handler():
    """Match the TaskHandler contract expected by the registry."""
    from tag101.tasks.framework import TaskHandler

    return TaskHandler(
        kind="sn101.tags.v1",
        spec_version="v1",
        solve_problem=solve_problem,
        score_answers=score_answers,
        description="Thin client that delegates tag generation to a central solver.",
    )

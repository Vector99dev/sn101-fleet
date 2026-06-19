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

    def solve(self, tweet: str) -> dict[str, Any]:
        """Return {"tags": [...], "path": "...", "latency_ms": ...} from the solver."""
        client = self._get_client()
        r = client.post(f"{self.base_url}/solve", json={"tweet": tweet})
        r.raise_for_status()
        body = r.json()
        raw_tags = body.get("tags", [])
        tags = [str(t) for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []
        return {
            "tags": tags,
            "path": str(body.get("path", "?")),
            "latency_ms": float(body.get("latency_ms", 0.0) or 0.0),
        }

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# Module-level client so we don't reconnect per request.
_client = SolverClient()


def _log_response(
    *,
    task_id: str,
    tweet: str,
    tags: list[str],
    path: str,
    solver_ms: float,
) -> None:
    """Emit a human-readable line to the miner's logs with the actual tags.

    Tag101's miner only logs `answer_keys=['tags']`, which doesn't show what
    we returned. This adds a parallel `THIN_MINER_RESPONSE` line so the
    operator can see the real values in `pm2 logs`.
    """
    tweet_snippet = (tweet[:80] + "…") if len(tweet) > 80 else tweet
    line = (
        f"THIN_MINER_RESPONSE task={task_id} "
        f"path={path} solver_ms={solver_ms:.0f} "
        f"tags={tags} tweet={tweet_snippet!r}"
    )
    try:
        import bittensor as bt  # imported here so test contexts without bt still work

        bt.logging.info(line)
    except Exception:
        # Fallback path (e.g. running thin_miner tests without bittensor installed).
        # Print goes to stdout, which pm2 captures.
        print(line, flush=True)


def solve_problem(envelope: Any, _runtime: Any) -> dict[str, Any]:
    """Drop-in replacement for tag101.tasks.sn101.solve_problem.

    The reference signature is (envelope, chain_runtime) -> dict.
    We ignore chain_runtime — all work delegates to the central solver.
    """
    payload = dict(getattr(envelope, "payload", {}) or {})
    tweet = str(payload.get("text", ""))
    task_id = str(getattr(envelope, "task_id", "?"))

    if not tweet:
        result = list(SAFE_DEFAULT_TAGS)
        _log_response(task_id=task_id, tweet="", tags=result,
                      path="no_tweet", solver_ms=0.0)
        return {"tags": result}

    path = "?"
    solver_ms = 0.0
    try:
        body = _client.solve(tweet)
        tags = list(body["tags"])
        path = body["path"]
        solver_ms = body["latency_ms"]
    except Exception as exc:
        logger.warning("solver call failed: %s", exc)
        tags = list(SAFE_DEFAULT_TAGS)
        path = "client_error"

    if not tags:
        tags = list(SAFE_DEFAULT_TAGS)
        path = f"{path}+empty_fallback"

    final_tags = tags[:3]
    _log_response(task_id=task_id, tweet=tweet, tags=final_tags,
                  path=path, solver_ms=solver_ms)
    return {"tags": final_tags}


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

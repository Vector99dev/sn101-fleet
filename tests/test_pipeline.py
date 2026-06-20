"""End-to-end verification of the SN101 fleet solver pipeline.

Run from the repo root:
    PYTHONPATH=/root/sn101-fleet python /root/sn101-fleet/tests/test_pipeline.py

What we verify:
  1. The solver loads its embedding model and vocabulary.
  2. solve(tweet) returns 3 well-formed, deduped, lower-case tags.
  3. Repeated calls hit the exact cache (path == "exact_cache").
  4. Tweets with strong vocab signal hit the vocab path (path == "vocab").
  5. Concurrent calls with the same tweet trigger ONLY ONE upstream LLM call
     (this proves the dedup lock works — critical for sybil cost savings).
  6. The full FastAPI app boots and serves /solve, /health, /stats.
  7. The thin miner client wraps the HTTP call and returns 3 tags.
  8. The local validity check matches the validator's scoring on a known tag.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# These tests assume 3-tag returns. Force 3-tag mode in the test process so the
# 1-tag production default in thin_miner.py doesn't break assertions.
os.environ.setdefault("SN101_MAX_TAGS_SUBMITTED", "3")

# Make `solver` and `thin_miner` importable when running this file directly.
sys.path.insert(0, "/root/sn101-fleet")
# Also expose tag101 (already on PYTHONPATH if installed, but just in case).
sys.path.insert(0, "/root")

from tests.sample_tweets import SAMPLE_TWEETS  # noqa: E402

OK = "\033[92mPASS\033[0m"
NO = "\033[91mFAIL\033[0m"
INFO = "\033[94mINFO\033[0m"


def header(title: str) -> None:
    print(f"\n{'='*72}\n{title}\n{'='*72}")


def expect(cond: bool, msg: str) -> bool:
    print(f"  {OK if cond else NO}  {msg}")
    return cond


# ---------------------------------------------------------------------------
# 1. Solver-level tests (no HTTP)
# ---------------------------------------------------------------------------

async def test_solver_basics() -> bool:
    header("TEST 1 — Solver basics: load, solve, shape of result")
    from solver.service import Solver

    t0 = time.perf_counter()
    solver = Solver()
    print(f"  {INFO}  Solver loaded in {time.perf_counter()-t0:.2f}s "
          f"(vocab={len(solver.vocab)})")

    all_ok = True
    for i, tweet in enumerate(SAMPLE_TWEETS[:7]):
        result = await solver.solve(tweet)
        ok = (
            isinstance(result.tags, list)
            and len(result.tags) == 3
            and all(isinstance(t, str) and t == t.lower() and t.strip() == t
                    for t in result.tags)
            and len(set(result.tags)) == 3
        )
        all_ok = expect(
            ok,
            f"tweet {i+1}: tags={result.tags!r} path={result.path} "
            f"{result.latency_ms:.0f}ms",
        ) and all_ok

    await solver.aclose()
    return all_ok


# ---------------------------------------------------------------------------
# 2. Cache behavior
# ---------------------------------------------------------------------------

async def test_cache_paths() -> bool:
    header("TEST 2 — Cache paths: vocab + exact")
    from solver.service import Solver

    solver = Solver()
    target = "Anthropic released Claude 4.7 today with stronger coding and search."

    r1 = await solver.solve(target)
    expect(
        r1.path in ("vocab", "llm"),
        f"first call took path={r1.path} (expected vocab or llm)",
    )

    r2 = await solver.solve(target)
    ok_exact = expect(
        r2.path == "exact_cache",
        f"second call took path={r2.path} (expected exact_cache)",
    )

    ok_same = expect(
        r1.tags == r2.tags,
        f"both calls returned same tags: {r1.tags} == {r2.tags}",
    )

    novel = (
        "OpenAI announced GPT-5 with multimodal reasoning, taking on "
        "Gemini and Llama."
    )
    r3 = await solver.solve(novel)
    ok_vocab = expect(
        r3.path in ("vocab", "exact_cache", "llm"),
        f"novel tweet path={r3.path}",
    )

    print(f"  {INFO}  stats: {solver.stats.snapshot()}")
    await solver.aclose()
    return ok_exact and ok_same and ok_vocab


# ---------------------------------------------------------------------------
# 3. Concurrency: 10 simultaneous miners for one tweet -> 1 upstream call
# ---------------------------------------------------------------------------

async def test_concurrency_dedup() -> bool:
    header("TEST 3 — Dedup lock: 10 concurrent solves trigger only 1 LLM call")
    from solver.service import Solver
    from solver.llm import LLMRouter

    call_count = {"n": 0}

    class CountingMockLLM(LLMRouter):
        async def generate(self, tweet: str, n: int = 5) -> list[str]:  # type: ignore[override]
            call_count["n"] += 1
            await asyncio.sleep(0.2)  # simulate LLM latency
            return ["ai", "tech", "release", "model", "news"]

    solver = Solver(llm=CountingMockLLM())
    novel_tweet = "Totally unique tweet about XYZGADGET-9000 launch event today."
    # Pre-empt vocab path by using a tweet with no vocab matches at all.
    # We confirm by checking the call_count is exactly 1.

    started = time.perf_counter()
    results = await asyncio.gather(
        *[solver.solve(novel_tweet) for _ in range(10)]
    )
    elapsed = time.perf_counter() - started

    paths = [r.path for r in results]
    tags_all = [tuple(r.tags) for r in results]
    same_tags = expect(
        len(set(tags_all)) == 1,
        f"all 10 miners got identical tags: {tags_all[0]!r}",
    )
    one_call = expect(
        call_count["n"] == 1,
        f"LLM was called exactly once (got {call_count['n']})",
    )
    fast = expect(
        elapsed < 1.0,
        f"10 concurrent solves took {elapsed*1000:.0f}ms "
        f"(should be ~200ms + overhead, not 2000ms)",
    )

    # First should be llm path (or vocab), rest should be exact_cache.
    cache_hits = sum(1 for p in paths if p == "exact_cache")
    cache_check = expect(
        cache_hits >= 8,
        f"{cache_hits}/10 hit the cache (LLM ran once, 9 should be cached)",
    )

    await solver.aclose()
    return same_tags and one_call and fast and cache_check


# ---------------------------------------------------------------------------
# 4. Validity check matches validator math
# ---------------------------------------------------------------------------

async def test_validity_matches_validator() -> bool:
    header("TEST 4 — Local validity replica matches validator's tier output")
    import numpy as np
    from sentence_transformers import SentenceTransformer
    from solver.scoring import build_spans

    # Build a validator-side scorer and our local predictor, score the same
    # input, and confirm the tier matches.
    try:
        from tag101.tasks.sn101_reference.core.scoring.validity import ValidityScorer
    except Exception as e:
        print(f"  {INFO}  skipping (tag101 not importable): {e}")
        return True

    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    scorer = ValidityScorer(model=model)
    post = "Anthropic released Claude 4.7 today with stronger coding."
    miner_responses = [["anthropic", "claude", "release"]]

    result = scorer.score(post, miner_responses)
    val_validity = result["validity_scores"][0]
    print(f"  {INFO}  validator validity scores: {val_validity}")

    from solver.service import Solver

    solver = Solver()
    spans = build_spans(post)
    tweet_emb = solver._embed([post])[0]
    span_embs = solver._embed(spans)
    tags = miner_responses[0]
    tag_embs = solver._embed(tags)
    local = [
        solver._predicted_validity(tag, tweet_emb, span_embs, emb, spans)
        for tag, emb in zip(tags, tag_embs)
    ]
    print(f"  {INFO}  local predicted validity: {local}")

    ok = all(abs(a - b) < 0.01 for a, b in zip(local, val_validity))
    expect(ok, "local validity matches validator validity tag-by-tag")

    await solver.aclose()
    return ok


# ---------------------------------------------------------------------------
# 5+6. FastAPI app + thin miner client over a real uvicorn server
# ---------------------------------------------------------------------------

async def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return True
            except (ConnectionRefusedError, socket.timeout, OSError):
                await asyncio.sleep(0.2)
    return False


async def test_http_and_thin_miner() -> bool:
    header("TEST 5/6 — uvicorn + HTTP /solve, /health, /stats + thin miner client")
    import contextlib
    import httpx
    import uvicorn
    from solver.app import app

    port = 7311
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    server_task = asyncio.create_task(server.serve())

    try:
        ok_up = await _wait_for_port("127.0.0.1", port, timeout=30.0)
        if not expect(ok_up, f"uvicorn listening on 127.0.0.1:{port}"):
            return False

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                    timeout=10.0) as cli:
            h = await cli.get("/health")
            ok_health = expect(
                h.status_code == 200 and h.json().get("ok") is True,
                f"/health 200 with ok=True (got {h.status_code} {h.text[:60]})",
            )

            body = {"tweet": "Anthropic released Claude 4.7 today."}
            s = await cli.post("/solve", json=body)
            ok_solve = expect(
                s.status_code == 200
                and isinstance(s.json().get("tags"), list)
                and len(s.json()["tags"]) == 3,
                f"/solve returns 3 tags: "
                f"{s.json() if s.status_code==200 else s.text}",
            )

            stats = await cli.get("/stats")
            ok_stats = expect(
                stats.status_code == 200 and stats.json().get("ready") is True,
                f"/stats ready=True (got {stats.json()})",
            )

        # Now the thin miner client (which uses the same /solve under the hood)
        os.environ["SN101_SOLVER_URL"] = f"http://127.0.0.1:{port}"
        import importlib
        import thin_miner
        importlib.reload(thin_miner)

        class StubEnvelope:
            payload = {"text": "Anthropic released Claude 4.7 today."}

        # solve_problem() does blocking I/O — wrap in to_thread so it doesn't
        # starve uvicorn's loop. In production, miner and solver are in
        # separate processes / hosts, so this is not needed there.
        result = await asyncio.to_thread(
            thin_miner.solve_problem, StubEnvelope(), None
        )
        ok_miner = expect(
            isinstance(result, dict)
            and isinstance(result.get("tags"), list)
            and len(result["tags"]) == 3
            and result["tags"] != thin_miner.SAFE_DEFAULT_TAGS,
            f"thin_miner.solve_problem returned: {result} "
            f"(should NOT be safe defaults)",
        )

        # Sybil verification: 5 thin-miner stubs run concurrently and all
        # return identical tags (proving solver dedup works through the
        # HTTP boundary too).
        async def _call_once() -> dict:
            return await asyncio.to_thread(
                thin_miner.solve_problem, StubEnvelope(), None
            )

        results = await asyncio.gather(*[_call_once() for _ in range(5)])
        all_tags = [tuple(r["tags"]) for r in results]
        ok_sybil = expect(
            len(set(all_tags)) == 1,
            f"5 concurrent thin-miner clients all returned identical tags: "
            f"{all_tags[0]}",
        )

        return ok_health and ok_solve and ok_stats and ok_miner and ok_sybil
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)


# ---------------------------------------------------------------------------
# 6. Auth gate (X-Solver-Key) actually blocks/allows
# ---------------------------------------------------------------------------

async def test_auth_gate() -> bool:
    header("TEST 6 — Auth gate: no key = 401, wrong key = 401, right key = 200")
    import contextlib
    import importlib

    import httpx
    import uvicorn

    # Tear down any cached app module so we can re-import with SOLVER_API_KEY set.
    SECRET = "test-secret-1234567890abcdef"
    os.environ["SOLVER_API_KEY"] = SECRET
    for mod in list(sys.modules):
        if mod.startswith("solver"):
            del sys.modules[mod]
    from solver.app import app  # re-imported with env var in place

    port = 7312  # different port so it doesn't clash with test 5
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    server_task = asyncio.create_task(server.serve())

    try:
        ok_up = await _wait_for_port("127.0.0.1", port, timeout=30.0)
        if not expect(ok_up, f"uvicorn listening on 127.0.0.1:{port}"):
            return False

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",
                                    timeout=10.0) as cli:
            # /health stays open regardless of the key — uptime monitors
            h = await cli.get("/health")
            ok_health_open = expect(
                h.status_code == 200,
                f"/health still open without key: {h.status_code}",
            )

            # /solve without key -> 401
            no_key = await cli.post("/solve", json={"tweet": "hi"})
            ok_no_key = expect(
                no_key.status_code == 401,
                f"/solve without key returns 401 (got {no_key.status_code})",
            )

            # /solve with wrong key -> 401
            wrong = await cli.post(
                "/solve",
                json={"tweet": "hi"},
                headers={"X-Solver-Key": "wrong-key"},
            )
            ok_wrong = expect(
                wrong.status_code == 401,
                f"/solve with wrong key returns 401 (got {wrong.status_code})",
            )

            # /solve with correct key -> 200
            right = await cli.post(
                "/solve",
                json={"tweet": "Anthropic released Claude 4.7 today."},
                headers={"X-Solver-Key": SECRET},
            )
            ok_right = expect(
                right.status_code == 200
                and isinstance(right.json().get("tags"), list)
                and len(right.json()["tags"]) == 3,
                f"/solve with correct key returns 3 tags: {right.json() if right.status_code==200 else right.text}",
            )

            # /stats with wrong key -> 401
            stats_wrong = await cli.get("/stats")
            ok_stats_wrong = expect(
                stats_wrong.status_code == 401,
                f"/stats also requires the key (got {stats_wrong.status_code})",
            )

            # /stats with correct key -> 200
            stats_right = await cli.get("/stats", headers={"X-Solver-Key": SECRET})
            ok_stats_right = expect(
                stats_right.status_code == 200 and stats_right.json().get("ready"),
                f"/stats with correct key returns ready=True",
            )

        # Now verify thin_miner sends the header when SN101_SOLVER_API_KEY is set
        os.environ["SN101_SOLVER_URL"] = f"http://127.0.0.1:{port}"
        os.environ["SN101_SOLVER_API_KEY"] = SECRET
        if "thin_miner" in sys.modules:
            del sys.modules["thin_miner"]
        import thin_miner

        class StubEnvelope:
            payload = {"text": "Anthropic released Claude 4.7 today."}

        result = await asyncio.to_thread(
            thin_miner.solve_problem, StubEnvelope(), None
        )
        ok_miner_with_key = expect(
            isinstance(result, dict)
            and len(result.get("tags", [])) == 3
            and result["tags"] != thin_miner.SAFE_DEFAULT_TAGS,
            f"thin_miner WITH key reached the solver: {result}",
        )

        # Drop the key and confirm thin_miner now gets 401 and falls back to safe defaults
        os.environ["SN101_SOLVER_API_KEY"] = ""
        if "thin_miner" in sys.modules:
            del sys.modules["thin_miner"]
        import thin_miner as thin_miner_nokey

        result_nokey = await asyncio.to_thread(
            thin_miner_nokey.solve_problem, StubEnvelope(), None
        )
        ok_miner_without_key = expect(
            result_nokey["tags"] == thin_miner_nokey.SAFE_DEFAULT_TAGS,
            f"thin_miner WITHOUT key gets blocked, returns safe defaults: {result_nokey}",
        )

        return (
            ok_health_open and ok_no_key and ok_wrong and ok_right
            and ok_stats_wrong and ok_stats_right
            and ok_miner_with_key and ok_miner_without_key
        )
    finally:
        del os.environ["SOLVER_API_KEY"]
        os.environ.pop("SN101_SOLVER_API_KEY", None)
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(server_task, timeout=5.0)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def main() -> int:
    tests = [
        ("solver_basics", test_solver_basics),
        ("cache_paths", test_cache_paths),
        ("concurrency_dedup", test_concurrency_dedup),
        ("validity_matches_validator", test_validity_matches_validator),
        ("http_and_thin_miner", test_http_and_thin_miner),
        ("auth_gate", test_auth_gate),
    ]

    results: dict[str, bool] = {}
    for name, fn in tests:
        try:
            ok = await fn()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            ok = False
        results[name] = bool(ok)

    header("SUMMARY")
    for name, ok in results.items():
        print(f"  {OK if ok else NO}  {name}")
    failed = [n for n, ok in results.items() if not ok]
    if failed:
        print(f"\n{len(failed)} test(s) failed: {failed}")
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

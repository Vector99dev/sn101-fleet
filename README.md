# sn101-fleet

A centralized solver backend for Bittensor [Subnet 101 (Tag101)](https://github.com/tag101-ai/tag101) miners.

One solver service generates canonical tags for incoming tweets; many thin-miner
clients share it. A per-tweet dedup lock collapses concurrent requests for the
same tweet into a single upstream LLM call, so a fleet of N miners makes one
LLM call per task instead of N.

> [!IMPORTANT]
> Targets **Ubuntu 22.04 LTS**. Tested on OVHCloud; should work on any
> Ubuntu 22.04 / 24.04 VPS with ≥4 GB RAM.

---

## Quick start

On a fresh Ubuntu 22.04 VPS:

```bash
sudo apt-get install -y git
git clone https://github.com/Vector99dev/sn101-fleet.git
cd sn101-fleet
OPENROUTER_API_KEY=sk-or-v1-... ./install.sh
```

Or run interactively (you'll be prompted for the key):

```bash
./install.sh
```

The installer handles everything: system deps, Node 20 + pm2, Python venv,
[tag101](https://github.com/tag101-ai/tag101) clone, sentence-transformers + torch,
MiniLM-L6-v2 cache, env file, auto-restart, boot persistence, and 4 smoke tests.
End-to-end: **~25 minutes**, almost all of it waiting on pip + torch download.

When it finishes, the green box prints your auto-generated `SOLVER_API_KEY` —
copy it to your password manager. You'll need it on every miner VPS.

---

## On the miner side

Each miner VPS clones [tag101](https://github.com/tag101-ai/tag101) and sets three env vars:

```bash
SN101_SOLVER_URL=http://<solver-ip>:7311
SN101_SOLVER_API_KEY=<key from installer output>
TASK_MINER_MODULE=thin_miner
```

The Tag101 miner's task registry then loads
[`thin_miner.solve_problem`](./thin_miner.py) instead of the OpenAI reference
handler. Every miner forward becomes a blocking HTTP call to the central solver.

---

## How it works

```
   [validator]
        │ tweet
        ▼
   [thin miner]  ──X-Solver-Key──▶  [solver]
                                         │
                                   per-key dedup lock
                                         │
              ┌──────────────┬───────────┴────────────┐
              ▼              ▼                        ▼
        exact cache     vocab cache              LLM fallback
        (24h TTL)    (embedded MiniLM)        (OpenRouter chain:
                                               gpt-4o-mini →
                                               gemini-flash →
                                               claude-haiku →
                                               llama-3.3)
                                                       │
                                                       ▼
                                              canonicalize →
                                              validity filter →
                                              diversity pick →
                                              cache → return
```

The solver's local validity replica mirrors Tag101's real
[`ValidityScorer`](https://github.com/tag101-ai/tag101/blob/main/tasks/sn101_reference/core/scoring/validity.py)
math closely enough that anything that passes the local check passes the
on-chain validator's check. See [`solver/scoring.py`](./solver/scoring.py).

---

## Repo layout

| Path | Purpose |
|------|---------|
| [`install.sh`](./install.sh) | One-shot installer for fresh VPS |
| [`RUNBOOK.md`](./RUNBOOK.md) | Manual install, DR recovery, troubleshooting |
| [`solver/`](./solver/) | FastAPI service, cache, vocab, LLM router, scoring replica |
| [`thin_miner.py`](./thin_miner.py) | Drop-in replacement for `tag101.tasks.sn101.solve_problem` |
| [`deploy/`](./deploy/) | pm2 ecosystem + wrapper script |
| [`tests/`](./tests/) | 6-test verification suite |

---

## Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `GET`  | `/health` | none | uptime probes / OVH DDoS protection |
| `POST` | `/solve`  | `X-Solver-Key` | `{tweet}` → `{tags, path, latency_ms}` |
| `GET`  | `/stats`  | `X-Solver-Key` | cache stats, hit rates, last_path |

---

## Tests

```bash
TRANSFORMERS_VERBOSITY=error HF_HUB_DISABLE_PROGRESS_BARS=1 \
  PYTHONPATH=.:$(realpath ..) python tests/test_pipeline.py
```

Six tests:

1. Solver basics (vocab + LLM paths, deduped lowercased tags)
2. Cache paths (vocab → exact_cache on repeat)
3. Dedup lock (10 concurrent miners → 1 LLM call)
4. Validity replica matches Tag101's `ValidityScorer` tier-by-tier
5. uvicorn HTTP + thin miner client + 5 concurrent thin miners return identical tags
6. Auth gate (no key → 401, wrong key → 401, right key → 200, miner-side env var injection)

All six pass on the deployed VPS.

---

## Operating notes

- **Auth gate**: `/solve` and `/stats` require `X-Solver-Key`. `/health` stays open
  so OVH probes and your monitoring don't get 401-noise.
- **PM2 restart policy**: `Restart=always` with 3 s backoff, 20 s min uptime, 2 GB
  memory ceiling. Survives `SIGKILL` in ~7 s and full daemon restart in ~4 s.
- **Boot persistence**: pm2 writes a `systemd` unit (`pm2-<user>.service`) that
  restores saved processes on reboot.
- **Env file**: `~/.sn101.env` (chmod 600). The pm2 wrapper sources it on each
  launch, so editing the file and `pm2 restart sn101-solver --update-env` is the
  standard flow for rotating keys.

---

## Disaster recovery

See [`RUNBOOK.md`](./RUNBOOK.md) — it documents the entire manual install
sequence step by step, troubleshooting for the six most common failures, and how
to rebuild everything from this repo on a fresh VPS if the installer breaks.

---

## License

MIT. Use at your own risk.

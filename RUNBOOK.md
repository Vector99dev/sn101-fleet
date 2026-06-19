# SN101 Solver — Emergency Recovery Runbook

> **Purpose:** rebuild the central solver backend on a brand-new Ubuntu VPS,
> from zero, without help. Print this, email it to yourself, or commit it to
> a private git repo. Treat the secrets at the bottom as sensitive.

---

## When to use this

- The current solver VPS is destroyed / lost / inaccessible
- You're migrating to a different provider (Hetzner, DigitalOcean, etc.)
- You're standing up a hot-spare solver
- You want to verify your DR plan actually works

**Expected time:** ~30 minutes, mostly waiting on pip downloads.

---

## Quick path — automated installer (recommended)

If `install.sh` exists in your repo (it does in the current version), you don't
need to follow the manual steps below. Just run:

```bash
ssh ubuntu@<NEW_VPS_IP>
sudo apt-get install -y git
git clone https://github.com/<YOU>/sn101-fleet.git
cd sn101-fleet
OPENROUTER_API_KEY=sk-or-v1-... ./install.sh
```

The installer does all 12 manual steps below: system deps → Node → pm2 → tag101
→ Python venv → MiniLM cache → env file (with auto-generated solver key) →
pm2 start → boot persistence → 4 smoke tests. At the end it prints your new
`SOLVER_API_KEY` — copy it into your password manager immediately; it's not
displayed again.

If the installer succeeds, jump straight to **Step 11 — Firewall** below.

If the installer fails partway through, the manual steps below help you finish
the job by hand. They are also useful for understanding what the installer is
doing.

---

## What you need before you start

| Item | Where to get it |
|------|-----------------|
| A fresh **Ubuntu 22.04 LTS** VPS (x86_64, ≥4 GB RAM, ≥20 GB disk) | Provider dashboard |
| **Root / sudo** access on that VPS | Provider dashboard |
| **Backup of `sn101-fleet/`** code | Your backup location (see "Backup Strategy" at the end) |
| **OpenRouter API key** | https://openrouter.ai/keys — rotate if you suspect compromise |
| Outbound internet to: `deb.nodesource.com`, `pypi.org`, `huggingface.co`, `openrouter.ai`, `github.com` | Provider default usually allows this |

If you do **not** have a backup of `sn101-fleet/`, jump to **Appendix A** at the bottom — it contains the small files you'd need to recreate by hand, and the large files you'd want to grab from git history or another miner VPS that still has the rsynced copy.

---

## Step 0 — SSH in

```bash
ssh ubuntu@<NEW_VPS_IP>
# OR if the provider gives you root only:
ssh root@<NEW_VPS_IP>
```

Everything below assumes you're logged in as a sudo-capable user (`ubuntu` on most cloud images). If you're logged in as `root`, prepend nothing; if as `ubuntu`, the `sudo` calls below work as-is.

---

## Step 1 — System dependencies

```bash
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3-venv python3-pip python3-dev \
  build-essential pkg-config \
  libssl-dev libffi-dev \
  curl git rsync openssl
```

**Verify:** `python3 --version` should print 3.10+ (Ubuntu 22.04 ships 3.10.12).

---

## Step 2 — Install Node.js 20 LTS + pm2

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y -qq nodejs
sudo npm install -g pm2@latest
```

**Verify:**
```bash
node --version    # v20.x
pm2 --version     # 7.x or newer
```

---

## Step 3 — Recover the solver code

You have two options, in order of preference:

### Option A — restore from your backup tarball

```bash
# scp the tarball from wherever it lives (laptop, backup VPS, etc.)
scp sn101-fleet-backup.tar.gz ubuntu@<NEW_VPS_IP>:/tmp/
# Then on the new VPS:
mkdir -p /home/ubuntu/sn101-fleet
tar xzf /tmp/sn101-fleet-backup.tar.gz -C /home/ubuntu/
```

### Option B — restore from a private git repo

```bash
cd /home/ubuntu
git clone <YOUR_PRIVATE_REPO_URL> sn101-fleet
```

### Option C — pull from a running miner VPS

If any miner VPS still has `/home/ubuntu/sn101-fleet/` from a prior rsync:
```bash
# From the new solver VPS:
rsync -avz --exclude='__pycache__' --exclude='*.pyc' \
  ubuntu@<MINER_VPS_IP>:/home/ubuntu/sn101-fleet/ /home/ubuntu/sn101-fleet/
```

### Verify

```bash
ls /home/ubuntu/sn101-fleet/
# Expected: solver/  thin_miner.py  tests/  deploy/  RUNBOOK.md
ls /home/ubuntu/sn101-fleet/solver/
# Expected: app.py cache.py llm.py scoring.py service.py vocab.py __init__.py
```

If files are missing, jump to **Appendix A** to recreate them by hand.

---

## Step 4 — Clone the official Tag101 repo

```bash
cd /home/ubuntu
git clone --depth=1 https://github.com/tag101-ai/tag101.git
```

**Verify:**
```bash
ls /home/ubuntu/tag101/  # should include miner.py, validator.py, tasks/, chain/
```

---

## Step 5 — Python venv + dependencies

```bash
python3 -m venv /home/ubuntu/sn101-venv
/home/ubuntu/sn101-venv/bin/pip install --upgrade pip wheel

# Install lightweight deps first
/home/ubuntu/sn101-venv/bin/pip install --retries 5 --timeout 120 \
  numpy scikit-learn

/home/ubuntu/sn101-venv/bin/pip install --retries 5 --timeout 120 \
  fastapi "uvicorn[standard]" httpx pydantic

# This one downloads torch (~200 MB) — be patient, several minutes
/home/ubuntu/sn101-venv/bin/pip install --retries 8 --timeout 300 \
  sentence-transformers

# Optional but recommended (lets miner.py run on the same box later)
/home/ubuntu/sn101-venv/bin/pip install --retries 8 --timeout 300 \
  bittensor
```

**Verify:**
```bash
/home/ubuntu/sn101-venv/bin/python -c \
  "import numpy, sklearn, sentence_transformers, fastapi, uvicorn, httpx, pydantic, torch; \
   print('phase 1 OK, torch=' + torch.__version__)"
```

If `pip install` is killed by network glitches, just rerun the same command — pip skips packages it already has.

---

## Step 6 — Pre-cache the MiniLM-L6-v2 model

This downloads the 80 MB embedding model so the first solver startup is fast.

```bash
TRANSFORMERS_VERBOSITY=error HF_HUB_DISABLE_PROGRESS_BARS=1 \
/home/ubuntu/sn101-venv/bin/python -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
print('cached, embedding dim =', m.encode(['hello']).shape)
"
```

Expected: `cached, embedding dim = (1, 384)`

---

## Step 7 — Configure the environment file

Generate a fresh **solver API key** (do NOT reuse the old one — assume the old VPS was compromised):

```bash
NEW_SOLVER_KEY=$(openssl rand -hex 32)
echo "New solver key: $NEW_SOLVER_KEY"
echo "WRITE THIS DOWN — you'll need it on every miner VPS"
```

Write the env file (replace `<YOUR_OPENROUTER_KEY>` with your actual key):

```bash
cat > /home/ubuntu/.sn101.env <<EOF
OPENROUTER_API_KEY=<YOUR_OPENROUTER_KEY>
SOLVER_API_KEY=$NEW_SOLVER_KEY
TRANSFORMERS_VERBOSITY=error
HF_HUB_DISABLE_PROGRESS_BARS=1
TOKENIZERS_PARALLELISM=false
PYTHONUNBUFFERED=1
EOF
chmod 600 /home/ubuntu/.sn101.env
```

**Verify:**
```bash
ls -la /home/ubuntu/.sn101.env
# Expected: -rw------- 1 ubuntu ubuntu (chmod 600, owned by ubuntu)
```

**Save the new solver key somewhere safe** (password manager, separate note). You'll need it on every miner VPS as `SN101_SOLVER_API_KEY`.

---

## Step 8 — Verify the wrapper + ecosystem files exist

These should be in your code backup at `/home/ubuntu/sn101-fleet/deploy/`. If they're not, see **Appendix A** for their contents.

```bash
ls -la /home/ubuntu/sn101-fleet/deploy/
# Expected:
#   ecosystem.config.cjs    (pm2 ecosystem)
#   start-solver.sh         (wrapper script)

chmod +x /home/ubuntu/sn101-fleet/deploy/start-solver.sh
```

---

## Step 9 — Start under pm2

```bash
pm2 start /home/ubuntu/sn101-fleet/deploy/ecosystem.config.cjs
sleep 15   # wait for sentence-transformers to load
pm2 list
```

Expected `pm2 list` output:
```
│  0 │ sn101-solver  │ default │ N/A │ fork │ <PID> │ <UPTIME> │ 0 │ online │ ...
```

**Save state** (so it survives a pm2 restart):
```bash
pm2 save
```

**Install boot-time auto-start:**
```bash
pm2 startup systemd -u ubuntu --hp /home/ubuntu
# pm2 will print a "sudo env PATH=..." command — copy it and run it:
sudo env PATH=$PATH:/usr/bin pm2 startup systemd -u ubuntu --hp /home/ubuntu
```

**Verify boot unit:**
```bash
sudo systemctl is-enabled pm2-ubuntu   # should print "enabled"
```

---

## Step 10 — Smoke-test the endpoint

```bash
KEY=$(grep SOLVER_API_KEY /home/ubuntu/.sn101.env | cut -d= -f2-)

# Health (no key required)
curl -sf http://127.0.0.1:7311/health
# Expected: {"ok":true}

# Solve without key — must be REJECTED
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:7311/solve \
  -H "content-type: application/json" -d '{"tweet":"hi"}'
# Expected: 401

# Solve with key — must SUCCEED
curl -s -X POST http://127.0.0.1:7311/solve \
  -H "content-type: application/json" \
  -H "X-Solver-Key: $KEY" \
  -d '{"tweet":"Anthropic released Claude 4.7 today with stronger coding."}'
# Expected: {"tags":["claude","coding","anthropic"],"path":"vocab","latency_ms":<30}
```

If you see `"path":"llm"` and latency >1000ms on a vocab-rich tweet, OpenRouter probably worked but vocab cache miss — usually fine but check the env key was loaded.

---

## Step 11 — Firewall (production)

Until you allowlist miner IPs, anyone with `<NEW_VPS_IP>:7311` can burn your
OpenRouter quota. Restrict to known miners:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp comment 'ssh'
# Add one rule per miner VPS:
sudo ufw allow from <MINER_1_IP> to any port 7311 comment 'sn101-miner-1'
sudo ufw allow from <MINER_2_IP> to any port 7311 comment 'sn101-miner-2'
sudo ufw allow from <MINER_3_IP> to any port 7311 comment 'sn101-miner-3'
sudo ufw enable
```

**Verify:**
```bash
sudo ufw status numbered
```

---

## Step 12 — Update miner VPSes with the new solver address + key

On every miner VPS, edit the miner env file (e.g. `/home/ubuntu/.sn101-miner.env`):

```bash
SN101_SOLVER_URL=http://<NEW_SOLVER_VPS_IP>:7311
SN101_SOLVER_API_KEY=<the new key from Step 7>
TASK_MINER_MODULE=thin_miner
```

Then restart each miner: `pm2 restart sn101-miner`.

---

## You're done

The solver is running, auto-restarts on crash, comes back on reboot, and only accepts authenticated requests.

---

## Troubleshooting

### `pm2 list` shows `errored`
```bash
pm2 logs sn101-solver --lines 80 --nostream
```
Common causes:
- Env file unreadable → check `/home/ubuntu/.sn101.env` permissions
- Wrong PYTHONPATH → check `start-solver.sh` paths
- Sentence-transformers download failed → re-run Step 6

### `pm2 list` shows `online` but `/health` returns nothing
```bash
ss -tlnp | grep 7311           # is uvicorn listening?
pm2 logs sn101-solver --lines 50 --nostream
```
Uvicorn might still be loading the embedding model (takes ~5–15s on first boot).

### `/solve` returns 401 with the right key
```bash
# Verify the key is what the solver actually loaded
sudo cat /home/ubuntu/.sn101.env | grep SOLVER_API_KEY
# Match it exactly against the X-Solver-Key header you're sending.
# Restart pm2 after env changes:
pm2 restart sn101-solver --update-env
```

### OpenRouter calls failing silently (path=fallback or path=llm but mock-looking tags)
```bash
# Test the key directly against OpenRouter:
KEY=$(grep OPENROUTER_API_KEY /home/ubuntu/.sn101.env | cut -d= -f2-)
curl -sS -X POST https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"test"}],"max_tokens":5}'
```
- If you see 401 → key is invalid/expired, rotate at openrouter.ai
- If you see 402 → out of credit, top up
- If you see a chat response → solver-side bug, check pm2 logs

### Solver eats too much RAM (>2 GB)
That's the `max_memory_restart` ceiling — pm2 will recycle it automatically. If it's happening often, increase to 3G in `ecosystem.config.cjs` or investigate via:
```bash
pm2 monit
```

### Need to rotate the solver API key
```bash
NEW=$(openssl rand -hex 32)
sed -i "s/^SOLVER_API_KEY=.*/SOLVER_API_KEY=$NEW/" /home/ubuntu/.sn101.env
pm2 restart sn101-solver --update-env
echo "new key: $NEW  — update all miner VPSes now"
```

---

## Backup strategy (do this proactively)

Before any disaster, set up regular backups so this runbook is short:

```bash
# Run weekly from another machine, e.g. cron on your laptop:
ssh ubuntu@<CURRENT_SOLVER_IP> 'tar czf - -C /home/ubuntu sn101-fleet' \
  > sn101-fleet-$(date +%Y%m%d).tar.gz
```

Or commit `sn101-fleet/` to a private git repo and push after each change.

Also keep a copy of:
- `/home/ubuntu/.sn101.env` (in password manager — NOT in git)
- `/home/ubuntu/sn101-fleet/deploy/ecosystem.config.cjs`
- `/home/ubuntu/sn101-fleet/deploy/start-solver.sh`

---

## Appendix A — Recreate critical files by hand

If your backup is corrupt or you have no backup, you can recreate the small operational files manually. The large Python files in `solver/` would need to be restored from git history or a miner VPS rsync.

### `/home/ubuntu/sn101-fleet/deploy/start-solver.sh`

```bash
#!/usr/bin/env bash
# pm2 entry-point for the SN101 solver. Loads the env file then execs uvicorn.

set -e
ENV_FILE="${SN101_ENV_FILE:-/home/ubuntu/.sn101.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[start-solver] env file unreadable: $ENV_FILE" >&2
  exit 2
fi

set -a
source "$ENV_FILE"
set +a

VENV_BIN="${SN101_VENV_BIN:-/home/ubuntu/sn101-venv/bin}"
SOLVER_DIR="${SN101_SOLVER_DIR:-/home/ubuntu/sn101-fleet}"
HOST="${SN101_SOLVER_HOST:-0.0.0.0}"
PORT="${SN101_SOLVER_PORT:-7311}"

cd "$SOLVER_DIR"
export PYTHONPATH="$SOLVER_DIR:/home/ubuntu:${PYTHONPATH:-}"
exec "$VENV_BIN/uvicorn" solver.app:app --host "$HOST" --port "$PORT" --workers 1
```

Then: `chmod +x /home/ubuntu/sn101-fleet/deploy/start-solver.sh`

### `/home/ubuntu/sn101-fleet/deploy/ecosystem.config.cjs`

```javascript
module.exports = {
  apps: [
    {
      name: "sn101-solver",
      script: "/home/ubuntu/sn101-fleet/deploy/start-solver.sh",
      interpreter: "bash",
      cwd: "/home/ubuntu/sn101-fleet",
      autorestart: true,
      watch: false,
      max_restarts: 50,
      min_uptime: "20s",
      restart_delay: 3000,
      exp_backoff_restart_delay: 200,
      max_memory_restart: "2G",
      kill_timeout: 10000,
      out_file: "/home/ubuntu/.pm2/logs/sn101-solver-out.log",
      error_file: "/home/ubuntu/.pm2/logs/sn101-solver-err.log",
      merge_logs: true,
      time: true,
      env: { NODE_ENV: "production" },
    },
  ],
};
```

### `/home/ubuntu/.sn101.env` template

```bash
OPENROUTER_API_KEY=<your-openrouter-key>
SOLVER_API_KEY=<32-byte-hex from `openssl rand -hex 32`>
TRANSFORMERS_VERBOSITY=error
HF_HUB_DISABLE_PROGRESS_BARS=1
TOKENIZERS_PARALLELISM=false
PYTHONUNBUFFERED=1
```

Permissions: `chmod 600 /home/ubuntu/.sn101.env`

### What to do if `solver/` source files are missing

The Python source under `solver/` (`app.py`, `service.py`, `cache.py`, `llm.py`, `scoring.py`, `vocab.py`) totals ~1,400 lines. Restoration options in order of preference:

1. **Git**: `git clone <your-private-repo>` if you committed it
2. **Miner VPS**: `rsync -avz ubuntu@<miner_ip>:/home/ubuntu/sn101-fleet/ /home/ubuntu/sn101-fleet/` if any miner still has the code locally (they don't run the solver but the code may have been rsynced during setup)
3. **From-scratch**: recreate from your local development copy (this is why the backup strategy above matters)

---

## Appendix B — Environment variable reference

### Solver-side (`/home/ubuntu/.sn101.env`)

| Variable | Required | Purpose |
|----------|----------|---------|
| `OPENROUTER_API_KEY` | yes | Authenticates the solver to OpenRouter. Without it, solver falls through to a deterministic mock generator (bad in production). |
| `SOLVER_API_KEY` | yes for production | When set, `/solve` and `/stats` require `X-Solver-Key: <value>` header. When empty, solver runs in **open mode** with a startup warning. |
| `TRANSFORMERS_VERBOSITY` | optional | `error` silences sentence-transformers chatter |
| `HF_HUB_DISABLE_PROGRESS_BARS` | optional | `1` silences download progress bars |
| `TOKENIZERS_PARALLELISM` | optional | `false` prevents a fork warning |
| `PYTHONUNBUFFERED` | optional | `1` ensures uvicorn logs flush immediately to pm2 |

### Miner-side (each miner VPS)

| Variable | Required | Purpose |
|----------|----------|---------|
| `SN101_SOLVER_URL` | yes | `http://<solver_ip>:7311` |
| `SN101_SOLVER_API_KEY` | yes (if solver has gate enabled) | Must match solver's `SOLVER_API_KEY` exactly |
| `SN101_SOLVER_TIMEOUT_S` | optional | Default 5.0. Increase if solver is far away geographically. |
| `TASK_MINER_MODULE` | yes | `thin_miner` — tells the Tag101 miner to load this module's `solve_problem` instead of OpenAI reference |

---

## Appendix C — Useful commands

```bash
# Status
pm2 list
pm2 describe sn101-solver
ss -tlnp | grep 7311

# Logs
pm2 logs sn101-solver --lines 100 --nostream
tail -f /home/ubuntu/.pm2/logs/sn101-solver-out.log

# Restart
pm2 restart sn101-solver
pm2 restart sn101-solver --update-env  # picks up new env file values

# Stop / remove
pm2 stop sn101-solver
pm2 delete sn101-solver
pm2 save   # always save after delete/add

# Inspect env file
sudo cat /home/ubuntu/.sn101.env

# Health checks
curl -s http://127.0.0.1:7311/health
KEY=$(grep SOLVER_API_KEY /home/ubuntu/.sn101.env | cut -d= -f2-)
curl -s http://127.0.0.1:7311/stats -H "X-Solver-Key: $KEY"
```

---

## Appendix D — Sanity-test script

After install, run this from the new solver VPS to verify everything works.
Save as `/tmp/post-install-check.sh`, `chmod +x`, run.

```bash
#!/usr/bin/env bash
set -e
KEY=$(grep SOLVER_API_KEY /home/ubuntu/.sn101.env | cut -d= -f2-)
echo "== health =="
curl -sf http://127.0.0.1:7311/health || { echo "FAIL health"; exit 1; }

echo "== /solve without key (should be 401) =="
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:7311/solve \
  -H "content-type: application/json" -d '{"tweet":"hi"}')
[[ "$HTTP" == "401" ]] || { echo "FAIL: expected 401, got $HTTP"; exit 1; }
echo "PASS: 401 without key"

echo "== /solve with key =="
RESP=$(curl -s -X POST http://127.0.0.1:7311/solve \
  -H "content-type: application/json" \
  -H "X-Solver-Key: $KEY" \
  -d '{"tweet":"Anthropic released Claude 4.7 today with stronger coding."}')
echo "  $RESP"
echo "$RESP" | grep -q '"tags"' || { echo "FAIL: no tags in response"; exit 1; }
echo "PASS: /solve returned tags"

echo "== /stats with key =="
curl -s http://127.0.0.1:7311/stats -H "X-Solver-Key: $KEY"
echo

echo "== pm2 status =="
pm2 list

echo "== ALL CHECKS PASSED =="
```

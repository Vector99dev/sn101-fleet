#!/usr/bin/env bash
#
# SN101 Solver — one-shot installer for a fresh Ubuntu 22.04 VPS.
#
# What this does:
#   1. Installs system + Python + Node + pm2
#   2. Clones the official Tag101 repo
#   3. Creates a Python venv and installs all solver deps
#   4. Pre-caches the MiniLM-L6-v2 embedding model
#   5. Generates a solver API key (or uses one you provide)
#   6. Writes /home/ubuntu/.sn101.env with your OpenRouter key
#   7. Starts the solver under pm2 with auto-restart + boot persistence
#   8. Smoke-tests every endpoint
#
# Usage:
#
#   git clone https://github.com/<YOU>/sn101-fleet.git
#   cd sn101-fleet
#   OPENROUTER_API_KEY=sk-or-v1-... ./install.sh
#
# Or interactively (prompts for the key):
#
#   ./install.sh
#
# Tested on: Ubuntu 22.04 LTS. Should work on 24.04 with no changes.
#
# Override defaults via env vars:
#   OPENROUTER_API_KEY    — required (the only thing the script can't generate)
#   SOLVER_API_KEY        — optional (random 32-byte hex if omitted)
#   SN101_SOLVER_PORT     — default 7311
#   SN101_TAG101_REPO     — default https://github.com/tag101-ai/tag101.git
#   SN101_REINSTALL_DEPS  — set to 1 to force pip reinstall
#

set -euo pipefail

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
CSI=$'\033['
log()  { printf "%s\n" "${CSI}1;34m[install]${CSI}0m $*"; }
ok()   { printf "%s\n" "${CSI}1;32m[ ok ]${CSI}0m   $*"; }
warn() { printf "%s\n" "${CSI}1;33m[warn]${CSI}0m   $*"; }
die()  { printf "%s\n" "${CSI}1;31m[FAIL]${CSI}0m   $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Config (override via env vars)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${SN101_SOLVER_PORT:=7311}"
: "${SN101_TAG101_REPO:=https://github.com/tag101-ai/tag101.git}"
: "${SN101_REINSTALL_DEPS:=0}"
: "${SOLVER_API_KEY:=}"

USER_NAME="$(id -un)"
USER_HOME="$HOME"

SN101_FLEET_DIR="$SCRIPT_DIR"
SN101_VENV="$USER_HOME/sn101-venv"
SN101_ENV_FILE="$USER_HOME/.sn101.env"
SN101_TAG101_DIR="$USER_HOME/tag101"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
log "Preflight checks"

[[ -f /etc/os-release ]] || die "Not a Linux system?"
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || warn "Expected Ubuntu, got $ID (continuing anyway)"
case "${VERSION_ID:-}" in
    22.04|24.04) ok "Ubuntu $VERSION_ID detected" ;;
    *) warn "Recipe was tested on 22.04/24.04, got $VERSION_ID" ;;
esac

if [[ "$(id -u)" -eq 0 ]]; then
    die "Run as a non-root sudo user (e.g. 'ubuntu'), not as root. \
The pm2 daemon and env file are placed under the calling user's home."
fi

if ! sudo -n true 2>/dev/null; then
    warn "Your user ($USER_NAME) needs sudo. You'll be prompted for the password."
fi

# Confirm we're running from a checkout of sn101-fleet
[[ -d "$SN101_FLEET_DIR/solver" && -f "$SN101_FLEET_DIR/thin_miner.py" ]] || \
    die "This script must be run from inside the sn101-fleet repo (current: $SN101_FLEET_DIR)"
[[ -f "$SN101_FLEET_DIR/deploy/start-solver.sh" && -f "$SN101_FLEET_DIR/deploy/ecosystem.config.cjs" ]] || \
    die "Repo is missing deploy/start-solver.sh or deploy/ecosystem.config.cjs"

# OpenRouter key: env var, else prompt
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    if [[ -t 0 ]]; then
        printf "Enter your OpenRouter API key (input hidden): "
        read -rs OPENROUTER_API_KEY
        echo
    else
        die "OPENROUTER_API_KEY env var is required when running non-interactively"
    fi
fi
[[ -n "$OPENROUTER_API_KEY" ]] || die "OPENROUTER_API_KEY is empty"

# Sanity-check that the OpenRouter key looks like one (sk-or-v1-...)
[[ "$OPENROUTER_API_KEY" =~ ^sk-or- ]] || \
    warn "OPENROUTER_API_KEY doesn't start with 'sk-or-' — typo?"

ok "Preflight passed (user=$USER_NAME, home=$USER_HOME, repo=$SN101_FLEET_DIR)"

# ---------------------------------------------------------------------------
# Step 1 — System packages
# ---------------------------------------------------------------------------
log "Step 1/9 — installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-venv python3-pip python3-dev \
    build-essential pkg-config \
    libssl-dev libffi-dev \
    curl git rsync openssl ufw
ok "System packages installed"

# ---------------------------------------------------------------------------
# Step 2 — Node + pm2
# ---------------------------------------------------------------------------
log "Step 2/9 — installing Node.js 20 LTS + pm2"

need_node=true
if command -v node >/dev/null 2>&1; then
    node_major=$(node -v 2>/dev/null | sed -n 's/^v\([0-9]\+\).*/\1/p')
    if [[ -n "$node_major" && "$node_major" -ge 20 ]]; then
        need_node=false
        ok "Node $(node -v) already installed"
    fi
fi
if [[ "$need_node" == true ]]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1
    sudo apt-get install -y -qq nodejs
    ok "Node $(node -v) installed"
fi

if ! command -v pm2 >/dev/null 2>&1; then
    sudo npm install -g pm2@latest >/dev/null 2>&1
fi
ok "pm2 $(pm2 --version) ready"

# ---------------------------------------------------------------------------
# Step 3 — Clone tag101
# ---------------------------------------------------------------------------
log "Step 3/9 — cloning tag101"
if [[ -d "$SN101_TAG101_DIR/.git" ]]; then
    git -C "$SN101_TAG101_DIR" pull --ff-only >/dev/null 2>&1 || \
        warn "tag101 pull failed; keeping existing checkout"
else
    git clone --depth=1 "$SN101_TAG101_REPO" "$SN101_TAG101_DIR" >/dev/null 2>&1
fi
[[ -f "$SN101_TAG101_DIR/miner.py" ]] || die "tag101 clone looks broken"
ok "tag101 cloned at $SN101_TAG101_DIR"

# ---------------------------------------------------------------------------
# Step 4 — Python venv + dependencies
# ---------------------------------------------------------------------------
log "Step 4/9 — Python venv + dependencies (slow, several minutes)"

if [[ ! -d "$SN101_VENV" ]]; then
    python3 -m venv "$SN101_VENV"
fi
"$SN101_VENV/bin/pip" install --quiet --upgrade pip wheel

PIP_ARGS=(--retries 5 --timeout 120)
if [[ "$SN101_REINSTALL_DEPS" == "1" ]]; then
    PIP_ARGS+=(--force-reinstall)
fi

"$SN101_VENV/bin/pip" install --quiet "${PIP_ARGS[@]}" numpy scikit-learn
ok "numpy + scikit-learn"

"$SN101_VENV/bin/pip" install --quiet "${PIP_ARGS[@]}" fastapi "uvicorn[standard]" httpx pydantic
ok "fastapi/uvicorn/httpx/pydantic"

"$SN101_VENV/bin/pip" install --quiet --retries 8 --timeout 300 sentence-transformers
ok "sentence-transformers (with torch)"

# bittensor is heavy and only needed if you'll also run the miner here
"$SN101_VENV/bin/pip" install --quiet --retries 8 --timeout 300 bittensor || \
    warn "bittensor install failed — solver-only mode, miners must run elsewhere"

# Sanity-import everything
"$SN101_VENV/bin/python" - <<'PY' || die "Python import sanity check failed"
import numpy, sklearn, sentence_transformers, fastapi, uvicorn, httpx, pydantic, torch
print(f"  torch={torch.__version__}")
print(f"  sentence-transformers={sentence_transformers.__version__}")
PY
ok "All Python deps importable"

# ---------------------------------------------------------------------------
# Step 5 — Pre-cache embedding model
# ---------------------------------------------------------------------------
log "Step 5/9 — pre-caching MiniLM-L6-v2 (~80 MB)"
TRANSFORMERS_VERBOSITY=error HF_HUB_DISABLE_PROGRESS_BARS=1 \
"$SN101_VENV/bin/python" - <<'PY' || die "Model pre-cache failed"
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
v = m.encode(["hello world"], normalize_embeddings=True)
assert v.shape == (1, 384), v.shape
PY
ok "MiniLM-L6-v2 cached"

# ---------------------------------------------------------------------------
# Step 6 — Secrets + env file
# ---------------------------------------------------------------------------
log "Step 6/9 — writing env file"

if [[ -z "$SOLVER_API_KEY" ]]; then
    SOLVER_API_KEY=$(openssl rand -hex 32)
    log "Generated new SOLVER_API_KEY"
fi

umask 077
cat > "$SN101_ENV_FILE" <<EOF
# SN101 solver env — pm2's wrapper script (start-solver.sh) sources this on launch.
# Owner: $USER_NAME, chmod 600.
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
SOLVER_API_KEY=$SOLVER_API_KEY
TRANSFORMERS_VERBOSITY=error
HF_HUB_DISABLE_PROGRESS_BARS=1
TOKENIZERS_PARALLELISM=false
PYTHONUNBUFFERED=1
EOF
chmod 600 "$SN101_ENV_FILE"
ok "env file at $SN101_ENV_FILE (chmod 600)"

# If the repo isn't at the conventional /home/ubuntu/sn101-fleet path, tell
# start-solver.sh where to find it via env-vars in the env file.
if [[ "$SN101_FLEET_DIR" != "/home/ubuntu/sn101-fleet" || "$SN101_VENV" != "/home/ubuntu/sn101-venv" ]]; then
    cat >> "$SN101_ENV_FILE" <<EOF
SN101_SOLVER_DIR=$SN101_FLEET_DIR
SN101_VENV_BIN=$SN101_VENV/bin
EOF
    ok "Custom paths recorded in env file"
fi

# ---------------------------------------------------------------------------
# Step 7 — pm2 start
# ---------------------------------------------------------------------------
log "Step 7/9 — starting under pm2"

chmod +x "$SN101_FLEET_DIR/deploy/start-solver.sh"

# Kill any stale uvicorn squatting on the port
if ss -tln 2>/dev/null | grep -q ":$SN101_SOLVER_PORT "; then
    warn "Port $SN101_SOLVER_PORT busy; freeing it"
    sudo fuser -k "${SN101_SOLVER_PORT}/tcp" 2>/dev/null || true
    sleep 2
fi

# Stop any existing pm2 instance under this name (idempotent reruns)
pm2 delete sn101-solver >/dev/null 2>&1 || true

pm2 start "$SN101_FLEET_DIR/deploy/ecosystem.config.cjs" >/dev/null

# Wait up to 30s for /health to come up (sentence-transformers warm load)
log "Waiting for solver to be ready (loads embedding model)…"
for i in $(seq 1 30); do
    if curl -sf -m 2 "http://127.0.0.1:$SN101_SOLVER_PORT/health" >/dev/null 2>&1; then
        ok "solver responded at t=${i}s"
        break
    fi
    sleep 1
    [[ $i -eq 30 ]] && die "solver did not come up in 30s — check: pm2 logs sn101-solver --lines 80 --nostream"
done

# ---------------------------------------------------------------------------
# Step 8 — pm2 persistence
# ---------------------------------------------------------------------------
log "Step 8/9 — pm2 save + boot persistence"
pm2 save >/dev/null
# This generates and runs the systemd boot hook
sudo env PATH="$PATH:/usr/bin" pm2 startup systemd -u "$USER_NAME" --hp "$USER_HOME" >/dev/null 2>&1 || \
    warn "pm2 startup install had warnings; check with: sudo systemctl is-enabled pm2-$USER_NAME"
sudo systemctl is-enabled "pm2-$USER_NAME" >/dev/null 2>&1 && \
    ok "pm2-$USER_NAME.service enabled (will start on boot)" || \
    warn "Boot hook not enabled — investigate manually"

# ---------------------------------------------------------------------------
# Step 9 — Smoke tests
# ---------------------------------------------------------------------------
log "Step 9/9 — smoke tests"

# (a) /health
curl -sf "http://127.0.0.1:$SN101_SOLVER_PORT/health" >/dev/null || die "/health smoke test failed"
ok "/health OK"

# (b) /solve without key — must be 401
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "http://127.0.0.1:$SN101_SOLVER_PORT/solve" \
    -H "content-type: application/json" -d '{"tweet":"hi"}')
[[ "$HTTP" == "401" ]] || die "/solve without key returned HTTP $HTTP (expected 401)"
ok "/solve without key → 401 (gate works)"

# (c) /solve with key — must be 200 with 3 tags
RESP=$(curl -s -X POST "http://127.0.0.1:$SN101_SOLVER_PORT/solve" \
    -H "content-type: application/json" \
    -H "X-Solver-Key: $SOLVER_API_KEY" \
    -d '{"tweet":"Anthropic released Claude 4.7 today with stronger coding."}')
echo "$RESP" | grep -q '"tags"' || die "/solve with key did not return tags: $RESP"
ok "/solve with key returned tags"

# (d) pm2 process is online
STATUS=$(pm2 jlist 2>/dev/null | python3 -c \
    "import sys,json; r=[a['pm2_env']['status'] for a in json.load(sys.stdin) if a['name']=='sn101-solver']; print(r[0] if r else 'missing')")
[[ "$STATUS" == "online" ]] || die "pm2 status is '$STATUS' (expected 'online')"
ok "pm2 status = online"

# ---------------------------------------------------------------------------
# DONE
# ---------------------------------------------------------------------------
PUBLIC_IP=$(curl -sf -m 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

cat <<EOF

${CSI}1;32m========================================================================${CSI}0m
${CSI}1;32m  SN101 SOLVER INSTALL COMPLETE${CSI}0m
${CSI}1;32m========================================================================${CSI}0m

  Solver URL:        http://${PUBLIC_IP}:${SN101_SOLVER_PORT}
  PM2 process:       sn101-solver
  Env file:          ${SN101_ENV_FILE}  (chmod 600)
  Boot persistence:  pm2-${USER_NAME}.service (systemd)
  Sample response:   ${RESP}

${CSI}1;33m------------------------------------------------------------------------${CSI}0m
${CSI}1;33m  SAVE THESE NOW — they will NOT be shown again${CSI}0m
${CSI}1;33m------------------------------------------------------------------------${CSI}0m

  SOLVER_API_KEY=${SOLVER_API_KEY}

  Each miner VPS needs:
      SN101_SOLVER_URL=http://${PUBLIC_IP}:${SN101_SOLVER_PORT}
      SN101_SOLVER_API_KEY=${SOLVER_API_KEY}
      TASK_MINER_MODULE=thin_miner

${CSI}1;34m------------------------------------------------------------------------${CSI}0m
${CSI}1;34m  NEXT STEPS${CSI}0m
${CSI}1;34m------------------------------------------------------------------------${CSI}0m

  1. Copy SOLVER_API_KEY into your password manager.

  2. Restrict port ${SN101_SOLVER_PORT} to known miner IPs:

        sudo ufw default deny incoming
        sudo ufw default allow outgoing
        sudo ufw allow 22/tcp
        sudo ufw allow from <miner_ip_1> to any port ${SN101_SOLVER_PORT}
        sudo ufw allow from <miner_ip_2> to any port ${SN101_SOLVER_PORT}
        sudo ufw enable

  3. Useful commands:

        pm2 list
        pm2 logs sn101-solver --lines 50 --nostream
        pm2 restart sn101-solver --update-env
        curl -sf http://127.0.0.1:${SN101_SOLVER_PORT}/health

EOF

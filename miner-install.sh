#!/usr/bin/env bash
#
# SN101 Miner — one-shot installer for a fresh Ubuntu 22.04 VPS.
#
# What this does:
#   1. Installs system deps + Node + pm2 (idempotent, safe to re-run)
#   2. Clones sn101-fleet AND the official tag101 repo
#   3. Creates a Python venv with httpx + bittensor (lightweight — no torch)
#   4. Writes /home/ubuntu/.sn101-miner.env with solver URL + auth key
#   5. Registers ONE miner under pm2 (start-miner.sh + per-hotkey ecosystem)
#   6. Opens the axon port in ufw
#   7. Saves pm2 state + boot persistence
#
# Usage — one miner per invocation. Re-run for additional hotkeys on the same VPS.
#
#   SN101_SOLVER_URL=http://<SOLVER_VPS_IP>:7311 \
#   SN101_SOLVER_API_KEY=0bcf...your-key... \
#   ./miner-install.sh PM2_NAME COLDKEY_NAME HOTKEY_NAME [AXON_PORT]
#
# Examples:
#   ./miner-install.sh sn101-miner-jinsai25  jinsai25  jinsai25  8091
#   ./miner-install.sh sn101-miner-jinsai24  jinsai24  jinsai24  8092
#   ./miner-install.sh miner-A               cold1     hk1       8093   # custom pm2 name
#
# All four args are REQUIRED. PORT is no longer auto-picked — declare it
# explicitly so it's obvious which miner is on which port at install time.
#
# REQUIREMENTS BEFORE RUNNING:
#   - The hotkey file must already exist at:
#         ~/.bittensor/wallets/COLDKEY_NAME/hotkeys/HOTKEY_NAME
#     (scp it from wherever you generated the wallet)
#
#   - The coldkeypub.txt (PUBLIC only, NEVER the private coldkey!) should be at:
#         ~/.bittensor/wallets/COLDKEY_NAME/coldkeypub.txt
#     (optional but recommended — silences a bittensor warning)

set -euo pipefail

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
CSI=$'\033['
log()  { printf "%s\n" "${CSI}1;34m[miner-install]${CSI}0m $*"; }
ok()   { printf "%s\n" "${CSI}1;32m[ ok ]${CSI}0m         $*"; }
warn() { printf "%s\n" "${CSI}1;33m[warn]${CSI}0m         $*"; }
die()  { printf "%s\n" "${CSI}1;31m[FAIL]${CSI}0m         $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Args + Config
# ---------------------------------------------------------------------------
# Usage:
#   ./miner-install.sh PM2_NAME COLDKEY HOTKEY [AXON_PORT]
#
# PM2_NAME is the pm2 process name (e.g. "sn101-miner-jinsai25" or "miner-A").
# It also drives the env file path (~/.sn101-miner-<PM2_NAME>.env), the
# ecosystem file path, and the pm2 log file names. Must be unique per miner
# on this VPS.
MINER_NAME="${1:-}"
COLDKEY="${2:-}"
HOTKEY="${3:-}"
AXON_PORT="${4:-}"

# All four args are required. Without explicit PORT we'd risk silently using
# the old 3-arg layout where someone passes "coldkey hotkey port" and it gets
# interpreted as "pm2_name coldkey hotkey".
[[ -n "$MINER_NAME" && -n "$COLDKEY" && -n "$HOTKEY" && -n "$AXON_PORT" ]] || \
    die "Usage: $0 PM2_NAME COLDKEY HOTKEY AXON_PORT
Example: $0 sn101-miner-jinsai25 jinsai25 jinsai25 8091"

# Sanity-check the pm2 name: only allow safe filename characters.
[[ "$MINER_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || \
    die "PM2_NAME must contain only letters, digits, '.', '_', or '-' (got: $MINER_NAME)"

# Sanity-check the hotkey name doesn't look like a port (catches old 3-arg invocations)
[[ ! "$HOTKEY" =~ ^[0-9]+$ ]] || \
    die "HOTKEY looks like a port ($HOTKEY). You probably used the old 3-arg form. \
The new form is: PM2_NAME COLDKEY HOTKEY PORT"

: "${SN101_SOLVER_URL:?Set SN101_SOLVER_URL env var (e.g. http://<SOLVER_VPS_IP>:7311)}"
: "${SN101_SOLVER_API_KEY:?Set SN101_SOLVER_API_KEY env var}"
: "${SN101_SUBTENSOR_NETWORK:=finney}"
: "${SN101_REPO_URL:=https://github.com/Vector99dev/sn101-fleet.git}"
: "${SN101_TAG101_REPO:=https://github.com/tag101-ai/tag101.git}"

USER_NAME="$(id -un)"
USER_HOME="$HOME"
FLEET_DIR="$USER_HOME/sn101-fleet"
TAG101_DIR="$USER_HOME/tag101"
VENV_DIR="$USER_HOME/sn101-venv"
# Env + ecosystem paths now derive from the pm2 name (not the hotkey) so
# multiple miners reusing the same hotkey can coexist if you want them to.
ENV_FILE="$USER_HOME/.sn101-miner-${MINER_NAME}.env"
WALLET_DIR="$USER_HOME/.bittensor/wallets/$COLDKEY"
HOTKEY_FILE="$WALLET_DIR/hotkeys/$HOTKEY"
ECOSYSTEM_DIR="$USER_HOME/sn101-fleet-pm2"

mkdir -p "$ECOSYSTEM_DIR"

[[ "$AXON_PORT" =~ ^[0-9]+$ ]] || die "AXON_PORT must be numeric (got: $AXON_PORT)"
[[ "$AXON_PORT" -ge 1024 && "$AXON_PORT" -le 65535 ]] || die "AXON_PORT out of range (got: $AXON_PORT)"

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
log "Preflight checks"
[[ "$(id -u)" -ne 0 ]] || die "Run as ubuntu user (or any non-root sudo user), not as root"
sudo -n true 2>/dev/null || warn "sudo will prompt for password"
[[ -f /etc/os-release ]] || die "Not a Linux system"
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || warn "Expected Ubuntu, got $ID"
ok "Preflight OK (user=$USER_NAME, miner=$MINER_NAME, port=$AXON_PORT)"

# ---------------------------------------------------------------------------
# Step 1 — System deps (idempotent)
# ---------------------------------------------------------------------------
need_pkgs=false
for p in python3-venv python3-pip python3-dev build-essential pkg-config libssl-dev libffi-dev curl git ufw; do
    dpkg -s "$p" >/dev/null 2>&1 || need_pkgs=true
done
if $need_pkgs; then
    log "Step 1/8 — installing system packages"
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        python3-venv python3-pip python3-dev \
        build-essential pkg-config \
        libssl-dev libffi-dev \
        curl git ufw
fi
ok "System packages ready"

# ---------------------------------------------------------------------------
# Step 2 — Node 20 + pm2 (idempotent)
# ---------------------------------------------------------------------------
need_node=true
if command -v node >/dev/null 2>&1; then
    nm=$(node -v 2>/dev/null | sed -n 's/^v\([0-9]\+\).*/\1/p')
    [[ -n "$nm" && "$nm" -ge 20 ]] && need_node=false
fi
if $need_node; then
    log "Step 2/8 — installing Node.js 20 LTS"
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null 2>&1
    sudo apt-get install -y -qq nodejs
fi
ok "Node $(node -v)"

if ! command -v pm2 >/dev/null 2>&1; then
    log "Installing pm2"
    sudo npm install -g pm2@latest >/dev/null 2>&1
fi
ok "pm2 $(pm2 --version)"

# ---------------------------------------------------------------------------
# Step 3 — Clone repos (idempotent)
# ---------------------------------------------------------------------------
log "Step 3/8 — repos"

if [[ -d "$FLEET_DIR/.git" ]]; then
    (cd "$FLEET_DIR" && git pull --ff-only >/dev/null 2>&1) || warn "sn101-fleet pull failed (keeping local)"
else
    git clone --depth=1 "$SN101_REPO_URL" "$FLEET_DIR" >/dev/null 2>&1
fi
[[ -f "$FLEET_DIR/thin_miner.py" ]] || die "sn101-fleet looks broken (no thin_miner.py)"
ok "sn101-fleet at $FLEET_DIR"

if [[ -d "$TAG101_DIR/.git" ]]; then
    (cd "$TAG101_DIR" && git pull --ff-only >/dev/null 2>&1) || warn "tag101 pull failed (keeping local)"
else
    git clone --depth=1 "$SN101_TAG101_REPO" "$TAG101_DIR" >/dev/null 2>&1
fi
[[ -f "$TAG101_DIR/miner.py" ]] || die "tag101 looks broken (no miner.py)"
ok "tag101 at $TAG101_DIR"

# ---------------------------------------------------------------------------
# Step 4 — Python venv + minimal deps (no torch, no sentence-transformers!)
# ---------------------------------------------------------------------------
log "Step 4/8 — Python venv + bittensor + httpx"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip wheel

"$VENV_DIR/bin/pip" install --quiet --retries 5 --timeout 120 httpx pydantic
"$VENV_DIR/bin/pip" install --quiet --retries 8 --timeout 300 bittensor
# tag101's miner module imports these — bittensor doesn't auto-pull them
"$VENV_DIR/bin/pip" install --quiet --retries 5 --timeout 120 munch tenacity aiohttp
ok "Python deps installed"

# Sanity-import everything that thin_miner needs at runtime.
# Note: PYTHONPATH needs the PARENT of $TAG101_DIR (so `import tag101` works)
# plus $FLEET_DIR (so `import thin_miner` works).
TAG101_PARENT="$(dirname "$TAG101_DIR")"
PYTHONPATH="$TAG101_PARENT:$FLEET_DIR" "$VENV_DIR/bin/python" - <<'PY' || die "Import sanity check failed"
import bittensor, httpx
from tag101.tasks import build_task_registry
from tag101.protocol import TaskEnvelope
import thin_miner
print(f"  bittensor={bittensor.__version__}")
print(f"  httpx={httpx.__version__}")
print(f"  tag101 task registry: OK")
print(f"  thin_miner: OK")
PY
ok "All imports verified"

# ---------------------------------------------------------------------------
# Step 5 — Verify hotkey file exists
# ---------------------------------------------------------------------------
log "Step 5/8 — wallet check"

if [[ ! -f "$HOTKEY_FILE" ]]; then
    cat <<EOF >&2
[FAIL] Hotkey file not found:
       $HOTKEY_FILE

  The hotkey must be on this VPS before running this installer.
  From wherever you have the wallet (laptop, offline machine), run:

      scp ~/.bittensor/wallets/$COLDKEY/hotkeys/$HOTKEY \\
          $USER_NAME@$(hostname -I | awk '{print $1}'):$HOTKEY_FILE

  Also recommended (PUBLIC coldkey marker, not the private coldkey!):

      scp ~/.bittensor/wallets/$COLDKEY/coldkeypub.txt \\
          $USER_NAME@$(hostname -I | awk '{print $1}'):$WALLET_DIR/coldkeypub.txt

  Then re-run this installer.
EOF
    exit 1
fi
chmod 600 "$HOTKEY_FILE" 2>/dev/null || true
ok "Hotkey present at $HOTKEY_FILE"

# Verify the hotkey loads under bittensor (catches malformed files early)
"$VENV_DIR/bin/python" - <<PY || die "Bittensor cannot load the hotkey (file malformed?)"
import bittensor as bt
# bittensor 10+ uses Wallet (capital W); older versions used wallet (lower w).
WalletCls = getattr(bt, "Wallet", None) or getattr(bt, "wallet")
w = WalletCls(name="$COLDKEY", hotkey="$HOTKEY")
print(f"  hotkey ss58: {w.hotkey.ss58_address}")
try:
    print(f"  coldkey ss58: {w.coldkeypub.ss58_address}")
except Exception as e:
    print(f"  (coldkey marker not loadable: {e})")
PY
ok "Bittensor loaded the hotkey"

# Harden wallet perms (private files chmod 600, dirs chmod 700)
chmod 700 "$USER_HOME/.bittensor" 2>/dev/null || true
chmod 700 "$WALLET_DIR" "$WALLET_DIR/hotkeys" 2>/dev/null || true
[[ -f "$WALLET_DIR/coldkey" ]] && chmod 600 "$WALLET_DIR/coldkey"
chmod 600 "$HOTKEY_FILE"
ok "Wallet permissions hardened"

# ---------------------------------------------------------------------------
# Step 6 — Write env file (idempotent — overwrite is fine, same content each time)
# ---------------------------------------------------------------------------
log "Step 6/8 — writing env file at $ENV_FILE"
umask 077
cat > "$ENV_FILE" <<EOF
# SN101 miner env — sourced by start-miner.sh
# Owner: $USER_NAME, chmod 600
TASK_MINER_MODULE=thin_miner
SN101_SOLVER_URL=$SN101_SOLVER_URL
SN101_SOLVER_API_KEY=$SN101_SOLVER_API_KEY
SN101_SOLVER_TIMEOUT_S=5.0
SN101_VENV_BIN=$VENV_DIR/bin
SN101_TAG101_DIR=$TAG101_DIR
SN101_FLEET_DIR=$FLEET_DIR
SN101_SUBTENSOR_NETWORK=$SN101_SUBTENSOR_NETWORK
SN101_LOG_LEVEL=--logging.info

# Per-miner identity (these MUST differ between miners on the same VPS)
SN101_COLDKEY=$COLDKEY
SN101_HOTKEY=$HOTKEY
SN101_AXON_PORT=$AXON_PORT
EOF
chmod 600 "$ENV_FILE"
ok "env file written"

# ---------------------------------------------------------------------------
# Step 7 — Generate per-miner pm2 ecosystem and start
# ---------------------------------------------------------------------------
ECOSYSTEM="$ECOSYSTEM_DIR/${MINER_NAME}.config.cjs"
log "Step 7/8 — pm2 ecosystem for $MINER_NAME at port $AXON_PORT"

chmod +x "$FLEET_DIR/deploy/start-miner.sh"

cat > "$ECOSYSTEM" <<EOF
// Auto-generated by miner-install.sh — one ecosystem file per miner.
// Per-miner config (coldkey, hotkey, port, solver URL, key) lives in:
//   $ENV_FILE
// Edit that file, then: pm2 restart $MINER_NAME --update-env
module.exports = {
  apps: [{
    name: "$MINER_NAME",
    script: "$FLEET_DIR/deploy/start-miner.sh",
    interpreter: "bash",
    cwd: "$TAG101_DIR",
    autorestart: true,
    watch: false,
    max_restarts: 50,
    min_uptime: "30s",
    restart_delay: 5000,
    exp_backoff_restart_delay: 500,
    max_memory_restart: "1G",
    kill_timeout: 15000,
    env: {
      SN101_MINER_ENV: "$ENV_FILE"
    },
    out_file: "$USER_HOME/.pm2/logs/${MINER_NAME}-out.log",
    error_file: "$USER_HOME/.pm2/logs/${MINER_NAME}-err.log",
    merge_logs: true,
    time: true,
  }]
};
EOF
ok "Ecosystem written to $ECOSYSTEM"

# Open axon port in firewall
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow "$AXON_PORT/tcp" 2>&1 | tail -2 || warn "ufw rule add failed (continuing)"
    ok "ufw: allow $AXON_PORT/tcp"
fi

# Reset any stale pm2 entry with the same name
pm2 delete "$MINER_NAME" >/dev/null 2>&1 || true

# Verify port isn't already bound by something else
if ss -tln 2>/dev/null | awk '{print $4}' | grep -q ":$AXON_PORT$"; then
    die "Port $AXON_PORT is already in use by another process (not pm2). Free it or pick another port."
fi

pm2 start "$ECOSYSTEM" >/dev/null
ok "pm2 started $MINER_NAME"

# Wait up to 60s for the miner to log "axon served" or similar
log "Waiting for miner to register its axon on the chain (up to 60s)…"
for i in $(seq 1 60); do
    sleep 1
    if pm2 logs "$MINER_NAME" --lines 100 --nostream 2>/dev/null | \
       grep -qE "(serving axon|axon served|MINER_SERVING|miner serving)"; then
        ok "Miner serving (t=${i}s)"
        break
    fi
    [[ $i -eq 60 ]] && warn "No 'serving axon' line within 60s. Check: pm2 logs $MINER_NAME --lines 80 --nostream"
done

# ---------------------------------------------------------------------------
# Step 8 — Persist + boot startup
# ---------------------------------------------------------------------------
log "Step 8/8 — pm2 save + boot persistence"
pm2 save >/dev/null
sudo env PATH="$PATH:/usr/bin" pm2 startup systemd -u "$USER_NAME" --hp "$USER_HOME" >/dev/null 2>&1 || \
    warn "pm2 boot-hook install had warnings"
ok "Boot persistence configured"

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
PUBLIC_IP=$(curl -sf -m 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

cat <<EOF

${CSI}1;32m========================================================================${CSI}0m
${CSI}1;32m  MINER INSTALLED: $MINER_NAME${CSI}0m
${CSI}1;32m========================================================================${CSI}0m

  Miner name:       $MINER_NAME
  Wallet:           $COLDKEY / $HOTKEY
  Axon port:        $AXON_PORT  (public: $PUBLIC_IP:$AXON_PORT)
  Env file:         $ENV_FILE
  Ecosystem:        $ECOSYSTEM
  Solver target:    $SN101_SOLVER_URL

${CSI}1;33m------------------------------------------------------------------------${CSI}0m
${CSI}1;33m  VERIFY${CSI}0m
${CSI}1;33m------------------------------------------------------------------------${CSI}0m

  pm2 list
  pm2 logs $MINER_NAME --lines 50 --nostream

  # Confirm axon is reachable from the public internet:
  # (from another machine)
  nc -zv $PUBLIC_IP $AXON_PORT

  # Confirm hotkey is in metagraph:
  $VENV_DIR/bin/btcli subnet metagraph --netuid 101 \\
      --subtensor.network $SN101_SUBTENSOR_NETWORK | grep $HOTKEY

${CSI}1;34m------------------------------------------------------------------------${CSI}0m
${CSI}1;34m  ADD ANOTHER MINER ON THIS SAME VPS${CSI}0m
${CSI}1;34m------------------------------------------------------------------------${CSI}0m

  1. scp another hotkey file:
       scp ~/.bittensor/wallets/$COLDKEY/hotkeys/<next-hotkey> \\
           $USER_NAME@$PUBLIC_IP:$WALLET_DIR/hotkeys/

  2. Re-run miner-install.sh — pick a unique pm2 name and (optionally) a port.
     If the port is omitted, the next free port is picked automatically.
       SN101_SOLVER_URL=$SN101_SOLVER_URL \\
       SN101_SOLVER_API_KEY=<key> \\
       ./miner-install.sh sn101-miner-<next-hotkey> $COLDKEY <next-hotkey> [<port>]

EOF

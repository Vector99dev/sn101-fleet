#!/usr/bin/env bash
# pm2 entry-point for one SN101 miner.
#
# All config comes from the per-miner env file pointed to by pm2 via
# SN101_MINER_ENV. The required keys are documented below; each miner has
# its own copy of this file so multiple miners on the same VPS can run with
# their own coldkey/hotkey/port without colliding.
#
# Required env keys (set in the per-miner env file):
#   SN101_COLDKEY        - wallet/coldkey name in ~/.bittensor/wallets/
#   SN101_HOTKEY         - hotkey filename under .../hotkeys/
#   SN101_AXON_PORT      - port this miner listens on (must be unique on host)
#   SN101_SOLVER_URL     - http://solver:7311
#   SN101_SOLVER_API_KEY - the auth-gate key
#   TASK_MINER_MODULE    - "thin_miner"
#
# Optional with defaults:
#   SN101_VENV_BIN, SN101_TAG101_DIR, SN101_FLEET_DIR,
#   SN101_SUBTENSOR_NETWORK, SN101_LOG_LEVEL, SN101_STORAGE_DIR

set -e

ENV_FILE="${SN101_MINER_ENV:?SN101_MINER_ENV must point at the per-miner env file}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[start-miner] env file unreadable: $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${SN101_COLDKEY:?SN101_COLDKEY missing from $ENV_FILE}"
: "${SN101_HOTKEY:?SN101_HOTKEY missing from $ENV_FILE}"
: "${SN101_AXON_PORT:?SN101_AXON_PORT missing from $ENV_FILE}"

VENV_BIN="${SN101_VENV_BIN:-/home/ubuntu/sn101-venv/bin}"
TAG101_DIR="${SN101_TAG101_DIR:-/home/ubuntu/tag101}"
FLEET_DIR="${SN101_FLEET_DIR:-/home/ubuntu/sn101-fleet}"
SUBTENSOR_NETWORK="${SN101_SUBTENSOR_NETWORK:-finney}"
LOG_LEVEL="${SN101_LOG_LEVEL:---logging.info}"

# Per-hotkey storage so multiple miners on the same VPS don't trample each other
STORAGE_DIR="${SN101_STORAGE_DIR:-/home/ubuntu/.bittensor/sn101}/${SN101_HOTKEY}"
mkdir -p "$STORAGE_DIR"

# Make tag101 + sn101-fleet importable. tag101 package directory lives INSIDE
# $TAG101_DIR so PYTHONPATH needs its parent.
TAG101_PARENT="$(dirname "$TAG101_DIR")"
export PYTHONPATH="${TAG101_PARENT}:${FLEET_DIR}:${PYTHONPATH:-}"
export TASK_MINER_MODULE="${TASK_MINER_MODULE:-thin_miner}"

cd "$TAG101_DIR"

# exec so pm2 supervises the python process directly
exec "$VENV_BIN/python" -m tag101.miner \
    --netuid 101 \
    --subtensor.network "$SUBTENSOR_NETWORK" \
    --wallet.name "$SN101_COLDKEY" \
    --wallet.hotkey "$SN101_HOTKEY" \
    --axon.port "$SN101_AXON_PORT" \
    --neuron.storage_dir "$STORAGE_DIR" \
    $LOG_LEVEL

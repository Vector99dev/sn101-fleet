#!/usr/bin/env bash
# pm2 entry-point for a single SN101 miner.
# Loads the miner env file, then execs the Tag101 miner against the chain.
#
# Args (passed by pm2 from ecosystem):
#   $1 = wallet/coldkey name (e.g. "cold1")
#   $2 = hotkey name           (e.g. "miner1")
#   $3 = axon port             (e.g. 8091)

set -e

COLDKEY="${1:?coldkey name required}"
HOTKEY="${2:?hotkey name required}"
AXON_PORT="${3:?axon port required}"

ENV_FILE="${SN101_MINER_ENV:-/home/ubuntu/.sn101-miner.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[start-miner] env file unreadable: $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

VENV_BIN="${SN101_VENV_BIN:-/home/ubuntu/sn101-venv/bin}"
TAG101_DIR="${SN101_TAG101_DIR:-/home/ubuntu/tag101}"
FLEET_DIR="${SN101_FLEET_DIR:-/home/ubuntu/sn101-fleet}"
SUBTENSOR_NETWORK="${SN101_SUBTENSOR_NETWORK:-finney}"
NEUTRON_LOG="${SN101_LOG_LEVEL:---logging.info}"

# Per-hotkey storage so multiple miners on the same VPS don't trample each other
STORAGE_DIR="${SN101_STORAGE_DIR:-/home/ubuntu/.bittensor/sn101}/${HOTKEY}"
mkdir -p "$STORAGE_DIR"

# Make tag101 and sn101-fleet importable.
# tag101 package directory is INSIDE $TAG101_DIR, so PYTHONPATH needs its parent
# so `import tag101` resolves. $FLEET_DIR goes directly on PYTHONPATH so
# `import thin_miner` resolves (thin_miner.py lives at the top of $FLEET_DIR).
TAG101_PARENT="$(dirname "$TAG101_DIR")"
export PYTHONPATH="${TAG101_PARENT}:${FLEET_DIR}:${PYTHONPATH:-}"
export TASK_MINER_MODULE="${TASK_MINER_MODULE:-thin_miner}"

cd "$TAG101_DIR"

# exec so pm2 supervises the python process directly
exec "$VENV_BIN/python" -m tag101.miner \
    --netuid 101 \
    --subtensor.network "$SUBTENSOR_NETWORK" \
    --wallet.name "$COLDKEY" \
    --wallet.hotkey "$HOTKEY" \
    --axon.port "$AXON_PORT" \
    --neuron.storage_dir "$STORAGE_DIR" \
    $NEUTRON_LOG

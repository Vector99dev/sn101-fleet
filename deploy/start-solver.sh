#!/usr/bin/env bash
# pm2 entry-point for the SN101 solver. Loads the env file then execs uvicorn.
# Kept tiny on purpose: any state lives in env vars + the solver process.

set -e
ENV_FILE="${SN101_ENV_FILE:-/home/ubuntu/.sn101.env}"
if [[ ! -r "$ENV_FILE" ]]; then
  echo "[start-solver] env file unreadable: $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

VENV_BIN="${SN101_VENV_BIN:-/home/ubuntu/sn101-venv/bin}"
SOLVER_DIR="${SN101_SOLVER_DIR:-/home/ubuntu/sn101-fleet}"
HOST="${SN101_SOLVER_HOST:-0.0.0.0}"
PORT="${SN101_SOLVER_PORT:-7311}"

cd "$SOLVER_DIR"
export PYTHONPATH="$SOLVER_DIR:/home/ubuntu:${PYTHONPATH:-}"

# exec so pm2 supervises uvicorn directly, not a shell that wraps it.
exec "$VENV_BIN/uvicorn" solver.app:app --host "$HOST" --port "$PORT" --workers 1

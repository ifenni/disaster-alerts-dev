#!/usr/bin/env bash
# scripts/run.sh
# Cron-friendly wrapper for one disaster-alerts pipeline execution (no locking).
# Usage:
#   ./scripts/run.sh --dry-run
#   ./scripts/run.sh --print-settings

set -euo pipefail

# --- Hardening / defaults ---
umask 027
export LANG=C
export LC_ALL=C

# --- Resolve repo root & paths ---
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export DISASTER_ALERTS_ROOT="${DISASTER_ALERTS_ROOT:-$ROOT}"
LOG_DIR="$ROOT/logs"
DATA_DIR="$ROOT/data"
mkdir -p "$LOG_DIR" "$DATA_DIR"

# .env loading is handled by disaster_alerts.settings (safe key/value parsing).

# --- Timestamped log (UTC) ---
TS_UTC="$(date -u +'%Y%m%dT%H%M%SZ')"
LOG_FILE="$LOG_DIR/run_${TS_UTC}.log"

# --- Conda environment settings ---
ENV_NAME="disaster-alerts"

activate_conda_env() {
  if [ -f /opt/conda/etc/profile.d/conda.sh ]; then
    # source conda
    source /opt/conda/etc/profile.d/conda.sh
    # activate environment
    conda activate "$ENV_NAME"
    # ensure Python can find your package
    export PYTHONPATH="$DISASTER_ALERTS_ROOT"
    return 0
  else
    echo "ERROR: Conda not found at /opt/conda/etc/profile.d/conda.sh"
    return 1
  fi
}

# --- Execute pipeline ---
{
  echo "[$(date -u +'%F %TZ')] ===== disaster-alerts start ====="
  echo "root=$DISASTER_ALERTS_ROOT | args=$*"

  set +e

  if activate_conda_env; then
    # Run the main pipeline script inside conda environment
    python -m disaster_alerts "$@"
    EC=$?
  elif command -v python >/dev/null 2>&1; then
    # fallback to system Python
    export PYTHONPATH="$DISASTER_ALERTS_ROOT"
    python -m disaster_alerts "$@"
    EC=$?
  else
    echo "ERROR: No conda env '$ENV_NAME' or system 'python' found."
    echo "       Create the env with: conda env create -f environment.yml"
    EC=1
  fi

  set -e
  echo "[$(date -u +'%F %TZ')] ===== disaster-alerts end (exit=$EC) ====="
  exit "$EC"

} 2>&1 | tee -a "$LOG_FILE"

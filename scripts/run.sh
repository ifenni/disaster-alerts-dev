#!/usr/bin/env bash
# scripts/run.sh
# Run one pipeline execution of disaster-alerts under the intended conda env.
# Pass any CLI args through, e.g.:
#   ./scripts/run.sh --dry-run --print-settings

set -euo pipefail

# --- Resolve repo root ---
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export DISASTER_ALERTS_ROOT="${DISASTER_ALERTS_ROOT:-$ROOT}"

# Optional: override config dir via env before running (uncomment & edit if desired)
# export DISASTER_ALERTS_CONFIG_DIR="$ROOT/config"

# --- Load .env if present (exports YAGMAIL_USER / YAGMAIL_APP_PASSWORD etc.) ---
if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a
  source "$ROOT/.env"
  set +a
fi

# --- Ensure data/logs directories exist (cron safety) ---
mkdir -p "$ROOT/data" "$ROOT/logs"

# --- Prefer conda env if available; otherwise fall back to system python ---
ENV_NAME="disaster-alerts"

run_with_conda() {
  # Initialize conda for this shell, if possible
  if command -v conda >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    eval "$(conda shell.bash hook)"
    if conda env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
      conda run -n "$ENV_NAME" python -m disaster_alerts "$@"
      return $?
    fi
  fi
  return 1
}

run_with_system_python() {
  if command -v python >/dev/null 2>&1; then
    python -m disaster_alerts "$@"
    return $?
  fi
  return 1
}

cd "$ROOT"

if run_with_conda "$@"; then
  exit 0
elif run_with_system_python "$@"; then
  exit 0
else
  echo "ERROR: Could not find conda env '$ENV_NAME' or a system 'python'." >&2
  echo "       Create the env with:  conda env create -f environment.yml" >&2
  exit 1
fi

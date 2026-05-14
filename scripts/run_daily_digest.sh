#!/bin/zsh

set -euo pipefail

ROOT_DIR="/Users/dahuang/CascadeProjects/knowledge-auto-update"
LOG_DIR="${HOME}/Library/Logs/knowledge-auto-update"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

mkdir -p "${LOG_DIR}"

{
  echo "[${STAMP}] Starting run-daily"
  cd "${ROOT_DIR}"
  /usr/bin/env python3 -m jike_collection run-daily "$@"
  echo "[${STAMP}] Finished run-daily"
} >> "${LOG_DIR}/daily.log" 2>> "${LOG_DIR}/daily.err.log"

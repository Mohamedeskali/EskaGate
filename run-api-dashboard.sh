#!/usr/bin/env bash
# Launch the local API Test Console on Ubuntu and other Linux distributions.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it with: sudo apt install python3"
  exit 1
fi

PORT="${1:-8000}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "Usage: $0 [port between 1 and 65535]" >&2
  exit 2
fi

exec "$PYTHON_BIN" api_web_dashboard_v2.py --port "$PORT" --open

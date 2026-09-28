#!/usr/bin/env bash
# EskaGate app launcher: starts the dashboard in the background (if not
# already running) and opens it in the browser. Use "stop" to shut it down.
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"
PORT="${ESKALI_API_PORT:-8000}"
URL="http://127.0.0.1:$PORT"
STATE_DIR="$HOME/.api-test-console"
PID_FILE="$STATE_DIR/eskali_api.pid"
LOG_FILE="$STATE_DIR/eskali_api.log"
PYTHON_BIN="${PYTHON_BIN:-python3}"

mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"

# Message in the language picked in the page (i18n/<lang>.json, default Arabic):
#   t <key> [name=value ...] [lang=xx]. Without Python the line is read from the JSON file.
t() {
  local out arg lang=""
  if out="$("$PYTHON_BIN" "$SCRIPT_DIR/i18n.py" "$@" 2>/dev/null)"; then printf '%s\n' "$out"; return; fi
  for arg in "${@:2}"; do [[ $arg == lang=* ]] && lang="${arg#lang=}"; done
  [[ -n $lang ]] || lang="$(sed -n 's/.*"lang": *"\([a-z]*\)".*/\1/p' "${API_CONSOLE_HOME:-$HOME/.api-test-console}/ui-settings.json" 2>/dev/null)"
  [[ -f "$SCRIPT_DIR/i18n/$lang.json" ]] || lang=ar
  out="$(sed -n "s/^ *\"$1\": \"\(.*\)\",\{0,1\}\$/\1/p" "$SCRIPT_DIR/i18n/$lang.json")"
  for arg in "${@:2}"; do out="${out//\{${arg%%=*}\}/${arg#*=}}"; done
  printf '%s\n' "$out"
}

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -i "$SCRIPT_DIR/assets/eskali_api_icon.png" "EskaGate" "$1"
  echo "$1"
}

is_running() {
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

port_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null
}

# True when the dashboard is already serving on the port (e.g. started from a terminal).
is_dashboard() {
  "$PYTHON_BIN" - "$URL" <<'EOF' >/dev/null 2>&1
import sys, urllib.request
body = urllib.request.urlopen(sys.argv[1], timeout=2).read(4096).decode("utf-8", "ignore")
sys.exit(0 if ("EskaGate" in body or "ESKALI API" in body or "API Monitor Console" in body) else 1)
EOF
}

case "${1:-start}" in
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE"
      notify "$(t launcher.stopped)"
    elif is_dashboard; then
      notify "$(t launcher.from_terminal)"
    else
      rm -f "$PID_FILE"
      notify "$(t launcher.not_running)"
    fi
    exit 0
    ;;
  start) ;;
  *) echo "Usage: $0 [start|stop]" >&2; exit 2 ;;
esac

if ! is_running && ! is_dashboard; then
  if port_open; then
    notify "$(t launcher.port_busy port="$PORT")"
    exit 1
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    notify "$(t launcher.no_python)"
    exit 1
  fi
  cd "$SCRIPT_DIR"
  nohup "$PYTHON_BIN" api_web_dashboard_v2.py --port "$PORT" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 50); do
    port_open && break
    if ! is_running; then
      notify "$(t launcher.failed log="$LOG_FILE")"
      rm -f "$PID_FILE"
      exit 1
    fi
    sleep 0.1
  done
fi

xdg-open "$URL" >/dev/null 2>&1 &

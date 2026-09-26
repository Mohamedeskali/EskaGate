#!/usr/bin/env bash
# ESKALI_API app launcher: starts the dashboard in the background (if not
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

notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send -i "$SCRIPT_DIR/assets/eskali_api_icon.png" "ESKALI_API" "$1"
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
sys.exit(0 if ("ESKALI API" in body or "API Monitor Console" in body) else 1)
EOF
}

case "${1:-start}" in
  stop)
    if is_running; then
      kill "$(cat "$PID_FILE")" && rm -f "$PID_FILE"
      notify "توقف ESKALI_API."
    elif is_dashboard; then
      notify "ESKALI_API شاعل من الطرفية، وقفو تما بـ Ctrl+C."
    else
      rm -f "$PID_FILE"
      notify "ESKALI_API ماشي شاعل."
    fi
    exit 0
    ;;
  start) ;;
  *) echo "Usage: $0 [start|stop]" >&2; exit 2 ;;
esac

if ! is_running && ! is_dashboard; then
  if port_open; then
    notify "البورت $PORT مستعمل من برنامج آخر."
    exit 1
  fi
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    notify "Python 3 ماكاينش. ثبتو بـ: sudo apt install python3"
    exit 1
  fi
  cd "$SCRIPT_DIR"
  nohup "$PYTHON_BIN" api_web_dashboard_v2.py --port "$PORT" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 50); do
    port_open && break
    if ! is_running; then
      notify "ESKALI_API ما بغاش يخدم. شوف: $LOG_FILE"
      rm -f "$PID_FILE"
      exit 1
    fi
    sleep 0.1
  done
fi

xdg-open "$URL" >/dev/null 2>&1 &

#!/usr/bin/env bash
# One-line install / update of EskaGate on Ubuntu (and other Linux desktops):
#   curl -fsSL https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.sh | bash
# Installs to ~/EskaGate (or $ESKAGATE_DIR), adds the apps-menu entry and desktop icon,
# then starts the app. Never runs sudo, and never touches your data in ~/.api-test-console.
set -euo pipefail

REPO_URL="${ESKAGATE_REPO:-https://github.com/Mohamedeskali/EskaGate.git}"
INSTALL_DIR="${ESKAGATE_DIR:-$HOME/EskaGate}"
MIN_PY="3.8"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Everything runs from main(), so a download cut off halfway through does nothing.
main() {
  local missing=()
  command -v python3 >/dev/null 2>&1 || missing+=(python3)
  command -v git >/dev/null 2>&1 || missing+=(git)
  if ((${#missing[@]})); then
    fail "missing: ${missing[*]}. Install it, then run this command again:
    sudo apt update && sudo apt install -y ${missing[*]}"
  fi
  python3 -c "import sys; sys.exit(sys.version_info < (${MIN_PY/./,}))" \
    || fail "EskaGate needs Python $MIN_PY or newer; found $(python3 -V 2>&1)."

  local before="" after=""
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    say "Updating $INSTALL_DIR"
    before="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    GIT_TERMINAL_PROMPT=0 git -C "$INSTALL_DIR" pull --ff-only \
      || fail "could not update $INSTALL_DIR (local changes?). Fix it with git, or move the folder away and run this again."
    after="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
  elif [[ -e "$INSTALL_DIR" ]]; then
    fail "$INSTALL_DIR exists but is not a git checkout. Move it away (or set ESKAGATE_DIR) and run this again."
  else
    say "Downloading EskaGate to $INSTALL_DIR"
    GIT_TERMINAL_PROMPT=0 git clone --depth 1 "$REPO_URL" "$INSTALL_DIR" \
      || fail "could not download $REPO_URL."
  fi

  say "Adding EskaGate to the apps menu and the desktop"
  bash "$INSTALL_DIR/install_eskali_api.sh"

  # After an update, restart a copy already running from this folder so the new code is used.
  local pid_file="$HOME/.api-test-console/eskali_api.pid" pid
  if [[ -n "$before" && "$before" != "$after" && -f "$pid_file" ]]; then
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null && [[ "$(readlink -f "/proc/$pid/cwd")" == "$(readlink -f "$INSTALL_DIR")" ]]; then
      say "Restarting the running copy to load the update"
      bash "$INSTALL_DIR/eskali_api_launcher.sh" stop >/dev/null
    fi
  fi

  say "Starting EskaGate"
  bash "$INSTALL_DIR/eskali_api_launcher.sh" start
  say "Done. EskaGate runs at http://127.0.0.1:${ESKALI_API_PORT:-8000}"
  echo "    Open it later from the apps menu or the desktop icon."
  echo "    Stop it:   $INSTALL_DIR/eskali_api_launcher.sh stop"
  echo "    Update it: run the same install command again."
}

main "$@"

#!/usr/bin/env bash
# One-line install / update of EskaGate on Ubuntu (and other Linux desktops):
#   curl -fsSL https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.sh | bash
# Installs to ~/EskaGate (or $ESKAGATE_DIR), adds the apps-menu entry and desktop icon,
# then starts the app. Run it again to update. Never runs sudo, and never touches your data
# in ~/.api-test-console. To uninstall:
#   curl -fsSL https://raw.githubusercontent.com/Mohamedeskali/EskaGate/main/get.sh | bash -s -- --uninstall
set -euo pipefail

REPO_URL="${ESKAGATE_REPO:-https://github.com/Mohamedeskali/EskaGate.git}"
INSTALL_DIR="${ESKAGATE_DIR:-$HOME/EskaGate}"
MIN_PY="3.8"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# PID of the copy started by the launcher, if it runs from INSTALL_DIR (not some other folder).
running_pid() {
  local pid_file="$HOME/.api-test-console/eskali_api.pid" pid
  [[ -f "$pid_file" ]] || return 1
  pid="$(cat "$pid_file")"
  kill -0 "$pid" 2>/dev/null && [[ "$(readlink -f "/proc/$pid/cwd")" == "$(readlink -f "$INSTALL_DIR")" ]] && echo "$pid"
}

uninstall() {
  local dir apps desktop entry
  dir="$(readlink -f "$INSTALL_DIR" 2>/dev/null || echo "$INSTALL_DIR")"
  [[ -n "$dir" && "$dir" != "/" && "$dir" != "$(readlink -f "$HOME")" ]] || fail "refusing to remove $INSTALL_DIR."
  if [[ -e "$dir" && ! -f "$dir/api_web_dashboard_v2.py" ]]; then
    fail "$dir does not look like EskaGate; not removing it."
  fi
  if running_pid >/dev/null; then
    say "Stopping EskaGate"
    bash "$dir/eskali_api_launcher.sh" stop >/dev/null
  fi
  # Menu entry, desktop icon and icon file, only when they belong to this install.
  apps="$HOME/.local/share/applications"
  desktop="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  entry="$apps/eskali-api.desktop"
  if [[ -f "$entry" ]] && grep -qF "$dir/eskali_api_launcher.sh" "$entry"; then
    say "Removing the apps-menu entry and the desktop icon"
    rm -f "$entry" "$desktop/ESKALI_API.desktop" "$HOME/.local/share/icons/hicolor/256x256/apps/eskali-api.png"
    update-desktop-database "$apps" >/dev/null 2>&1 || true
  elif [[ -f "$entry" ]]; then
    say "Keeping the apps-menu entry: it belongs to an EskaGate in another folder."
  fi
  if [[ -e "$dir" ]]; then
    say "Removing $dir"
    rm -rf -- "$dir"
  else
    say "$dir is not there; nothing else to remove."
  fi
  say "EskaGate is uninstalled."
  echo "    Your keys and settings are still in ~/.api-test-console."
  echo "    To delete them too: rm -rf ~/.api-test-console"
}

# Everything runs from main(), so a download cut off halfway through does nothing.
main() {
  case "${1:-}" in
    --uninstall) uninstall; return ;;
    "") ;;
    *) fail "unknown option '$1'. Use no option to install or update, or --uninstall." ;;
  esac

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
  if [[ -n "$before" && "$before" != "$after" ]] && running_pid >/dev/null; then
    say "Restarting the running copy to load the update"
    bash "$INSTALL_DIR/eskali_api_launcher.sh" stop >/dev/null
  fi

  say "Starting EskaGate"
  bash "$INSTALL_DIR/eskali_api_launcher.sh" start
  say "Done. EskaGate runs at http://127.0.0.1:${ESKALI_API_PORT:-8000}"
  echo "    Open it later from the apps menu or the desktop icon."
  echo "    Stop it:   $INSTALL_DIR/eskali_api_launcher.sh stop"
  echo "    Update it: run the same install command again."
}

main "$@"

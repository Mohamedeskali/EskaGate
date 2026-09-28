#!/usr/bin/env bash
# Installs EskaGate as an application (apps menu + desktop icon).
# Run again after moving this folder. Use "--uninstall" to remove it.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
ENTRY_NAME="eskali-api.desktop"
PYTHON_BIN="${PYTHON_BIN:-python3}"

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

if [[ "${1:-}" == "--uninstall" ]]; then
  "$SCRIPT_DIR/eskali_api_launcher.sh" stop >/dev/null 2>&1 || true
  rm -f "$APPS_DIR/$ENTRY_NAME" "$DESKTOP_DIR/ESKALI_API.desktop" "$ICON_DIR/eskali-api.png"
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  t install.removed
  exit 0
fi

chmod +x "$SCRIPT_DIR/eskali_api_launcher.sh"
mkdir -p "$APPS_DIR" "$ICON_DIR"
cp "$SCRIPT_DIR/assets/eskali_api_icon.png" "$ICON_DIR/eskali-api.png"

LAUNCHER="$SCRIPT_DIR/eskali_api_launcher.sh"
# Name/Comment in every language; the desktop picks the one matching the system locale.
comments="" stop_names=""
for l in ar en fr; do
  comments+="Comment[$l]=$(t install.comment lang=$l)"$'\n'
  stop_names+="Name[$l]=$(t install.stop_action lang=$l)"$'\n'
done
ENTRY="[Desktop Entry]
Type=Application
Version=1.0
Name=EskaGate
Comment=$(t install.comment lang=ar)
${comments}Exec=\"$LAUNCHER\" start
Path=$SCRIPT_DIR
Icon=$ICON_DIR/eskali-api.png
Terminal=false
Categories=Development;
Keywords=api;gateway;eskali;dashboard;
StartupNotify=false
Actions=stop;

[Desktop Action stop]
Name=$(t install.stop_action lang=ar)
${stop_names}Exec=\"$LAUNCHER\" stop
"

printf '%s' "$ENTRY" >"$APPS_DIR/$ENTRY_NAME"
chmod +x "$APPS_DIR/$ENTRY_NAME"

# Shortcut on the desktop.
target="$DESKTOP_DIR/ESKALI_API.desktop"
if [[ -d "$DESKTOP_DIR" ]]; then
  printf '%s' "$ENTRY" >"$target"
  chmod +x "$target"
  gio set "$target" metadata::trusted true >/dev/null 2>&1 || true
fi

update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

t install.done
t install.menu
t install.desktop path="$DESKTOP_DIR/ESKALI_API.desktop"
t install.stop_hint

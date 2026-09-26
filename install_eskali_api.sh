#!/usr/bin/env bash
# Installs ESKALI_API as an application (apps menu + desktop icon).
# Run again after moving this folder. Use "--uninstall" to remove it.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
ENTRY_NAME="eskali-api.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  "$SCRIPT_DIR/eskali_api_launcher.sh" stop >/dev/null 2>&1 || true
  rm -f "$APPS_DIR/$ENTRY_NAME" "$DESKTOP_DIR/ESKALI_API.desktop" "$ICON_DIR/eskali-api.png"
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  echo "تحيد ESKALI_API من التطبيقات."
  exit 0
fi

chmod +x "$SCRIPT_DIR/eskali_api_launcher.sh"
mkdir -p "$APPS_DIR" "$ICON_DIR"
cp "$SCRIPT_DIR/assets/eskali_api_icon.png" "$ICON_DIR/eskali-api.png"

LAUNCHER="$SCRIPT_DIR/eskali_api_launcher.sh"
ENTRY="[Desktop Entry]
Type=Application
Version=1.0
Name=ESKALI_API
Comment=لوحة اختبار المفاتيح و AI Gateway المحلي
Comment[en]=API test console and local AI gateway
Exec=\"$LAUNCHER\" start
Path=$SCRIPT_DIR
Icon=eskali-api
Terminal=false
Categories=Development;
Keywords=api;gateway;eskali;dashboard;
StartupNotify=false
Actions=stop;

[Desktop Action stop]
Name=إيقاف ESKALI_API
Name[en]=Stop ESKALI_API
Exec=\"$LAUNCHER\" stop
"

printf '%s' "$ENTRY" >"$APPS_DIR/$ENTRY_NAME"
chmod +x "$APPS_DIR/$ENTRY_NAME"

# Shortcut on the desktop.
target="$DESKTOP_DIR/ESKALI_API.desktop"
if [[ -d "$DESKTOP_DIR" ]]; then
  printf '%s' "${ENTRY/Icon=eskali-api/Icon=$ICON_DIR/eskali-api.png}" >"$target"
  chmod +x "$target"
  gio set "$target" metadata::trusted true >/dev/null 2>&1 || true
fi

update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "تثبت ESKALI_API ✔"
echo "  - غادي تلقاه فقائمة التطبيقات (Show Apps) باسم ESKALI_API"
echo "  - أيقونة على سطح المكتب: $DESKTOP_DIR/ESKALI_API.desktop"
echo "  - باش توقفو: كليك يمين على الأيقونة ← إيقاف ESKALI_API"

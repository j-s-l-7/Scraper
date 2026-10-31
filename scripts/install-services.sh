#!/bin/bash
# Install (or reinstall) macOS LaunchAgents for the current user.
#   com.scraper.console  http://127.0.0.1:8787, kept alive, starts at login
#   com.scraper.refresh  scripts/refresh.sh every 15 min
# Usage: scripts/install-services.sh [console] [refresh]   (default: both)
#        scripts/install-services.sh --uninstall
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
mkdir -p "$DEST" "$ROOT/logs"
chmod +x "$ROOT/scripts/refresh.sh"
UNINSTALL=0
if [ "${1:-}" = "--uninstall" ]; then UNINSTALL=1; set --; fi
NAMES=("$@"); [ ${#NAMES[@]} -eq 0 ] && NAMES=(console refresh)
for short in "${NAMES[@]}"; do
  name="com.scraper.$short"
  launchctl bootout "gui/$(id -u)/$name" 2>/dev/null || true
  rm -f "$DEST/$name.plist"
  if [ $UNINSTALL = 1 ]; then echo "removed $name"; continue; fi
  sed "s|__ROOT__|$ROOT|g" "$ROOT/scripts/launchd/$name.plist" > "$DEST/$name.plist"
  plutil -lint "$DEST/$name.plist" >/dev/null
  launchctl bootstrap "gui/$(id -u)" "$DEST/$name.plist"
  echo "installed $name"
done

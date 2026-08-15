#!/usr/bin/env bash
# Install (or reinstall) the approver as a macOS launchd user agent.
#
#   ./launchd/install_approver.sh            install / restart
#   ./launchd/install_approver.sh uninstall  stop and remove
#
# The approver is the ONLY process that submits orders, and it only does so after
# you tap "Go ahead" in Telegram. It runs under your user account, on your Mac.
set -euo pipefail

LABEL="com.trading-agent.approver"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
TEMPLATE="$HERE/$LABEL.plist.template"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

[[ -x "$ROOT/.venv/bin/trader" ]] || { echo "no .venv/bin/trader — run: python3 -m venv .venv && .venv/bin/pip install -e ." ; exit 1; }
[[ -f "$ROOT/.env" ]] || { echo "no .env — copy .env.example to .env and fill it in first."; exit 1; }

mkdir -p "$PLIST_DIR" "$ROOT/data"
sed "s#__PROJECT_ROOT__#$ROOT#g" "$TEMPLATE" > "$PLIST"

# Restart cleanly if already loaded.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "installed $LABEL"
echo "  status : launchctl print gui/$(id -u)/$LABEL | grep -E 'state|pid'"
echo "  logs   : tail -f $ROOT/data/approver.log"
echo "  remove : $0 uninstall"

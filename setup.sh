#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$SCRIPT_DIR/com.user.surfmonitor.plist"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LAUNCHD_DEST="$LAUNCHD_DIR/com.user.surfmonitor.plist"

echo "=== Surf Monitor Setup ==="

# 1. Install dependencies
echo "→ Installing Python dependencies..."
python3 -m pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

# 2. Check ntfy_topic is configured
TOPIC=$(python3 -c "import json; c=json.load(open('$SCRIPT_DIR/config.json')); print(c['ntfy_topic'])")
if echo "$TOPIC" | grep -q "change-me"; then
  echo ""
  echo "⚠️  You must set a unique ntfy_topic in config.json before continuing."
  echo "   Open: $SCRIPT_DIR/config.json"
  echo "   Change 'ntfy_topic' to something personal, e.g. 'kaimana-surf-12345'"
  echo "   Then re-run this script."
  exit 1
fi

# 3. Install launchd agent
echo "→ Installing launchd agent (auto-start on login)..."
mkdir -p "$LAUNCHD_DIR"
cp "$PLIST" "$LAUNCHD_DEST"

# Unload if already loaded (ignore errors)
launchctl unload "$LAUNCHD_DEST" 2>/dev/null || true
launchctl load "$LAUNCHD_DEST"

echo ""
echo "✓ Done! Surf Monitor is running in the background."
echo ""
echo "Next steps:"
echo "  1. Install the 'ntfy' app on your phone (iOS or Android)"
echo "  2. Subscribe to topic: $TOPIC"
echo "  3. You'll get a 'Surf Monitor Active' notification to confirm it's working"
echo ""
echo "Useful commands:"
echo "  View log:    tail -f ~/.surf_monitor.log"
echo "  Stop:        launchctl unload $LAUNCHD_DEST"
echo "  Restart:     launchctl unload $LAUNCHD_DEST && launchctl load $LAUNCHD_DEST"
echo "  Run manually: cd $SCRIPT_DIR && python3 surf_monitor.py"

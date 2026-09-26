#!/bin/zsh
# Stops and removes the background service. Your data (releases.db) is kept unless you pass --purge.
LABEL="com.bandcamp-scanner"
APP_DIR="$HOME/Library/Application Support/Bandcamp Scanner"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "$APP_DIR"
  echo "Removed service and data. The Keychain item 'bandcamp-scanner' was left in place (delete it in Keychain Access if you like)."
else
  echo "Service removed. Data kept in: $APP_DIR"
fi

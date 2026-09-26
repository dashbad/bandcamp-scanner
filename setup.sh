#!/bin/zsh
# Installs Bandcamp Release Scanner as a background service that starts at login.
# Re-run any time to update the installed code or change settings.
set -euo pipefail

SRC="${0:A:h}"
APP_DIR="$HOME/Library/Application Support/Bandcamp Scanner"
LABEL="com.bandcamp-scanner"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="$(command -v python3)"
SERVICE="bandcamp-scanner"

mkdir -p "$APP_DIR/static" "$HOME/Library/LaunchAgents"
cp "$SRC/scanner.py" "$APP_DIR/"
cp "$SRC/static/index.html" "$APP_DIR/static/"

# --- Gmail address -----------------------------------------------------------
CURRENT=""
[[ -f "$APP_DIR/config.json" ]] && CURRENT="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("gmail_address",""))' "$APP_DIR/config.json")"
read "ADDR?Gmail address [${CURRENT:-you@gmail.com}]: "
ADDR="${ADDR:-$CURRENT}"
[[ -z "$ADDR" ]] && { echo "A Gmail address is required."; exit 1; }

"$PYTHON" - "$APP_DIR/config.json" "$ADDR" <<'EOF'
import json, sys, os
path, addr = sys.argv[1], sys.argv[2]
cfg = json.load(open(path)) if os.path.exists(path) else {}
cfg.setdefault("port", 8765)
cfg.setdefault("poll_seconds", 60)
cfg.setdefault("backfill_days", 2)
cfg.setdefault("notify", True)
cfg["gmail_address"] = addr
json.dump(cfg, open(path, "w"), indent=2)
EOF

# --- App password (stored in the login Keychain, never on disk) --------------
if security find-generic-password -s "$SERVICE" -a "$ADDR" >/dev/null 2>&1; then
  read "REPLACE?An app password for $ADDR is already in Keychain. Replace it? [y/N]: "
else
  REPLACE=y
fi
if [[ "$REPLACE" == [yY]* ]]; then
  echo
  echo "Create a Gmail app password at https://myaccount.google.com/apppasswords"
  echo "(requires 2-Step Verification). Also make sure IMAP is enabled in Gmail settings."
  read -s "PW?Paste the 16-character app password (input hidden): "
  echo
  PW="${PW// /}"
  security add-generic-password -U -s "$SERVICE" -a "$ADDR" -l "Bandcamp Release Scanner" -w "$PW"
  unset PW
  echo "Saved to Keychain."
fi

# --- Verify the login works ---------------------------------------------------
echo "Testing Gmail connection…"
"$PYTHON" - "$ADDR" <<'EOF' || { echo "Gmail login failed — check the address/app password and that IMAP is enabled."; exit 1; }
import imaplib, subprocess, sys
addr = sys.argv[1]
pw = subprocess.run(["security", "find-generic-password", "-s", "bandcamp-scanner", "-a", addr, "-w"],
                    capture_output=True, text=True, check=True).stdout.strip()
m = imaplib.IMAP4_SSL("imap.gmail.com"); m.login(addr, pw); m.logout()
print("Gmail login OK.")
EOF

# --- LaunchAgent --------------------------------------------------------------
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>$APP_DIR/scanner.py</string></array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/scanner.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/scanner.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

PORT="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["port"])' "$APP_DIR/config.json")"
echo
echo "Bandcamp Release Scanner is running: http://localhost:$PORT"
echo "The first sync backfills the last 2 days of release emails and takes a few minutes."
sleep 2
open "http://localhost:$PORT"

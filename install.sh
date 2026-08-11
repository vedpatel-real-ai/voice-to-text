#!/bin/bash
# install.sh — one-time setup for VoiceToText as an auto-launching menu
# bar app, running from the venv interpreter directly.
#
# For the nicer path (a real VoiceToText.app bundle, so TCC shows
# "VoiceToText" instead of "python3"), run this first, then install_app.sh.
set -euo pipefail

APP_DIR="$HOME/voice-to-text"
PLIST_LABEL="com.ved.voicetotext"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
SUPPORT_DIR="$HOME/Library/Application Support/VoiceToText"

# The app is no longer a single file — every module must be present or it
# will fail at import time with a confusing traceback.
REQUIRED=(
  voice_to_text.py
  config.py
  diagnostics.py
  statemachine.py
  audio.py
  transcription.py
  injection.py
  pill.py
  singleinstance.py
)

echo "▶ Setting up in $APP_DIR ..."
mkdir -p "$APP_DIR"

missing=0
for f in "${REQUIRED[@]}"; do
  if [ ! -f "$APP_DIR/$f" ]; then
    echo "❌ missing $f"
    missing=1
  fi
done
if [ "$missing" -ne 0 ]; then
  echo ""
  echo "   Copy all of the files listed above into $APP_DIR, then re-run."
  exit 1
fi

cd "$APP_DIR"
mkdir -p "$SUPPORT_DIR"

if [ ! -d "venv" ]; then
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip
pip install rumps pynput sounddevice numpy mlx-whisper pillow \
            pyobjc-framework-Cocoa pyobjc-framework-Quartz \
            pyobjc-framework-ApplicationServices
deactivate

echo "▶ Creating LaunchAgent so this runs automatically at login ..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP_DIR}/venv/bin/python3</string>
        <string>-u</string>
        <string>${APP_DIR}/voice_to_text.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>LimitLoadToSessionType</key>
    <string>Aqua</string>
    <key>StandardOutPath</key>
    <string>${SUPPORT_DIR}/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>${SUPPORT_DIR}/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo ""
echo "✅ Installed and started. Look for the icon in your menu bar."
echo ""
echo "⚠️  IMPORTANT — permissions:"
echo "   Go to System Settings → Privacy & Security and add this exact binary"
echo "   to Microphone, Accessibility, AND Input Monitoring:"
echo ""
echo "     ${APP_DIR}/venv/bin/python3"
echo ""
echo "   Accessibility specifically is what lets the Left-Option hotkey"
echo "   register at all — Input Monitoring alone is not enough for a"
echo "   modifier-only key. Use 'Check permissions' in the menu bar to"
echo "   verify; the app now tells you instead of failing silently."
echo ""
echo "Logs and settings:  ${SUPPORT_DIR}"
echo "  app log:    voicetotext.log      (rotating, always written)"
echo "  launchd:    launchd.{out,err}.log"
echo "  crashes:    crash.log"
echo "  settings:   settings.json"
echo "  vocabulary: vocabulary.txt"
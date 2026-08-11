#!/bin/bash
# install_app.sh — package voice_to_text.py as a proper VoiceToText.app
# bundle, so macOS TCC grants Accessibility / Input Monitoring /
# Microphone to a real app identity instead of a bare Homebrew python3.
#
# Run this AFTER install.sh has set up $HOME/voice-to-text/venv.
set -euo pipefail

APP_DIR="$HOME/voice-to-text"
PLIST_LABEL="com.ved.voicetotext"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
BUNDLE_NAME="VoiceToText.app"
INSTALLED_APP="$HOME/Applications/${BUNDLE_NAME}"
EXECUTABLE="${INSTALLED_APP}/Contents/MacOS/VoiceToText"
SUPPORT_DIR="$HOME/Library/Application Support/VoiceToText"

cd "$APP_DIR"

if [ ! -f "setup.py" ]; then
  echo "❌ setup.py not found in $APP_DIR — copy it here first, then re-run."
  exit 1
fi

for f in config.py diagnostics.py statemachine.py audio.py \
         transcription.py injection.py pill.py singleinstance.py; do
  if [ ! -f "$f" ]; then
    echo "❌ $f not found in $APP_DIR — the app is no longer a single file."
    exit 1
  fi
done

mkdir -p "$SUPPORT_DIR"

echo "▶ Unloading any existing LaunchAgent so nothing is running mid-build..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true

echo "▶ Installing py2app into the venv..."
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade py2app

echo "▶ Cleaning any previous build output..."
rm -rf build dist

echo "▶ Building VoiceToText.app (alias mode — links back to this venv)..."
python setup.py py2app -A
deactivate

if [ ! -d "dist/${BUNDLE_NAME}" ]; then
  echo "❌ Build did not produce dist/${BUNDLE_NAME}. Check the py2app output above."
  exit 1
fi

echo "▶ Installing to ${INSTALLED_APP} ..."
mkdir -p "$HOME/Applications"
rm -rf "$INSTALLED_APP"
cp -R "dist/${BUNDLE_NAME}" "$INSTALLED_APP"

if [ ! -x "$EXECUTABLE" ]; then
  echo "❌ Expected executable not found at ${EXECUTABLE}"
  exit 1
fi

echo "▶ Writing LaunchAgent ..."
mkdir -p "$HOME/Library/LaunchAgents"
# Two deliberate changes from the previous version, both from audit §12:
#
#  1. StandardOutPath / StandardErrorPath. launchd DISCARDS stdout and
#     stderr for jobs that don't redirect them, which meant the crash
#     dumps, the faulthandler output and the FATAL startup handler were
#     all invisible in the one context where they matter most.
#
#  2. We launch the bundle's executable directly instead of going through
#     `/usr/bin/open -a`. `open` hands off to Launch Services and returns,
#     so the app is not launchd's child and the redirection above would
#     capture nothing. Running Contents/MacOS/VoiceToText keeps the app
#     bundle identity that TCC cares about *and* gives launchd the stdio.
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${EXECUTABLE}</string>
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

echo ""
echo "✅ Built and installed ${INSTALLED_APP}"
echo ""
echo "⚠️  IMPORTANT — do this next, in order:"
echo ""
echo "  1. Launch it once by hand, NOT via the LaunchAgent yet, so macOS's"
echo "     permission dialogs actually fire:"
echo ""
echo "       open '${INSTALLED_APP}'"
echo ""
echo "  2. System Settings → Privacy & Security → Accessibility, Input"
echo "     Monitoring, and Microphone. You should see 'VoiceToText' (not"
echo "     python3) in each — enable it in all three."
echo ""
echo "  3. Use 'Check permissions' in the menu bar to confirm, then quit"
echo "     and relaunch once so the grants take effect."
echo ""
echo "  4. Test: hold Left Option, speak, release — it should type the text."
echo "     Press esc while holding ⌥ to cancel a recording."
echo ""
echo "  5. Once that works, enable auto-start at login:"
echo ""
echo "       launchctl load ${PLIST_PATH}"
echo ""
echo "Useful commands going forward:"
echo "  Stop:    launchctl unload ${PLIST_PATH}"
echo "  Start:   launchctl load ${PLIST_PATH}"
echo "  Rebuild: ./install_app.sh"
echo "  Logs:    tail -f '${SUPPORT_DIR}/voicetotext.log'"
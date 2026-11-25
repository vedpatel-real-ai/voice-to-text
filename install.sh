#!/bin/bash
# The only supported installer for VoiceToText 3.x.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
SUPPORT_DIR="$HOME/Library/Application Support/VoiceToText"
RUNTIME_DIR="$SUPPORT_DIR/runtime"
APP_PATH="$HOME/Applications/VoiceToText.app"
EXECUTABLE="$APP_PATH/Contents/MacOS/VoiceToText"
PLIST_LABEL="com.ved.voicetotext"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
PYTHON_BIN="${VTT_PYTHON:-/opt/homebrew/opt/python@3.12/bin/python3.12}"
MODE="${1:-install}"

write_launch_agent() {
  mkdir -p "$HOME/Library/LaunchAgents" "$SUPPORT_DIR"
  /usr/bin/python3 - "$PLIST_PATH" "$EXECUTABLE" "$SUPPORT_DIR" <<'PY'
import plistlib, sys
path, executable, support = sys.argv[1:]
payload = {
    "Label": "com.ved.voicetotext",
    "ProgramArguments": [executable],
    "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    "RunAtLoad": True,
    "KeepAlive": False,
    "ProcessType": "Interactive",
    "LimitLoadToSessionType": "Aqua",
    "StandardOutPath": support + "/launchd.out.log",
    "StandardErrorPath": support + "/launchd.err.log",
}
with open(path, "wb") as stream:
    plistlib.dump(payload, stream, sort_keys=False)
PY
}

stop_existing() {
  launchctl bootout "gui/$(id -u)/$PLIST_LABEL" 2>/dev/null || true
  if [ -x "$EXECUTABLE" ]; then
    while IFS= read -r process_id; do
      [ -n "$process_id" ] && kill "$process_id" 2>/dev/null || true
    done < <(pgrep -f "^${EXECUTABLE}$" || true)
  fi
}

if [ "$MODE" = "--enable-login" ]; then
  if [ ! -x "$EXECUTABLE" ]; then
    echo "VoiceToText is not installed. Run ./install.sh first."
    exit 1
  fi
  # Hand ownership to launchd cleanly. Starting the job while the manually
  # launched app still owns the single-instance lock makes the launch job
  # exit immediately and leaves login startup looking enabled but inert.
  stop_existing
  write_launch_agent
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
  launchctl kickstart "gui/$(id -u)/$PLIST_LABEL"
  echo "Login startup enabled for $APP_PATH"
  exit 0
fi

if [ "$MODE" != "install" ]; then
  echo "Usage: ./install.sh [--enable-login]"
  exit 2
fi

required=(
  voice_to_text.py audio.py buildinfo.py config.py diagnostics.py hotkey.py
  injection.py permissions.py pill.py singleinstance.py statemachine.py
  transcription.py setup.py requirements.lock smoketest.py
)
for name in "${required[@]}"; do
  if [ ! -f "$SOURCE_DIR/$name" ]; then
    echo "Missing required source file: $name"
    exit 1
  fi
done

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Python 3.12 was not found at $PYTHON_BIN"
  echo "Install it with: brew install python@3.12"
  exit 1
fi

python_executable=$("$PYTHON_BIN" -c 'import sys; print(sys.executable)')
python_version=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
python_arch=$("$PYTHON_BIN" -c 'import platform; print(platform.machine())')
python_prefix=$("$PYTHON_BIN" -c 'import sys; print(sys.prefix)')
echo "Interpreter:  $python_executable"
echo "Python:       $python_version"
echo "Architecture: $python_arch"
echo "Prefix:       $python_prefix"
if [ "$python_version" != "3.12" ] || [ "$python_arch" != "arm64" ]; then
  echo "VoiceToText requires arm64 Python 3.12."
  exit 1
fi
if [[ "$python_executable" == *conda* ]] || [[ "$python_prefix" == *conda* ]]; then
  echo "Conda is not supported for the production runtime."
  exit 1
fi
# The selected interpreter is verified non-Conda. Remove ambient activation
# variables so they cannot leak into venv creation or dependency installation.
unset CONDA_PREFIX CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER PYTHONPATH PYTHONHOME

stop_existing
mkdir -p "$SUPPORT_DIR" "$HOME/Applications"
staging="$SUPPORT_DIR/.runtime-staging.$$"
app_staging="$HOME/Applications/.VoiceToText.app.staging.$$"
runtime_previous="$SUPPORT_DIR/.runtime-previous"
runtime_swapped=0
install_complete=0
cleanup_staging() {
  rm -rf "$staging" "$app_staging"
  if [ "$runtime_swapped" -eq 1 ] && [ "$install_complete" -eq 0 ]; then
    rm -rf "$RUNTIME_DIR"
    [ -d "$runtime_previous" ] && mv "$runtime_previous" "$RUNTIME_DIR"
  fi
}
trap cleanup_staging EXIT
mkdir -p "$staging"
for name in "${required[@]}" requirements-dev.lock; do
  [ -f "$SOURCE_DIR/$name" ] && /usr/bin/ditto "$SOURCE_DIR/$name" "$staging/$name"
done

rm -rf "$runtime_previous"
if [ -d "$RUNTIME_DIR" ]; then
  mv "$RUNTIME_DIR" "$runtime_previous"
fi
mv "$staging" "$RUNTIME_DIR"
staging="$SUPPORT_DIR/.staging-consumed"
runtime_swapped=1

"$PYTHON_BIN" -m venv "$RUNTIME_DIR/.venv"
"$RUNTIME_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --requirement "$RUNTIME_DIR/requirements.lock"
cd "$RUNTIME_DIR"
"$RUNTIME_DIR/.venv/bin/python" setup.py py2app -A
if [ ! -d "$RUNTIME_DIR/dist/VoiceToText.app" ]; then
  echo "py2app did not create VoiceToText.app"
  exit 1
fi

rm -rf "$app_staging"
/usr/bin/ditto "$RUNTIME_DIR/dist/VoiceToText.app" "$app_staging"
signing_identity="${VTT_SIGN_IDENTITY:--}"
/usr/bin/codesign --force --deep --sign "$signing_identity" "$app_staging"

rm -rf "$APP_PATH"
mv "$app_staging" "$APP_PATH"
app_staging="$HOME/Applications/.app-consumed"
rm -rf "$runtime_previous"
install_complete=1

write_launch_agent
echo "Installed VoiceToText to $APP_PATH"
echo "Runtime: $RUNTIME_DIR (pinned Python 3.12 environment)"
if [ "$signing_identity" = "-" ]; then
  echo "Signing: ad-hoc. A Developer ID can be supplied with VTT_SIGN_IDENTITY."
else
  echo "Signing: $signing_identity"
fi
echo
echo "Launching once for permission setup..."
/usr/bin/open "$APP_PATH"
echo "Grant VoiceToText Microphone, Accessibility, and Input Monitoring access."
echo "Then run: ./install.sh --enable-login"
echo "Diagnostics: $RUNTIME_DIR/.venv/bin/python $RUNTIME_DIR/smoketest.py"

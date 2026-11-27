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
  /usr/bin/python3 - "$1" "$EXECUTABLE" "$SUPPORT_DIR" <<'PY'
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

case "$MODE" in
  install|--enable-login) ;;
  *) echo "Usage: ./install.sh [--enable-login]" >&2; exit 2 ;;
esac

required=(
  voice_to_text.py audio.py buildinfo.py config.py diagnostics.py hotkey.py
  injection.py permissions.py pill.py singleinstance.py statemachine.py
  transcription.py setup.py prepare_bundle.py requirements.lock bootstrap.lock build.lock smoketest.py
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
umask 077
mkdir -p "$SUPPORT_DIR/releases" "$HOME/Applications"
lock="$SUPPORT_DIR/.install-lock"
if ! mkdir "$lock"; then
  echo "Installation is already active or interrupted. Inspect .install-lock before retrying." >&2
  exit 1
fi
domain="gui/$(id -u)"
job="$domain/$PLIST_LABEL"
app_tmp=""
release=""
previous=""
loaded=0
manual_pid=""
switching=0
success=0
new_app=0
new_runtime=0
new_plist=0
new_job=0

finish() {
  result=$?
  trap - EXIT INT TERM HUP
  set +e
  rollback_ok=1
  if [[ $success == 0 && $switching == 1 ]]; then
    if [[ $new_job == 1 ]] && launchctl print "$job" >/dev/null 2>&1; then
      launchctl bootout "$job" || rollback_ok=0
    fi
    if [[ $new_plist == 1 ]]; then rm -f "$PLIST_PATH" || rollback_ok=0; fi
    if [[ $new_app == 1 ]]; then mv "$APP_PATH" "$app_tmp/rejected.app" || rollback_ok=0; fi
    if [[ $new_runtime == 1 ]]; then rm -f "$RUNTIME_DIR" || rollback_ok=0; fi
    if [[ -e "$previous/runtime" || -L "$previous/runtime" ]]; then mv "$previous/runtime" "$RUNTIME_DIR" || rollback_ok=0; fi
    if [[ -d "$app_tmp/old.app" ]]; then mv "$app_tmp/old.app" "$APP_PATH" || rollback_ok=0; fi
    if [[ $loaded == 1 ]] && ! launchctl print "$job" >/dev/null 2>&1; then
      launchctl bootstrap "$domain" "$PLIST_PATH" || rollback_ok=0
    elif [[ -n "$manual_pid" ]] && ! kill -0 "$manual_pid" 2>/dev/null; then
      /usr/bin/open "$APP_PATH" || rollback_ok=0
    fi
  fi
  if [[ $rollback_ok == 0 ]]; then
    echo "Rollback incomplete. Preserved recovery files in $previous and $app_tmp." >&2
    exit 1
  fi
  if [[ $success == 0 && -n "$release" ]]; then rm -rf "$release"; fi
  if [[ -n "$previous" ]]; then rm -rf "$previous"; fi
  if [[ -n "$app_tmp" ]]; then rm -rf "$app_tmp"; fi
  rmdir "$lock"
  exit "$result"
}
trap finish EXIT
trap 'exit 1' INT TERM HUP

previous=$(mktemp -d "$SUPPORT_DIR/.upgrade.XXXXXX")
app_tmp=$(mktemp -d "$HOME/Applications/.VoiceToText.XXXXXX")
if [[ "$MODE" == install ]]; then
  # An alias and a venv embed absolute paths. Build once at their final location.
  release=$(mktemp -d "$SUPPORT_DIR/releases/runtime.XXXXXX")
  for name in "${required[@]}"; do /usr/bin/ditto "$SOURCE_DIR/$name" "$release/$name"; done
  "$PYTHON_BIN" -m venv "$release/.venv"
  python="$release/.venv/bin/python"
  "$python" -m pip install --disable-pip-version-check --require-hashes -r "$release/bootstrap.lock"
  # rumps ships as source. Build it against the already verified setuptools
  # wheel to avoid pip resolving an unpinned build-isolation dependency.
  "$python" -m pip install --disable-pip-version-check --require-hashes -r "$release/build.lock"
  "$python" -m pip install --disable-pip-version-check --no-build-isolation --require-hashes -r "$release/requirements.lock"
  "$python" -m pip check
  cd "$release"
  "$python" setup.py py2app -A
  candidate="$app_tmp/VoiceToText.app"
  /usr/bin/ditto "$release/dist/VoiceToText.app" "$candidate"
  "$python" prepare_bundle.py "$candidate"
  identity="${VTT_SIGN_IDENTITY:--}"
  /usr/bin/codesign --force --sign "$identity" "$candidate/Contents/MacOS/python3.12"
  /usr/bin/codesign --force --sign "$identity" "$candidate"
  /usr/bin/codesign --verify --strict --deep "$candidate"
  fingerprint=$("$python" -c 'from buildinfo import source_fingerprint; print(source_fingerprint()[:16])')
  built=$("$candidate/Contents/MacOS/VoiceToText" --check-bundle) || exit 1
  [[ -n "$fingerprint" && "$built" == "$fingerprint" ]] || exit 1
  ln -s "$release" "$previous/new-runtime"
else
  [[ -x "$EXECUTABLE" ]] || { echo "Install VoiceToText first." >&2; exit 1; }
fi

if launchctl print "$job" >/dev/null 2>&1; then
  loaded=1
  [[ -f "$PLIST_PATH" ]] || { echo "Loaded job has no restorable plist." >&2; exit 1; }
fi
# Match the exact executable path even when launchd has a loaded but exited job.
if [[ -x "$EXECUTABLE" ]]; then
  manual_pid=$("$PYTHON_BIN" - "$EXECUTABLE" <<'PY'
import subprocess, sys
rows = subprocess.check_output(["/bin/ps", "-axo", "pid=,comm="], text=True)
for row in rows.splitlines():
    fields = row.strip().split(None, 1)
    if len(fields) == 2 and fields[1] == sys.argv[1]:
        print(fields[0])
        break
PY
)
fi
switching=1
if [[ $loaded == 1 ]]; then
  launchctl bootout "$job"
fi
if [[ -n "$manual_pid" ]] && kill -0 "$manual_pid" 2>/dev/null; then
  kill -TERM "$manual_pid" 2>/dev/null || ! kill -0 "$manual_pid" 2>/dev/null
  for _ in {1..50}; do
    kill -0 "$manual_pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$manual_pid" 2>/dev/null; then echo "Previous app is still closing." >&2; exit 1; fi
fi

if [[ "$MODE" == install ]]; then
  if [[ -e "$RUNTIME_DIR" || -L "$RUNTIME_DIR" ]]; then mv "$RUNTIME_DIR" "$previous/runtime"; fi
  mv "$previous/new-runtime" "$RUNTIME_DIR"
  new_runtime=1
  if [[ -d "$APP_PATH" ]]; then mv "$APP_PATH" "$app_tmp/old.app"; fi
  mv "$candidate" "$APP_PATH"
  new_app=1
  installed=$("$EXECUTABLE" --check-bundle) || exit 1
  [[ "$installed" == "$fingerprint" ]] || exit 1
fi
if [[ "$MODE" == --enable-login && ! -e "$PLIST_PATH" ]]; then
  write_launch_agent "$previous/agent.plist"
  mv "$previous/agent.plist" "$PLIST_PATH"
  new_plist=1
fi
if [[ $loaded == 1 || "$MODE" == --enable-login ]]; then
  new_job=1
  launchctl bootstrap "$domain" "$PLIST_PATH"
elif [[ -n "$manual_pid" || ! -d "$app_tmp/old.app" ]]; then
  /usr/bin/open "$APP_PATH"
fi
success=1
echo "VoiceToText ready at $APP_PATH"

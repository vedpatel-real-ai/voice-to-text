"""Exercise the actual shell transaction against a disposable filesystem and service."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

SHIM = r'''#!/bin/bash
set -eu
name=${0##*/}
fail() {
  if [[ "$FAULT" == "$1" && ! -f "$FIXTURE/failed" ]]; then
    touch "$FIXTURE/failed"
    exit 23
  fi
}
case "$name" in
  ditto) cp -R "$1" "$2" ;;
  codesign) fail sign; [[ "$1" != --verify ]] || fail verify ;;
  open) fail launch; touch "$FIXTURE/opened" ;;
  launchctl)
    case "$1" in
      print) [[ -f "$FIXTURE/loaded" ]] ;;
      bootout) fail bootout; rm "$FIXTURE/loaded" ;;
      bootstrap) [[ "$FAULT" != service_unavailable ]] || exit 24; fail bootstrap; touch "$FIXTURE/loaded" ;;
      *) exit 99 ;;
    esac ;;
  mv)
    case "$1" in
      */new-runtime) fail runtime-install ;;
      */runtime) [[ "$2" != */.upgrade.*/runtime ]] || fail runtime-backup ;;
      */VoiceToText.app) if [[ "$2" == */old.app ]]; then fail app-backup; else fail app-install; fi ;;
      */agent.plist) fail plist ;;
    esac
    /bin/mv "$@" ;;
  native|python)
    if [[ "$1" == -c ]]; then
      case "$2" in
        *platform.machine*) echo arm64 ;;
        *version_info*) echo 3.12 ;;
        *source_fingerprint*) echo fixture ;;
        *) echo "$FIXTURE/native" ;;
      esac
    elif [[ "$1" == - ]]; then
      cat >/dev/null
    elif [[ "$1 $2" == "-m venv" ]]; then
      fail venv; mkdir -p "$3/bin"; cp "$0" "$3/bin/python"
    elif [[ "$1 $2" == "-m pip" ]]; then
      fail pip
    elif [[ "$1" == setup.py ]]; then
      fail build
      mkdir -p dist/VoiceToText.app/Contents/MacOS
      cat > dist/VoiceToText.app/Contents/MacOS/VoiceToText <<'BIN'
#!/bin/bash
case "$0" in
  */Applications/VoiceToText.app/*) [[ "$FAULT" != health ]] || exit 2 ;;
esac
echo fixture
BIN
      chmod +x dist/VoiceToText.app/Contents/MacOS/VoiceToText
    elif [[ "$1" == prepare_bundle.py ]]; then fail prepare
    else exit 98; fi ;;
  *) exit 97 ;;
esac
'''


def layout(tmp_path):
    home = tmp_path / "profile"
    support = home / "Library/Application Support/VoiceToText"
    app = home / "Applications/VoiceToText.app"
    agent = home / "Library/LaunchAgents/com.ved.voicetotext.plist"
    for directory in (support/"runtime", app, agent.parent):
        directory.mkdir(parents=True)
    (app/"original").touch()
    (support/"runtime"/"original").touch()
    (support/"settings.json").write_text("fixture preferences")
    (support/"vocabulary.txt").write_text("fixture vocabulary")
    bin_dir = tmp_path/"bin"
    bin_dir.mkdir()
    for cmd in ("ditto", "codesign", "open", "launchctl", "mv", "native"):
        path=bin_dir/cmd
        path.write_text(SHIM)
        path.chmod(0o700)
    source = tmp_path/"source"
    source.mkdir()
    for f in list(ROOT.glob("*.py"))+list(ROOT.glob("*.lock")):
        shutil.copy(f, source/f.name)
    script=(ROOT/"install.sh").read_text()
    for cmd in ("ditto", "codesign", "open"):
        script=script.replace("/usr/bin/"+cmd, str(bin_dir/cmd))
    (source/"install.sh").write_text(script)
    env={"HOME":str(home), "PATH":str(bin_dir)+":/usr/bin:/bin",
         "FIXTURE":str(tmp_path), "VTT_PYTHON":str(bin_dir/"native")}
    return app,support,agent,source,env


@pytest.mark.parametrize("fault", ["venv","pip","build","prepare","sign","verify","runtime-backup",
                                  "runtime-install","app-backup","app-install","health","bootout","bootstrap","success"])
def test_upgrade_transaction(tmp_path, fault):
    app,support,agent,source,env=layout(tmp_path)
    agent.write_text("original login configuration")
    loaded=tmp_path/"loaded"
    loaded.touch()
    result=subprocess.run(["bash",str(source/"install.sh")], env={**env,"FAULT":fault},capture_output=True,text=True)
    assert (result.returncode==0)==(fault=="success"), result.stdout+result.stderr
    assert (app/"original").exists()==(fault!="success")
    assert (support/"runtime"/"original").exists()==(fault!="success")
    assert agent.read_text()=="original login configuration" and loaded.exists()
    assert (support/"settings.json").read_text()=="fixture preferences"
    assert (support/"vocabulary.txt").read_text()=="fixture vocabulary"


def test_upgrade_preserves_disabled_login(tmp_path):
    app,support,agent,source,env=layout(tmp_path)
    subprocess.run(["bash",str(source/"install.sh")],env={**env,"FAULT":""},check=True)
    assert not agent.exists() and not (tmp_path/"loaded").exists()
    assert (support/"runtime").is_symlink() and not (tmp_path/"opened").exists()


@pytest.mark.parametrize("fault", ["plist","bootstrap","success"])
def test_enable_login_rolls_back_new_plist(tmp_path, fault):
    app,support,agent,source,env=layout(tmp_path)
    executable=app/"Contents/MacOS/VoiceToText"
    executable.parent.mkdir(parents=True)
    executable.touch(mode=0o700)
    result=subprocess.run(["bash",str(source/"install.sh"),"--enable-login"],env={**env,"FAULT":fault})
    assert (result.returncode==0)==(fault=="success")
    assert agent.exists()==(fault=="success") and (tmp_path/"loaded").exists()==(fault=="success")
    assert (app/"original").exists() and (support/"runtime/original").exists()


def test_clean_install_launch_failure_removes_candidate(tmp_path):
    app,support,agent,source,env=layout(tmp_path)
    shutil.rmtree(app)
    shutil.rmtree(support/"runtime")
    result=subprocess.run(["bash",str(source/"install.sh")],env={**env,"FAULT":"launch"})
    assert result.returncode
    assert not app.exists() and not os.path.lexists(support/"runtime")
    assert not agent.exists()



def test_launch_service_outage_leaves_files_in_place_and_reports_partial_rollback(tmp_path):
    app, support, agent, source, env = layout(tmp_path)
    agent.write_text("original login configuration")
    (tmp_path / "loaded").touch()
    result = subprocess.run(["bash", str(source / "install.sh")],
                            env={**env, "FAULT": "service_unavailable"},
                            capture_output=True, text=True)
    assert result.returncode and "Rollback incomplete" in result.stderr
    assert (app / "original").exists() and (support / "runtime/original").exists()
    assert agent.read_text() == "original login configuration"
    assert not (tmp_path / "loaded").exists()
    assert (support / ".install-lock").is_dir()

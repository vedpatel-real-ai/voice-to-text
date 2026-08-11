"""
Builds VoiceToText.app — a proper macOS app bundle wrapping voice_to_text.py.

We use py2app's ALIAS mode (`python setup.py py2app -A`), which does NOT
copy/freeze dependencies into the bundle; it symlinks back to this venv's
site-packages. That matters because mlx-whisper ships compiled
Metal/Accelerate extensions that are fragile to re-freeze — alias mode
keeps using the exact working venv you already have, and just gives the
process a real, stable identity so macOS TCC (Accessibility / Input
Monitoring / Microphone) registers it properly.

Audit §10 note, now resolved: alias mode was also the only reason the old
icon cache worked, because __file__ still pointed at the writable script
in $HOME. All writable state (icon, settings, logs, vocabulary) now lives
in ~/Library/Application Support/VoiceToText, so a future non-alias or
PyInstaller build will not break on a read-only bundle.

Run from inside the activated venv:
    pip install py2app
    python setup.py py2app -A
"""
from setuptools import setup

APP = ['voice_to_text.py']

# The app is no longer one file. In alias mode these are importable from
# the script's own directory, but listing them keeps a future frozen build
# honest — py2app will then actually bundle them.
MODULES = [
    'config',
    'diagnostics',
    'statemachine',
    'audio',
    'transcription',
    'injection',
    'pill',
    'singleinstance',
]

OPTIONS = {
    'argv_emulation': False,
    'includes': MODULES,
    'plist': {
        'CFBundleName': 'VoiceToText',
        'CFBundleDisplayName': 'VoiceToText',
        'CFBundleIdentifier': 'com.ved.voicetotext',
        'CFBundleShortVersionString': '2.0.0',
        'CFBundleVersion': '2.0.0',
        # Hides the Dock icon / Cmd-Tab entry — menu-bar-only, matching
        # the runtime NSApplicationActivationPolicyAccessory call.
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
        # Launch Services refuses to open a second copy of the bundle,
        # which complements the flock guard in singleinstance.py (§2).
        'LSMultipleInstancesProhibited': True,
        'NSMicrophoneUsageDescription':
            'VoiceToText needs microphone access to transcribe your speech.',
        'NSAppleEventsUsageDescription':
            'VoiceToText needs to send keystrokes to type transcribed text.',
    },
}

setup(
    app=APP,
    py_modules=MODULES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
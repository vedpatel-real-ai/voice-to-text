"""Build the local-production VoiceToText.app alias bundle.

MLX native extensions remain in the pinned runtime venv. The supported
installer creates that runtime in Application Support before invoking this
file, so the app does not depend on a developer checkout or Conda.
"""

from setuptools import setup

from buildinfo import APP_VERSION, BUNDLE_ID

APP = ["voice_to_text.py"]
MODULES = [
    "audio",
    "buildinfo",
    "config",
    "diagnostics",
    "hotkey",
    "injection",
    "permissions",
    "pill",
    "singleinstance",
    "statemachine",
    "transcription",
]

OPTIONS = {
    "argv_emulation": False,
    "includes": MODULES,
    "plist": {
        "CFBundleName": "VoiceToText",
        "CFBundleDisplayName": "VoiceToText",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSUIElement": True,
        "LSMultipleInstancesProhibited": True,
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": (
            "VoiceToText records only while you hold Left Option and transcribes locally."
        ),
    },
}

setup(
    app=APP,
    py_modules=MODULES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app==0.28.10"],
)

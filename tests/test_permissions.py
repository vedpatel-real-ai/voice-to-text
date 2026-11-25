import sys
from types import SimpleNamespace

import permissions
from permissions import PermissionState


def test_microphone_permission_mapping(monkeypatch):
    fake = SimpleNamespace(
        AVCaptureDevice=SimpleNamespace(authorizationStatusForMediaType_=lambda _kind: 3),
        AVMediaTypeAudio="audio",
    )
    monkeypatch.setitem(sys.modules, "AVFoundation", fake)
    assert permissions.microphone_state() is PermissionState.GRANTED


def test_quartz_denied_classification(monkeypatch):
    fake = SimpleNamespace(CGPreflightListenEventAccess=lambda: False)
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    assert permissions.input_monitoring_state() is PermissionState.DENIED

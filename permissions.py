"""Native macOS permission inspection for the exact running identity."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict

from diagnostics import get_logger

log = get_logger("permissions")


class PermissionState(Enum):
    GRANTED = "granted"
    DENIED = "denied"
    NOT_DETERMINED = "not_determined"
    RESTRICTED = "restricted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class PermissionReport:
    microphone: PermissionState
    accessibility: PermissionState
    input_monitoring: PermissionState
    event_posting: PermissionState

    @property
    def missing(self) -> Dict[str, PermissionState]:
        values = {
            "Microphone": self.microphone,
            "Accessibility": self.accessibility,
            "Input Monitoring": self.input_monitoring,
            "Event Posting": self.event_posting,
        }
        return {name: state for name, state in values.items() if state is not PermissionState.GRANTED}


def accessibility_state() -> PermissionState:
    try:
        from ApplicationServices import AXIsProcessTrusted

        return PermissionState.GRANTED if AXIsProcessTrusted() else PermissionState.DENIED
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.warning("Accessibility status unavailable: %s", exc)
        return PermissionState.UNAVAILABLE


def _quartz_preflight(function_name: str) -> PermissionState:
    try:
        import Quartz

        function = getattr(Quartz, function_name)
        return PermissionState.GRANTED if function() else PermissionState.DENIED
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.warning("%s unavailable: %s", function_name, exc)
        return PermissionState.UNAVAILABLE


def input_monitoring_state() -> PermissionState:
    return _quartz_preflight("CGPreflightListenEventAccess")


def event_posting_state() -> PermissionState:
    return _quartz_preflight("CGPreflightPostEventAccess")


def microphone_state() -> PermissionState:
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        status = int(AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio))
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.warning("Microphone authorization status unavailable: %s", exc)
        return PermissionState.UNAVAILABLE
    return {
        0: PermissionState.NOT_DETERMINED,
        1: PermissionState.RESTRICTED,
        2: PermissionState.DENIED,
        3: PermissionState.GRANTED,
    }.get(status, PermissionState.UNAVAILABLE)


def inspect_permissions() -> PermissionReport:
    return PermissionReport(
        microphone=microphone_state(),
        accessibility=accessibility_state(),
        input_monitoring=input_monitoring_state(),
        event_posting=event_posting_state(),
    )


def request_accessibility_once() -> None:
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions

        AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.debug("Accessibility prompt unavailable: %s", exc)


def request_input_monitoring_once() -> None:
    try:
        import Quartz

        request = getattr(Quartz, "CGRequestListenEventAccess")
        request()
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.debug("Input Monitoring prompt unavailable: %s", exc)


def request_microphone_once() -> None:
    if microphone_state() is not PermissionState.NOT_DETERMINED:
        return
    try:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio

        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeAudio,
            lambda granted: log.info("Microphone permission response: %s", bool(granted)),
        )
    except (ImportError, AttributeError, RuntimeError) as exc:
        log.debug("Microphone prompt unavailable: %s", exc)

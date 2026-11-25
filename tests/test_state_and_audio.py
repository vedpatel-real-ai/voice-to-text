import json
import time

import numpy as np

from audio import _linear_resample, analyze_speech
from config import Settings
from statemachine import Mode, StateMachine


def test_state_machine_valid_path():
    state = StateMachine()
    assert state.transition(Mode.RECORDING, expect=Mode.IDLE)
    assert state.transition(Mode.TRANSCRIBING, expect=Mode.RECORDING)
    assert state.transition(Mode.DONE, expect=Mode.TRANSCRIBING)
    assert state.transition(Mode.IDLE, expect=Mode.DONE)


def test_state_machine_rejects_duplicate_start_and_stop():
    state = StateMachine()
    assert state.transition(Mode.RECORDING, expect=Mode.IDLE)
    assert not state.transition(Mode.RECORDING, expect=Mode.IDLE)
    assert state.transition(Mode.TRANSCRIBING, expect=Mode.RECORDING)
    assert not state.transition(Mode.TRANSCRIBING, expect=Mode.RECORDING)


def test_cancel_is_terminal_then_idle():
    state = StateMachine()
    state.transition(Mode.RECORDING)
    assert state.transition(Mode.CANCELLED, expect=Mode.RECORDING)
    assert state.transition(Mode.IDLE, expect=Mode.CANCELLED)


def test_quiet_speech_is_not_rejected():
    rate = 16000
    audio = np.zeros(rate, np.float32)
    t = np.arange(int(rate * 0.35)) / rate
    audio[4000 : 4000 + len(t)] = 0.005 * np.sin(2 * np.pi * 220 * t)
    result = analyze_speech(audio, rate, absolute_floor=0.001, trim_floor=0.0015)
    assert result.speech_present
    assert result.audio.size >= len(t)


def test_observed_builtin_mic_level_is_not_rejected():
    """Regression for live capture RMS~0.002 / peak~0.02 being gated."""
    rate = 16000
    rng = np.random.default_rng(17)
    audio = rng.normal(0, 0.00008, rate).astype(np.float32)
    t = np.arange(int(rate * 0.55), dtype=np.float32) / rate
    phrase = 0.0030 * np.sin(2 * np.pi * 185 * t)
    phrase += 0.0012 * np.sin(2 * np.pi * 370 * t)
    audio[2400 : 2400 + phrase.size] += phrase.astype(np.float32)
    result = analyze_speech(
        audio,
        rate,
        absolute_floor=0.0008,
        trim_floor=0.0010,
    )
    assert 0.001 < float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) < 0.003
    assert result.speech_present


def test_room_silence_is_rejected():
    rng = np.random.default_rng(7)
    audio = rng.normal(0, 0.0002, 16000).astype(np.float32)
    result = analyze_speech(audio, 16000)
    assert not result.speech_present


def test_short_click_does_not_count_as_speech():
    audio = np.zeros(16000, np.float32)
    audio[8000:8020] = 0.8
    assert not analyze_speech(audio, 16000).speech_present


def test_vad_keeps_padding():
    audio = np.zeros(32000, np.float32)
    audio[12000:16000] = 0.02
    result = analyze_speech(audio, 16000, padding_ms=200)
    assert result.speech_present
    assert result.audio.size >= 4000 + 2 * 3000


def test_resample_preserves_duration():
    audio = np.ones(48000, np.float32)
    result = _linear_resample(audio, 48000, 16000)
    assert len(result) == 16000


def test_settings_round_trip_and_unknown_key_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"sound_enabled": False, "unknown": 123}))
    settings = Settings(str(path))
    assert settings.get("sound_enabled") is False
    assert "unknown" not in settings.as_dict()
    settings.set("sound_enabled", True)
    assert Settings(str(path)).get("sound_enabled") is True


def test_legacy_audio_thresholds_are_migrated(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "silence_rms_threshold": 0.006,
                "trim_threshold": 0.01,
                "injection_mode": "auto",
            }
        )
    )
    settings = Settings(str(path))
    assert settings.get("settings_schema_version") == 4
    assert settings.get("silence_rms_threshold") == 0.0008
    assert settings.get("trim_threshold") == 0.0010
    assert settings.get("injection_mode") == "paste"
    assert settings.get("model") == "mlx-community/whisper-small.en-mlx"
    assert settings.get("decode_temperature") == 0.0
    assert settings.get("option_key_mode") == "hold"

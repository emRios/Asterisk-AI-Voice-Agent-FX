import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from api.local_ai import (  # noqa: E402
    SwitchModelRequest,
    _build_local_ai_env_and_yaml_updates,
    _build_local_ai_ws_switch_payload,
)


def test_ws_payload_faster_whisper_uses_stt_config_model() -> None:
    req = SwitchModelRequest(model_type="stt", backend="faster_whisper", model_path="base")
    assert _build_local_ai_ws_switch_payload(req) == {
        "type": "switch_model",
        "stt_backend": "faster_whisper",
        "stt_config": {"model": "base"},
    }


def test_env_and_yaml_updates_faster_whisper_persists_model_id() -> None:
    req = SwitchModelRequest(model_type="stt", backend="faster_whisper", model_path="base")
    env_updates, yaml_updates = _build_local_ai_env_and_yaml_updates(req)
    assert env_updates["LOCAL_STT_BACKEND"] == "faster_whisper"
    assert env_updates["FASTER_WHISPER_MODEL"] == "base"
    assert yaml_updates["stt_backend"] == "faster_whisper"
    assert yaml_updates["stt_model"] == "base"


def test_ws_payload_melotts_uses_tts_config_voice() -> None:
    req = SwitchModelRequest(model_type="tts", backend="melotts", model_path="EN-US")
    assert _build_local_ai_ws_switch_payload(req) == {
        "type": "switch_model",
        "tts_backend": "melotts",
        "tts_config": {"voice": "EN-US"},
    }


def test_env_and_yaml_updates_melotts_persists_voice_id() -> None:
    req = SwitchModelRequest(model_type="tts", backend="melotts", model_path="EN-US")
    env_updates, yaml_updates = _build_local_ai_env_and_yaml_updates(req)
    assert env_updates["LOCAL_TTS_BACKEND"] == "melotts"
    assert env_updates["MELOTTS_VOICE"] == "EN-US"
    assert yaml_updates["tts_backend"] == "melotts"
    assert yaml_updates["tts_voice"] == "EN-US"

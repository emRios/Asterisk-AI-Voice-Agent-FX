from __future__ import annotations

import os
from typing import Any, Dict

from config import LocalAIConfig


def detect_capabilities(config: LocalAIConfig) -> Dict[str, Any]:
    capabilities: Dict[str, Any] = {
        "vosk": True,
        "sherpa": False,
        "kroko_embedded": False,
        "faster_whisper": False,
        "piper": True,
        "kokoro": False,
        "melotts": False,
        "llama": True,
    }

    try:
        import sherpa_onnx  # noqa: F401
        capabilities["sherpa"] = True
    except ImportError:
        pass

    kroko_binary = "/usr/local/bin/kroko-server"
    if os.path.exists(kroko_binary):
        capabilities["kroko_embedded"] = True

    try:
        import kokoro  # noqa: F401
        capabilities["kokoro"] = True
    except ImportError:
        if config.kokoro_mode == "api" and config.kokoro_api_base_url:
            capabilities["kokoro"] = True
        elif os.path.exists(config.kokoro_model_path):
            capabilities["kokoro"] = True

    try:
        from faster_whisper import WhisperModel  # noqa: F401
        capabilities["faster_whisper"] = True
    except ImportError:
        pass

    try:
        from melo.api import TTS  # noqa: F401
        capabilities["melotts"] = True
    except ImportError:
        pass

    return capabilities


from __future__ import annotations

# Optional backend imports. These may be excluded in minimal images built with
# INCLUDE_VOSK/INCLUDE_LLAMA/INCLUDE_PIPER false. Guard imports so the server
# can still start for other modes/backends.
try:
    from vosk import Model as VoskModel, KaldiRecognizer  # type: ignore
except ImportError:  # pragma: no cover
    VoskModel = None  # type: ignore[assignment]
    KaldiRecognizer = None  # type: ignore[assignment]

try:
    from faster_whisper import WhisperModel as FasterWhisperModel  # type: ignore
except ImportError:  # pragma: no cover
    FasterWhisperModel = None  # type: ignore[assignment]

try:
    from llama_cpp import Llama  # type: ignore
except ImportError:  # pragma: no cover
    Llama = None  # type: ignore[assignment]

try:
    from piper import PiperVoice  # type: ignore
except ImportError:  # pragma: no cover
    PiperVoice = None  # type: ignore[assignment]

try:
    from melo.api import TTS as MeloTTS  # type: ignore
except ImportError:  # pragma: no cover
    MeloTTS = None  # type: ignore[assignment]


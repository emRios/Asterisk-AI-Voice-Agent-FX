"""
# Milestone7: Deepgram cloud component adapters for configurable pipelines.

This module introduces concrete implementations for Deepgram STT and TTS adapters
used by the pipeline orchestrator. Both adapters honour pipeline/provider options,
support latency-aware logging, and integrate with Deepgram's WebSocket (STT) and
REST (TTS) APIs.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import websockets
from websockets.client import WebSocketClientProtocol

from ..audio import convert_pcm16le_to_target_format, mulaw_to_pcm16le, resample_audio
from ..config import AppConfig, DeepgramProviderConfig
from ..logging_config import get_logger
from .base import STTComponent, TTSComponent

logger = get_logger(__name__)


# Shared helpers -----------------------------------------------------------------


def _normalize_ws_url(base_url: Optional[str]) -> str:
    default = "wss://api.deepgram.com/v1/listen"
    if not base_url:
        return default
    parsed = urlparse(base_url)
    if parsed.scheme in ("ws", "wss"):
        if not parsed.path or parsed.path == "/":
            parsed = parsed._replace(path="/v1/listen")
        return urlunparse(parsed)
    if parsed.scheme in ("http", "https"):
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = parsed.path if parsed.path and parsed.path != "/" else "/v1/listen"
        return urlunparse(parsed._replace(scheme=scheme, path=path))
    # Assume bare host
    return f"wss://{base_url.strip('/')}/v1/listen"


def _normalize_rest_url(base_url: Optional[str]) -> str:
    default = "https://api.deepgram.com/v1/speak"
    if not base_url:
        return default
    parsed = urlparse(base_url)
    if parsed.path.endswith("/v1/speak"):
        return urlunparse(parsed)
    path = parsed.path.rstrip("/") + "/v1/speak"
    return urlunparse(parsed._replace(path=path))


def _merge_dicts(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(base)
    if override:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = _merge_dicts(merged[key], value)
            elif value is not None:
                merged[key] = value
    return merged


def _bytes_per_sample(encoding: str) -> int:
    fmt = (encoding or "").lower()
    if fmt in ("ulaw", "mulaw", "mu-law", "g711_ulaw"):
        return 1
    return 2


# Deepgram STT Adapter ------------------------------------------------------------


@dataclass
class _STTSessionState:
    websocket: WebSocketClientProtocol
    options: Dict[str, Any]


class DeepgramSTTAdapter(STTComponent):
    """
    # Milestone7: Deepgram WebSocket streaming STT adapter.

    Maintains a per-call WebSocket session and exposes a simple transcription API.
    """

    def __init__(
        self,
        component_key: str,
        app_config: AppConfig,
        provider_config: DeepgramProviderConfig,
        options: Optional[Dict[str, Any]] = None,
    ):
        self.component_key = component_key
        self._app_config = app_config
        self._provider_defaults = provider_config
        self._pipeline_defaults = options or {}
        self._sessions: Dict[str, _STTSessionState] = {}
        self._default_timeout = float(self._pipeline_defaults.get("response_timeout_sec", 5.0))

    async def start(self) -> None:
        # No global warm-up required yet.
        logger.debug(
            "Deepgram STT adapter initialized",
            component=self.component_key,
            default_model=self._provider_defaults.model,
        )

    async def stop(self) -> None:
        # Close any lingering sessions.
        for call_id in list(self._sessions.keys()):
            await self.close_call(call_id)

    async def open_call(self, call_id: str, options: Dict[str, Any]) -> None:
        merged = self._compose_options(options)
        api_key = merged.get("api_key")
        if not api_key:
            raise RuntimeError("Deepgram STT requires an API key")

        query_params = {
            "model": merged.get("model"),
            "language": merged.get("language"),
            "encoding": merged.get("encoding"),
            "sample_rate": merged.get("sample_rate"),
            "smart_format": str(merged.get("smart_format", True)).lower(),
        }
        query_params = {k: v for k, v in query_params.items() if v}

        ws_url = _normalize_ws_url(merged.get("base_url"))
        parsed = urlparse(ws_url)
        existing = dict(parse_qsl(parsed.query))
        existing.update({k: str(v) for k, v in query_params.items()})
        ws_url = urlunparse(parsed._replace(query=urlencode(existing)))

        logger.info(
            "Deepgram STT opening session",
            call_id=call_id,
            url=ws_url,
            component=self.component_key,
        )

        headers = [
            ("Authorization", f"Token {api_key}"),
            ("User-Agent", "Asterisk-AI-Voice-Agent/1.0"),
        ]
        websocket = await websockets.connect(
            ws_url,
            extra_headers=headers,
            max_size=16 * 1024 * 1024,  # allow generous transcript payloads
        )
        self._sessions[call_id] = _STTSessionState(websocket=websocket, options=merged)

    async def close_call(self, call_id: str) -> None:
        session = self._sessions.pop(call_id, None)
        if not session:
            return
        try:
            await session.websocket.close()
        finally:
            logger.info("Deepgram STT session closed", call_id=call_id)

    async def transcribe(
        self,
        call_id: str,
        audio_pcm16: bytes,
        sample_rate_hz: int,
        options: Dict[str, Any],
    ) -> str:
        session = self._sessions.get(call_id)
        if not session:
            raise RuntimeError(f"Deepgram STT session not found for call {call_id}")

        merged = _merge_dicts(session.options, options or {})
        timeout = float(merged.get("response_timeout_sec", self._default_timeout))
        request_id = f"dg-stt-{uuid.uuid4().hex[:12]}"

        # # Milestone7: Log upstream chunk metadata for debugging/telemetry.
        logger.debug(
            "Deepgram STT sending audio chunk",
            call_id=call_id,
            request_id=request_id,
            chunk_bytes=len(audio_pcm16),
            sample_rate=sample_rate_hz,
        )

        await session.websocket.send(audio_pcm16)
        await session.websocket.send(json.dumps({"type": "flush"}))

        started_at = time.perf_counter()
        try:
            while True:
                message = await asyncio.wait_for(session.websocket.recv(), timeout=timeout)
                transcript = self._extract_transcript(message)
                if transcript is None:
                    continue
                latency_ms = (time.perf_counter() - started_at) * 1000.0
                logger.info(
                    "Deepgram STT transcript received",
                    call_id=call_id,
                    request_id=request_id,
                    latency_ms=round(latency_ms, 2),
                )
                return transcript
        except (asyncio.TimeoutError, websockets.ConnectionClosed) as exc:
            logger.warning(
                "Deepgram STT failed to deliver transcript",
                call_id=call_id,
                request_id=request_id,
                error=str(exc),
            )
            raise

    def _compose_options(self, runtime_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        runtime_options = runtime_options or {}
        merged = {
            "base_url": runtime_options.get("base_url", self._pipeline_defaults.get("base_url", self._provider_defaults.base_url)),
            "model": runtime_options.get("model", self._pipeline_defaults.get("model", self._provider_defaults.model)),
            "language": runtime_options.get("language", self._pipeline_defaults.get("language", self._provider_defaults.stt_language)),
            "encoding": runtime_options.get("encoding", self._pipeline_defaults.get("encoding", self._provider_defaults.input_encoding)),
            "sample_rate": runtime_options.get("sample_rate", self._pipeline_defaults.get("sample_rate", self._provider_defaults.input_sample_rate_hz)),
            "smart_format": runtime_options.get("smart_format", self._pipeline_defaults.get("smart_format", True)),
            "api_key": runtime_options.get("api_key", self._pipeline_defaults.get("api_key", self._provider_defaults.api_key)),
        }
        if runtime_options.get("response_timeout_sec") is not None:
            merged["response_timeout_sec"] = runtime_options["response_timeout_sec"]
        return merged

    @staticmethod
    def _extract_transcript(message: Any) -> Optional[str]:
        if isinstance(message, bytes):
            return None
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.debug("Deepgram STT received non-JSON message", message=message)
            return None

        if isinstance(payload, dict):
            channel = payload.get("channel") or {}
            alternatives = channel.get("alternatives") or []
            for alt in alternatives:
                transcript = alt.get("transcript")
                is_final = payload.get("is_final", True)
                if transcript and (is_final or alt.get("confidence") is not None):
                    return transcript
        return None


# Deepgram TTS Adapter ------------------------------------------------------------


class DeepgramTTSAdapter(TTSComponent):
    """
    # Milestone7: Deepgram REST TTS adapter with μ-law conversion and chunking.
    """

    def __init__(
        self,
        component_key: str,
        app_config: AppConfig,
        provider_config: DeepgramProviderConfig,
        options: Optional[Dict[str, Any]] = None,
        *,
        session_factory: Optional[Callable[[], aiohttp.ClientSession]] = None,
    ):
        self.component_key = component_key
        self._app_config = app_config
        self._provider_defaults = provider_config
        self._pipeline_defaults = options or {}
        self._session_factory = session_factory
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        logger.debug(
            "Deepgram TTS adapter initialized",
            component=self.component_key,
            default_voice=self._provider_defaults.tts_model,
        )

    async def stop(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def open_call(self, call_id: str, options: Dict[str, Any]) -> None:
        # No call-scoped preparation required beyond ensuring the session exists.
        await self._ensure_session()

    async def close_call(self, call_id: str) -> None:
        # Nothing to tear down per call.
        return

    async def synthesize(
        self,
        call_id: str,
        text: str,
        options: Dict[str, Any],
    ) -> AsyncIterator[bytes]:
        if not text:
            return  # Exit early - yields nothing (async generator)
            yield  # Unreachable but makes this an async generator
        await self._ensure_session()

        merged = self._compose_options(options)
        api_key = merged.get("api_key")
        if not api_key:
            raise RuntimeError("Deepgram TTS requires an API key")

        target_format = merged["format"]
        target_encoding = target_format.get("encoding", "mulaw")
        target_sample_rate = int(target_format.get("sample_rate", 8000))

        request_id = f"dg-tts-{uuid.uuid4().hex[:12]}"
        url, params = self._build_tts_request(merged, target_encoding, target_sample_rate)

        logger.info(
            "Deepgram TTS synthesis started",
            call_id=call_id,
            request_id=request_id,
            text_preview=text[:64],
            url=url,
            params=params,
        )

        payload = {"text": text}
        headers = {
            "Authorization": f"Token {api_key}",
            "Accept": "audio/*",
            "Content-Type": "application/json",
        }

        started_at = time.perf_counter()
        async with self._session.post(url, json=payload, params=params, headers=headers) as response:
            if response.status >= 400:
                body = await response.text()
                logger.error(
                    "Deepgram TTS synthesis failed",
                    call_id=call_id,
                    request_id=request_id,
                    status=response.status,
                    body=body,
                )
                response.raise_for_status()

            raw_audio = await response.read()
            source_encoding = params.get("encoding", "linear16")
            source_sample_rate = int(params.get("sample_rate", target_sample_rate))
            converted = self._convert_audio(raw_audio, source_encoding, source_sample_rate, target_encoding, target_sample_rate)
            latency_ms = (time.perf_counter() - started_at) * 1000.0

        logger.info(
            "Deepgram TTS synthesis completed",
            call_id=call_id,
            request_id=request_id,
            latency_ms=round(latency_ms, 2),
            output_bytes=len(converted),
            target_encoding=target_encoding,
            target_sample_rate=target_sample_rate,
        )

        chunk_ms = int(merged.get("chunk_size_ms", 20))
        for chunk in self._chunk_audio(converted, target_encoding, target_sample_rate, chunk_ms):
            if chunk:
                yield chunk

    async def _ensure_session(self) -> None:
        if self._session and not self._session.closed:
            return
        factory = self._session_factory or aiohttp.ClientSession
        self._session = factory()

    def _compose_options(self, runtime_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        runtime_options = runtime_options or {}
        format_defaults = self._pipeline_defaults.get("format", {})
        merged_format = {
            "encoding": runtime_options.get("format", {}).get("encoding", format_defaults.get("encoding", "mulaw")),
            "sample_rate": runtime_options.get("format", {}).get("sample_rate", format_defaults.get("sample_rate", 8000)),
        }

        merged = {
            "base_url": runtime_options.get("base_url", self._pipeline_defaults.get("base_url", self._provider_defaults.base_url)),
            "model": runtime_options.get("model", self._pipeline_defaults.get("model", self._provider_defaults.tts_model or self._provider_defaults.model)),
            "voice": runtime_options.get("voice", self._pipeline_defaults.get("voice", self._provider_defaults.tts_model)),
            "language": runtime_options.get("language", self._pipeline_defaults.get("language", self._provider_defaults.stt_language)),
            "chunk_size_ms": runtime_options.get("chunk_size_ms", self._pipeline_defaults.get("chunk_size_ms", 20)),
            "api_key": runtime_options.get("api_key", self._pipeline_defaults.get("api_key", self._provider_defaults.api_key)),
            "format": merged_format,
        }
        # Default the provider output (source_format) sample rate to the target sample rate
        # so we request 8 kHz from Deepgram when our downstream is μ-law 8 kHz.
        source_cfg = runtime_options.get("source_format", self._pipeline_defaults.get("source_format", {}))
        default_source_rate = int(merged_format.get("sample_rate", 8000))
        merged["source_format"] = {
            "encoding": source_cfg.get("encoding", "linear16"),
            "sample_rate": int(source_cfg.get("sample_rate", default_source_rate)),
        }
        return merged

    def _build_tts_request(
        self,
        options: Dict[str, Any],
        target_encoding: str,
        target_sample_rate: int,
    ) -> Tuple[str, Dict[str, Any]]:
        url = _normalize_rest_url(options.get("base_url"))
        params: Dict[str, Any] = {
            "model": options.get("model") or options.get("voice"),
            "voice": options.get("voice"),
            "language": options.get("language"),
        }
        source_format = options.get("source_format", {})
        params["encoding"] = source_format.get("encoding", "linear16")
        # Request provider to emit audio at the downstream target sample rate by default
        params["sample_rate"] = int(source_format.get("sample_rate", target_sample_rate))
        params["target_encoding"] = target_encoding
        params["target_sample_rate"] = target_sample_rate
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}
        return url, params

    @staticmethod
    def _convert_audio(
        audio_bytes: bytes,
        source_encoding: str,
        source_rate: int,
        target_encoding: str,
        target_rate: int,
    ) -> bytes:
        if not audio_bytes:
            return b""

        fmt = (source_encoding or "").lower()
        if fmt in ("ulaw", "mulaw", "mu-law", "g711_ulaw"):
            pcm_bytes = mulaw_to_pcm16le(audio_bytes)
        else:
            pcm_bytes = audio_bytes

        if source_rate != target_rate:
            pcm_bytes, _ = resample_audio(pcm_bytes, source_rate, target_rate)

        return convert_pcm16le_to_target_format(pcm_bytes, target_encoding)

    @staticmethod
    def _chunk_audio(
        audio_bytes: bytes,
        encoding: str,
        sample_rate: int,
        chunk_ms: int,
    ) -> Iterable[bytes]:
        if not audio_bytes:
            return
        bytes_per = _bytes_per_sample(encoding)
        frame_size = max(bytes_per, int(sample_rate * (chunk_ms / 1000.0) * bytes_per))
        for idx in range(0, len(audio_bytes), frame_size):
            yield audio_bytes[idx : idx + frame_size]
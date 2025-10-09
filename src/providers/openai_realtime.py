"""
OpenAI Realtime provider implementation.

This module integrates OpenAI's server-side Realtime WebSocket transport into the
Asterisk AI Voice Agent without requiring WebRTC. Audio from AudioSocket is
upsampled to PCM16 @ 24 kHz, streamed to OpenAI, and PCM16 24 kHz output is
resampled to the configured downstream AudioSocket format (µ-law or PCM16 8 kHz).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import uuid
from typing import Any, Dict, Optional, List

import websockets
from websockets import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from structlog import get_logger

from .base import AIProviderInterface
from ..audio import (
    convert_pcm16le_to_target_format,
    mulaw_to_pcm16le,
    resample_audio,
)
from ..config import OpenAIRealtimeProviderConfig

logger = get_logger(__name__)

_COMMIT_INTERVAL_SEC = 0.2
_KEEPALIVE_INTERVAL_SEC = 15.0


class OpenAIRealtimeProvider(AIProviderInterface):
    """
    OpenAI Realtime provider using server-side WebSocket transport.

    Lifecycle:
    1. start_session(call_id) -> establishes WebSocket session.
    2. send_audio(bytes) -> converts inbound AudioSocket frames to PCM16 24 kHz,
       base64-encodes, and streams via input_audio_buffer.
    3. Provider output deltas are decoded, resampled to AudioSocket format, and
       emitted as AgentAudio / AgentAudioDone events.
    4. stop_session() -> closes the WebSocket and cancels background tasks.
    """

    def __init__(
        self,
        config: OpenAIRealtimeProviderConfig,
        on_event,
    ):
        super().__init__(on_event)
        self.config = config
        self.websocket: Optional[WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()

        self._call_id: Optional[str] = None
        self._pending_response: bool = False
        self._in_audio_burst: bool = False
        self._first_output_chunk_logged: bool = False
        self._closing: bool = False
        self._closed: bool = False

        self._input_resample_state: Optional[tuple] = None
        self._output_resample_state: Optional[tuple] = None
        self._transcript_buffer: str = ""
        self._input_info_logged: bool = False
        # Aggregate provider-rate PCM16 bytes (24 kHz default) and commit in >=100ms chunks
        self._pending_audio_provider_rate: bytearray = bytearray()
        self._last_commit_ts: float = 0.0
        # Serialize append/commit to avoid empty commits from races
        self._audio_lock: asyncio.Lock = asyncio.Lock()

        try:
            if self.config.input_encoding:
                self.config.input_encoding = self.config.input_encoding.strip()
        except Exception:
            pass

    def describe_alignment(
        self,
        *,
        audiosocket_format: str,
        streaming_encoding: str,
        streaming_sample_rate: int,
    ) -> List[str]:
        issues: List[str] = []
        inbound_enc = (self.config.input_encoding or "slin16").lower()
        inbound_rate = int(self.config.input_sample_rate_hz or 0)
        target_enc = (self.config.target_encoding or "ulaw").lower()
        target_rate = int(self.config.target_sample_rate_hz or 0)

        def _class(enc: str) -> str:
            e = (enc or "").lower()
            if e in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                return "ulaw"
            if e in ("slin16", "linear16", "pcm16"):
                return "pcm16"
            return e

        if inbound_enc in ("slin16", "linear16", "pcm16") and _class(audiosocket_format) == "ulaw":
            issues.append(
                "OpenAI inbound encoding is PCM16 but AudioSocket format is μ-law; set audiosocket.format=slin16 "
                "or change openai_realtime.input_encoding to ulaw."
            )
        if inbound_enc in ("ulaw", "mulaw", "g711_ulaw", "mu-law") and _class(audiosocket_format) != "ulaw":
            issues.append(
                f"OpenAI inbound encoding {inbound_enc} does not match audiosocket.format={audiosocket_format}."
            )
        if inbound_enc in ("ulaw", "mulaw", "g711_ulaw", "mu-law") and inbound_rate and inbound_rate != 8000:
            issues.append(
                f"OpenAI inbound μ-law sample rate is {inbound_rate} Hz; μ-law transport should be 8000 Hz."
            )

        if _class(target_enc) != _class(streaming_encoding):
            issues.append(
                f"OpenAI target_encoding={target_enc} but streaming manager emits {streaming_encoding}."
            )
        if target_rate and target_rate != streaming_sample_rate:
            issues.append(
                f"OpenAI target_sample_rate_hz={target_rate} but streaming sample rate is {streaming_sample_rate}."
            )

        provider_rate = int(self.config.provider_input_sample_rate_hz or 0)
        if provider_rate and provider_rate < 24000:
            issues.append(
                f"OpenAI provider_input_sample_rate_hz={provider_rate}; recommend 24000 for realtime streaming."
            )

        return issues
        # Track provider output format we requested in session.update
        self._provider_output_format: str = "pcm16"

    @property
    def supported_codecs(self):
        fmt = (self.config.target_encoding or "ulaw").lower()
        return [fmt]

    async def start_session(self, call_id: str):
        if not self.config.api_key:
            raise ValueError("OpenAI Realtime provider requires OPENAI_API_KEY")

        await self.stop_session()
        self._call_id = call_id
        self._pending_response = False
        self._in_audio_burst = False
        self._first_output_chunk_logged = False
        self._input_resample_state = None
        self._output_resample_state = None
        self._transcript_buffer = ""
        self._closing = False
        self._closed = False

        url = self._build_ws_url()
        headers = [
            ("Authorization", f"Bearer {self.config.api_key}"),
            ("OpenAI-Beta", "realtime=v1"),
        ]
        if self.config.organization:
            headers.append(("OpenAI-Organization", self.config.organization))

        logger.info("Connecting to OpenAI Realtime", url=url, call_id=call_id)
        try:
            self.websocket = await websockets.connect(url, extra_headers=headers)
        except Exception:
            logger.error("Failed to connect to OpenAI Realtime", call_id=call_id, exc_info=True)
            raise

        await self._send_session_update()

        # Proactively request an initial response so the agent can greet
        # even before user audio arrives. Prefer explicit greeting text
        # when provided; otherwise fall back to generic instructions.
        try:
            if (self.config.greeting or "").strip():
                await self._send_explicit_greeting()
            else:
                await self._ensure_response_request()
        except Exception:
            logger.debug("Initial response.create request failed", call_id=call_id, exc_info=True)

        self._receive_task = asyncio.create_task(self._receive_loop())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

        logger.info("OpenAI Realtime session established", call_id=call_id)

    async def send_audio(self, audio_chunk: bytes):
        if not audio_chunk:
            return
        if not self.websocket or self.websocket.closed:
            logger.debug("Dropping inbound audio: websocket not ready", call_id=self._call_id)
            return

        try:
            # Log input codec/config once for diagnosis
            if not self._input_info_logged:
                try:
                    logger.info(
                        "OpenAI input config",
                        call_id=self._call_id,
                        input_encoding=self.config.input_encoding,
                        input_sample_rate_hz=self.config.input_sample_rate_hz,
                        provider_input_sample_rate_hz=self.config.provider_input_sample_rate_hz,
                    )
                    self._input_info_logged = True
                except Exception:
                    pass

            pcm16 = self._convert_inbound_audio(audio_chunk)
            if not pcm16:
                return

            # Log first conversion sizes to verify resample
            if self._input_info_logged is True and self._input_resample_state is not None:
                try:
                    logger.debug(
                        "OpenAI input frame sizes",
                        call_id=self._call_id,
                        src_bytes=len(audio_chunk),
                        dst_bytes=len(pcm16),
                    )
                except Exception:
                    pass

            # If server VAD is enabled, just append frames; do not commit.
            vad_enabled = getattr(self.config, "turn_detection", None) is not None
            if vad_enabled:
                try:
                    audio_b64 = base64.b64encode(pcm16).decode("ascii")
                    await self._send_json({"type": "input_audio_buffer.append", "audio": audio_b64})
                except Exception:
                    logger.error("Failed to append input audio buffer (VAD)", call_id=self._call_id, exc_info=True)
            else:
                # Serialize accumulation and commit to avoid empty commits due to races
                async with self._audio_lock:
                    # Accumulate until we have >= 160ms to comfortably satisfy >=100ms minimum
                    self._pending_audio_provider_rate.extend(pcm16)
                    bytes_per_ms = int(self.config.provider_input_sample_rate_hz * 2 / 1000)
                    commit_threshold_ms = 160
                    commit_threshold_bytes = bytes_per_ms * commit_threshold_ms

                    if len(self._pending_audio_provider_rate) >= commit_threshold_bytes:
                        chunk = bytes(self._pending_audio_provider_rate)
                        self._pending_audio_provider_rate.clear()
                        audio_b64 = base64.b64encode(chunk).decode("ascii")
                        try:
                            await self._send_json({"type": "input_audio_buffer.append", "audio": audio_b64})
                            await self._send_json({"type": "input_audio_buffer.commit"})
                            self._last_commit_ts = time.monotonic()
                            logger.debug(
                                "OpenAI committed input audio",
                                call_id=self._call_id,
                                ms=len(chunk) // bytes_per_ms,
                                bytes=len(chunk),
                            )
                        except Exception:
                            logger.error("Failed to append/commit input audio buffer", call_id=self._call_id, exc_info=True)
                        await self._ensure_response_request()
        except ConnectionClosedError:
            logger.warning("OpenAI Realtime socket closed while sending audio", call_id=self._call_id)
        except Exception:
            logger.error("Failed to send audio to OpenAI Realtime", call_id=self._call_id, exc_info=True)

    async def stop_session(self):
        if self._closing or self._closed:
            return

        self._closing = True
        try:
            if self._receive_task:
                self._receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._receive_task
            if self._keepalive_task:
                self._keepalive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._keepalive_task

            if self.websocket and not self.websocket.closed:
                await self.websocket.close()

            await self._emit_audio_done()
        finally:
            self._receive_task = None
            self._keepalive_task = None
            self.websocket = None
            self._call_id = None
            self._closing = False
            self._closed = True
            self._pending_response = False
            self._in_audio_burst = False
            self._input_resample_state = None
            self._output_resample_state = None
            self._transcript_buffer = ""
            logger.info("OpenAI Realtime session stopped")

    def get_provider_info(self) -> Dict[str, Any]:
        return {
            "name": "OpenAIRealtimeProvider",
            "type": "cloud",
            "model": self.config.model,
            "voice": self.config.voice,
            "supported_codecs": self.supported_codecs,
        }

    def is_ready(self) -> bool:
        return bool(self.config.api_key)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _build_ws_url(self) -> str:
        base = (self.config.base_url or "").strip()
        # Fallback if unresolved placeholders exist or scheme isn't ws/wss
        if base.startswith("${") or not base.startswith(("ws://", "wss://")):
            logger.warning("Invalid OpenAI base_url in config; falling back to default", base_url=base)
            base = "wss://api.openai.com/v1/realtime"
        base = base.rstrip("/")
        return f"{base}?model={self.config.model}"

    async def _send_session_update(self):
        # Map config modalities to output_modalities per latest guide
        output_modalities = [m for m in (self.config.response_modalities or []) if m in ("audio", "text")]
        if not output_modalities:
            output_modalities = ["audio"]

        # Choose OpenAI output format for this session:
        # If downstream target is μ-law, request g711_ulaw from provider to test end-to-end μ-law.
        # Otherwise keep PCM16.
        out_fmt = "pcm16"

        session: Dict[str, Any] = {
            # Model is selected via URL; keep accepted keys here
            "modalities": output_modalities,
            "input_audio_format": "pcm16",
            "output_audio_format": out_fmt,
            "voice": self.config.voice,
        }
        # Record provider output format for runtime handling
        self._provider_output_format = out_fmt
        # Optional server-side VAD/turn detection at session level
        if getattr(self.config, "turn_detection", None):
            try:
                td = self.config.turn_detection
                session["turn_detection"] = {
                    "type": td.type,
                    "silence_duration_ms": td.silence_duration_ms,
                    "threshold": td.threshold,
                    "prefix_padding_ms": td.prefix_padding_ms,
                }
            except Exception:
                logger.debug("Failed to include turn_detection in session.update", call_id=self._call_id, exc_info=True)

        if self.config.instructions:
            session["instructions"] = self.config.instructions

        payload: Dict[str, Any] = {
            "type": "session.update",
            "event_id": f"sess-{uuid.uuid4()}",
            "session": session,
        }

        await self._send_json(payload)

    async def _send_explicit_greeting(self):
        greeting = (self.config.greeting or "").strip()
        if not greeting or not self.websocket or self.websocket.closed:
            return

        # Map config modalities to output_modalities
        output_modalities = [m for m in (self.config.response_modalities or []) if m in ("audio", "text")] or ["audio"]

        response_payload: Dict[str, Any] = {
            "type": "response.create",
            "event_id": f"resp-{uuid.uuid4()}",
            "response": {
                # Force audio modality for greeting at response level (optional)
                "modalities": output_modalities,
                # Be explicit to ensure the model speaks immediately
                "instructions": f"Please greet the user with the following: {greeting}",
                "metadata": {"call_id": self._call_id, "purpose": "initial_greeting"},
                # Optionally strip context to avoid distractions
                "input": [],
            },
        }

        await self._send_json(response_payload)
        self._pending_response = True

    async def _ensure_response_request(self):
        if self._pending_response or not self.websocket or self.websocket.closed:
            return

        response_payload: Dict[str, Any] = {
            "type": "response.create",
            "event_id": f"resp-{uuid.uuid4()}",
            "response": {
                # Use 'modalities' which is accepted by server
                "modalities": [m for m in (self.config.response_modalities or []) if m in ("audio", "text")] or ["audio"],
                "metadata": {"call_id": self._call_id},
            },
        }
        if self.config.instructions:
            response_payload["response"]["instructions"] = self.config.instructions

        await self._send_json(response_payload)
        self._pending_response = True

    async def _send_json(self, payload: Dict[str, Any]):
        if not self.websocket or self.websocket.closed:
            return
        # Avoid logging base64 audio payloads; but log control message types
        try:
            ptype = payload.get("type")
            if ptype and not ptype.startswith("input_audio_buffer."):
                logger.debug("OpenAI send", call_id=self._call_id, type=ptype)
        except Exception:
            pass
        message = json.dumps(payload)
        async with self._send_lock:
            await self.websocket.send(message)

    def _convert_inbound_audio(self, audio_chunk: bytes) -> Optional[bytes]:
        fmt_raw = getattr(self.config, "input_encoding", None) or "slin16"
        fmt = fmt_raw.strip().lower()
        # Persist sanitized value so future checks stay consistent
        try:
            self.config.input_encoding = fmt
        except Exception:
            pass

        valid_encodings = {
            "ulaw",
            "mulaw",
            "g711_ulaw",
            "mu-law",
            "slin16",
            "linear16",
            "pcm16",
        }
        if fmt not in valid_encodings:
            logger.warning("Unsupported input encoding for OpenAI Realtime", encoding=fmt_raw)
            fmt = "slin16"
            try:
                self.config.input_encoding = fmt
            except Exception:
                pass

        chunk_len = len(audio_chunk)
        # Infer actual transport format from canonical 20 ms frame sizes when possible
        #  - 160 B ≈ μ-law @ 8 kHz (20 ms)
        #  - 320 B ≈ PCM16 @ 8 kHz (20 ms)
        #  - 640 B ≈ PCM16 @ 16 kHz (20 ms)
        if chunk_len == 160:
            actual_format = "ulaw"
            inferred_rate = 8000
        elif chunk_len == 320:
            actual_format = "pcm16"
            inferred_rate = 8000
        elif chunk_len == 640:
            actual_format = "pcm16"
            inferred_rate = 16000
        else:
            actual_format = "pcm16" if fmt in ("slin16", "linear16", "pcm16") else "ulaw"
            inferred_rate = int(getattr(self.config, "input_sample_rate_hz", 0) or 0) or 8000

        # Select source_rate based on declared encoding and inference
        if actual_format == "ulaw":
            source_rate = 8000
        else:
            # PCM path: prefer declared input_sample_rate_hz if set, else inference
            declared_rate = int(getattr(self.config, "input_sample_rate_hz", 0) or 0)
            source_rate = declared_rate or inferred_rate or 8000
        if actual_format == "ulaw":
            pcm_src = mulaw_to_pcm16le(audio_chunk)
        else:
            pcm_src = audio_chunk

        provider_rate = int(getattr(self.config, "provider_input_sample_rate_hz", 0) or 0)

        if provider_rate and provider_rate != source_rate:
            pcm_provider_rate, self._input_resample_state = resample_audio(
                pcm_src,
                source_rate,
                provider_rate,
                state=self._input_resample_state,
            )
            return pcm_provider_rate

        self._input_resample_state = None
        return pcm_src

    async def _receive_loop(self):
        assert self.websocket is not None
        try:
            async for message in self.websocket:
                if isinstance(message, bytes):
                    continue
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Failed to decode OpenAI Realtime payload", payload_preview=message[:64])
                    continue
                await self._handle_event(event)
        except asyncio.CancelledError:
            pass
        except (ConnectionClosedError, ConnectionClosedOK):
            logger.info("OpenAI Realtime connection closed", call_id=self._call_id)
        except Exception:
            logger.error("OpenAI Realtime receive loop error", call_id=self._call_id, exc_info=True)
        finally:
            await self._emit_audio_done()
            self._pending_response = False

    async def _handle_event(self, event: Dict[str, Any]):
        event_type = event.get("type")

        # Log top-level error events with full payload to diagnose API contract issues
        if event_type == "error":
            # Use 'error_event' key to avoid structlog 'event' argument conflict
            logger.error("OpenAI Realtime error event", call_id=self._call_id, error_event=event)
            return

        if event_type == "response.created":
            logger.debug("OpenAI response created", call_id=self._call_id)
            return

        if event_type == "response.delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")

            if delta_type == "output_audio.delta":
                audio_b64 = delta.get("audio")
                if audio_b64:
                    await self._handle_output_audio(audio_b64)
            elif delta_type == "output_audio.done":
                await self._emit_audio_done()
            elif delta_type == "output_text.delta":
                text = delta.get("text")
                if text:
                    await self._emit_transcript(text, is_final=False)
            elif delta_type == "output_text.done":
                if self._transcript_buffer:
                    await self._emit_transcript("", is_final=True)
            return

        # Modern event naming variants (top-level types)
        if event_type == "response.output_audio.delta":
            audio_b64 = (
                event.get("audio")
                or (event.get("delta") or {}).get("audio")
            )
            if audio_b64:
                await self._handle_output_audio(audio_b64)
            else:
                logger.debug("Missing audio in response.output_audio.delta", call_id=self._call_id)
            return

        if event_type == "response.output_audio.done":
            await self._emit_audio_done()
            return

        # Additional modern variant used by some previews
        if event_type == "response.audio.delta":
            audio_b64 = event.get("delta")
            if audio_b64:
                await self._handle_output_audio(audio_b64)
            else:
                logger.debug("Missing audio in response.audio.delta", call_id=self._call_id)
            return

        if event_type == "response.audio.done":
            await self._emit_audio_done()
            return

        if event_type == "response.audio_transcript.delta":
            delta = event.get("delta")
            text = event.get("text")
            if text is None:
                if isinstance(delta, dict):
                    text = delta.get("text")
                elif isinstance(delta, str):
                    text = delta
            if text:
                await self._emit_transcript(text, is_final=False)
            return

        if event_type == "response.audio_transcript.done":
            if self._transcript_buffer:
                await self._emit_transcript("", is_final=True)
            return

        if event_type in ("response.completed", "response.error", "response.cancelled", "response.done"):
            await self._emit_audio_done()
            if event_type == "response.error":
                logger.error("OpenAI Realtime response error", call_id=self._call_id, error=event.get("error"))
            self._pending_response = False
            if self._transcript_buffer:
                await self._emit_transcript("", is_final=True)
            return

        if event_type == "input_transcription.completed":
            transcript = event.get("transcript")
            if transcript:
                await self._emit_transcript(transcript, is_final=True)
            return

        if event_type == "response.output_text.delta":
            delta = event.get("delta") or {}
            text = delta.get("text")
            if text:
                await self._emit_transcript(text, is_final=False)
            return

        # Optional acks/telemetry for audio buffer operations
        if event_type and event_type.startswith("input_audio_buffer"):
            logger.debug("OpenAI input_audio_buffer ack", call_id=self._call_id, event_type=event_type)
            return

        # Additional transcript variants per guide
        if event_type == "response.output_audio_transcript.delta":
            delta = event.get("delta")
            text = None
            if isinstance(delta, dict):
                text = delta.get("text")
            elif isinstance(delta, str):
                text = delta
            if text:
                await self._emit_transcript(text, is_final=False)
            return

        if event_type == "response.output_audio_transcript.done":
            if self._transcript_buffer:
                await self._emit_transcript("", is_final=True)
            return

        logger.debug("Unhandled OpenAI Realtime event", event_type=event_type)

    async def _handle_output_audio(self, audio_b64: str):
        try:
            pcm_provider_output = base64.b64decode(audio_b64)
        except Exception:
            logger.warning("Invalid base64 audio payload from OpenAI", call_id=self._call_id)
            return

        if not pcm_provider_output:
            return

        target_rate = self.config.target_sample_rate_hz
        pcm_target, self._output_resample_state = resample_audio(
            pcm_provider_output,
            self.config.output_sample_rate_hz,
            target_rate,
            state=self._output_resample_state,
        )

        outbound = convert_pcm16le_to_target_format(pcm_target, self.config.target_encoding)
        if not outbound:
            return

        if self.on_event:
            if not self._first_output_chunk_logged:
                logger.info(
                    "OpenAI Realtime first audio chunk",
                    call_id=self._call_id,
                    bytes=len(outbound),
                    target_encoding=self.config.target_encoding,
                )
                self._first_output_chunk_logged = True

            self._in_audio_burst = True
            try:
                await self.on_event(
                    {
                        "type": "AgentAudio",
                        "data": outbound,
                        "streaming_chunk": True,
                        "call_id": self._call_id,
                        "encoding": (self.config.target_encoding or "slin16"),
                        "sample_rate": self.config.target_sample_rate_hz,
                    }
                )
            except Exception:
                logger.error("Failed to emit AgentAudio event", call_id=self._call_id, exc_info=True)

    async def _emit_audio_done(self):
        if not self._in_audio_burst or not self.on_event or not self._call_id:
            return
        try:
            await self.on_event(
                {
                    "type": "AgentAudioDone",
                    "streaming_done": True,
                    "call_id": self._call_id,
                }
            )
        except Exception:
            logger.error("Failed to emit AgentAudioDone event", call_id=self._call_id, exc_info=True)
        finally:
            self._in_audio_burst = False
            self._output_resample_state = None
            self._first_output_chunk_logged = False

    async def _emit_transcript(self, text: str, *, is_final: bool):
        if not self.on_event or not self._call_id:
            return

        if text:
            self._transcript_buffer += text

        payload = {
            "type": "Transcript",
            "call_id": self._call_id,
            "text": text or self._transcript_buffer,
            "is_final": is_final,
        }
        try:
            await self.on_event(payload)
        except Exception:
            logger.error("Failed to emit transcript event", call_id=self._call_id, exc_info=True)

        if is_final:
            self._transcript_buffer = ""

    async def _keepalive_loop(self):
        try:
            while self.websocket and not self.websocket.closed:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SEC)
                if not self.websocket or self.websocket.closed:
                    break
                try:
                    # Use native WebSocket ping control frames instead of
                    # sending an application-level {"type":"ping"} event,
                    # which Realtime rejects with invalid_request_error.
                    async with self._send_lock:
                        if self.websocket and not self.websocket.closed:
                            await self.websocket.ping()
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.debug("OpenAI Realtime keepalive failed", call_id=self._call_id, exc_info=True)
                    break
        except asyncio.CancelledError:
            pass

"""
StreamingPlaybackManager - Handles streaming audio playback via AudioSocket/ExternalMedia.

This module provides streaming audio playback capabilities that send audio chunks
directly over the AudioSocket connection instead of using file-based playback.
It includes automatic fallback to file playback on errors or timeouts.
"""

import asyncio
import time
import audioop
from typing import Optional, Dict, Any, TYPE_CHECKING, Set, Callable, Awaitable
import structlog
from prometheus_client import Counter, Gauge, Histogram
import math

from src.audio.resampler import (
    mulaw_to_pcm16le,
    pcm16le_to_mulaw,
    resample_audio,
)
from src.core.session_store import SessionStore
from src.core.models import CallSession, PlaybackRef

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.core.conversation_coordinator import ConversationCoordinator
    from src.core.playback_manager import PlaybackManager

logger = structlog.get_logger(__name__)

# Prometheus metrics for streaming playback (module-scope, registered once)
_STREAMING_ACTIVE_GAUGE = Gauge(
    "ai_agent_streaming_active",
    "Whether streaming playback is active for a call (1 = active)",
    labelnames=("call_id",),
)
_STREAMING_BYTES_TOTAL = Counter(
    "ai_agent_streaming_bytes_total",
    "Total bytes queued to streaming playback (pre-conversion)",
    labelnames=("call_id",),
)
_STREAMING_FALLBACKS_TOTAL = Counter(
    "ai_agent_streaming_fallbacks_total",
    "Number of times streaming fell back to file playback",
    labelnames=("call_id",),
)
_STREAMING_JITTER_DEPTH = Gauge(
    "ai_agent_streaming_jitter_buffer_depth",
    "Current jitter buffer depth in queued chunks",
    labelnames=("call_id",),
)
_STREAMING_LAST_CHUNK_AGE = Gauge(
    "ai_agent_streaming_last_chunk_age_seconds",
    "Seconds since last streaming chunk was received",
    labelnames=("call_id",),
)
_STREAMING_KEEPALIVES_SENT_TOTAL = Counter(
    "ai_agent_streaming_keepalives_sent_total",
    "Count of keepalive ticks sent while streaming",
    labelnames=("call_id",),
)
_STREAMING_KEEPALIVE_TIMEOUTS_TOTAL = Counter(
    "ai_agent_streaming_keepalive_timeouts_total",
    "Count of keepalive-detected streaming timeouts",
    labelnames=("call_id",),
)
_STREAM_TX_BYTES = Counter(
    "ai_agent_stream_tx_bytes_total",
    "Outbound audio bytes sent to caller (per call)",
    labelnames=("call_id",),
)

# New observability metrics for tuning
_STREAM_STARTED_TOTAL = Counter(
    "ai_agent_stream_started_total",
    "Number of streaming segments started",
    labelnames=("call_id", "playback_type"),
)
_STREAM_FIRST_FRAME_SECONDS = Histogram(
    "ai_agent_stream_first_frame_seconds",
    "Time from stream start to first outbound frame",
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0),
    labelnames=("call_id", "playback_type"),
)
_STREAM_SEGMENT_DURATION_SECONDS = Histogram(
    "ai_agent_stream_segment_duration_seconds",
    "Streaming segment duration",
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 15.0, 30.0),
    labelnames=("call_id", "playback_type"),
)
_STREAM_END_REASON_TOTAL = Counter(
    "ai_agent_stream_end_reason_total",
    "Count of stream end reasons",
    labelnames=("call_id", "reason"),
)
_STREAM_ENDIAN_CORRECTIONS_TOTAL = Counter(
    "ai_agent_stream_endian_corrections_total",
    "Count of PCM16 egress byte-order corrections applied automatically",
    labelnames=("call_id", "mode"),
)


class StreamingPlaybackManager:
    """
    Manages streaming audio playback with automatic fallback to file playback.
    
    Responsibilities:
    - Stream audio chunks directly over AudioSocket/ExternalMedia
    - Handle jitter buffering and timing
    - Implement automatic fallback to file playback
    - Manage streaming state and cleanup
    - Coordinate with ConversationCoordinator for gating
    """
    
    def __init__(
        self,
        session_store: SessionStore,
        ari_client,
        conversation_coordinator: Optional["ConversationCoordinator"] = None,
        fallback_playback_manager: Optional["PlaybackManager"] = None,
        streaming_config: Optional[Dict[str, Any]] = None,
        audio_transport: str = "externalmedia",
        rtp_server: Optional[Any] = None,
        audiosocket_server: Optional[Any] = None,
        audio_diag_callback: Optional[Callable[[str, str, bytes, str, int], Awaitable[None]]] = None,
    ):
        self.session_store = session_store
        self.ari_client = ari_client
        self.conversation_coordinator = conversation_coordinator
        self.fallback_playback_manager = fallback_playback_manager
        self.streaming_config = streaming_config or {}
        self.audio_transport = audio_transport
        self.rtp_server = rtp_server
        self.audiosocket_server = audiosocket_server
        self.audio_diag_callback = audio_diag_callback
        self.audiosocket_format: str = "ulaw"  # default format expected by dialplan
        # Debug: when True, send frames to all AudioSocket conns for the call
        self.audiosocket_broadcast_debug: bool = bool(self.streaming_config.get('audiosocket_broadcast_debug', False))
        # Egress endianness override mode: 'auto' | 'force_true' | 'force_false'
        try:
            self.egress_swap_mode: str = str(self.streaming_config.get('egress_swap_mode', 'auto')).lower().strip() or 'auto'
        except Exception:
            self.egress_swap_mode = 'auto'
        self.egress_force_mulaw: bool = bool(self.streaming_config.get('egress_force_mulaw', False))
        
        # Streaming state
        self.active_streams: Dict[str, Dict[str, Any]] = {}  # call_id -> stream_info
        self.jitter_buffers: Dict[str, asyncio.Queue] = {}  # call_id -> audio_queue
        self.keepalive_tasks: Dict[str, asyncio.Task] = {}  # call_id -> keepalive_task
        # Per-call remainder buffer for precise frame sizing
        self.frame_remainders: Dict[str, bytes] = {}
        # Per-call resampler state (used when converting between rates)
        self._resample_states: Dict[str, Optional[tuple]] = {}
        # First outbound frame logged tracker
        self._first_send_logged: Set[str] = set()
        # Startup gating to allow jitter buffers to fill before playback begins
        self._startup_ready: Dict[str, bool] = {}
        
        # Configuration defaults
        self.sample_rate = self.streaming_config.get('sample_rate', 8000)
        self.jitter_buffer_ms = self.streaming_config.get('jitter_buffer_ms', 50)
        self.keepalive_interval_ms = self.streaming_config.get('keepalive_interval_ms', 5000)
        self.connection_timeout_ms = self.streaming_config.get('connection_timeout_ms', 10000)
        self.fallback_timeout_ms = self.streaming_config.get('fallback_timeout_ms', 4000)
        self.chunk_size_ms = self.streaming_config.get('chunk_size_ms', 20)
        # Derived configuration (chunk counts)
        self.min_start_ms = max(0, int(self.streaming_config.get('min_start_ms', 120)))
        self.low_watermark_ms = max(0, int(self.streaming_config.get('low_watermark_ms', 80)))
        self.provider_grace_ms = max(0, int(self.streaming_config.get('provider_grace_ms', 500)))
        self.min_start_chunks = max(1, int(math.ceil(self.min_start_ms / max(1, self.chunk_size_ms))))
        self.low_watermark_chunks = max(0, int(math.ceil(self.low_watermark_ms / max(1, self.chunk_size_ms))))
        # Greeting-specific warm-up (optional)
        try:
            self.greeting_min_start_ms = int(self.streaming_config.get('greeting_min_start_ms', 0))
        except Exception:
            self.greeting_min_start_ms = 0
        self.greeting_min_start_chunks = (
            max(1, int(math.ceil(self.greeting_min_start_ms / max(1, self.chunk_size_ms))))
            if self.greeting_min_start_ms > 0 else self.min_start_chunks
        )
        # Logging verbosity override
        self.logging_level = (self.streaming_config.get('logging_level') or "info").lower()
        if self.logging_level == "debug":
            logger.debug("Streaming playback logging level set to DEBUG")
        elif self.logging_level == "warning":
            logger.warning("Streaming playback logging level set to WARNING")
        elif self.logging_level not in ("info", "debug", "warning"):
            logger.info("Streaming playback logging level", value=self.logging_level)

        logger.info("StreamingPlaybackManager initialized",
                   sample_rate=self.sample_rate,
                   jitter_buffer_ms=self.jitter_buffer_ms)

    @staticmethod
    def _canonicalize_encoding(value: Optional[str]) -> str:
        if not value:
            return ""
        token = str(value).strip().lower()
        mapping = {
            "mu-law": "ulaw",
            "mulaw": "ulaw",
            "g711_ulaw": "ulaw",
            "g711ulaw": "ulaw",
            "linear16": "slin16",
            "pcm16": "slin16",
            "slin": "slin16",
            "slin12": "slin16",
            "slin16": "slin16",
        }
        return mapping.get(token, token)

    @staticmethod
    def _is_mulaw(value: Optional[str]) -> bool:
        canonical = StreamingPlaybackManager._canonicalize_encoding(value)
        return canonical in {"ulaw", "mulaw", "g711_ulaw", "mu-law"}

    @staticmethod
    def _default_sample_rate_for_format(fmt: Optional[str], fallback: int) -> int:
        canonical = StreamingPlaybackManager._canonicalize_encoding(fmt)
        if canonical in {"ulaw", "mulaw", "g711_ulaw", "mu-law"}:
            return 8000
        if canonical in {"slin16", "linear16", "pcm16"}:
            return fallback if fallback > 0 else 16000
        return fallback if fallback > 0 else 8000

    async def start_streaming_playback(
        self,
        call_id: str,
        audio_chunks: asyncio.Queue,
        playback_type: str = "response",
        source_encoding: Optional[str] = None,
        source_sample_rate: Optional[int] = None,
        target_encoding: Optional[str] = None,
        target_sample_rate: Optional[int] = None,
    ) -> Optional[str]:
        """
        Start streaming audio playback for a call.
        
        Args:
            call_id: Canonical call ID
            audio_chunks: Queue of audio chunks to stream
            playback_type: Type of playback (greeting, response, etc.)
            source_encoding: Provider audio encoding reported for this stream.
            source_sample_rate: Provider audio sample rate for this stream.
        
        Returns:
            stream_id if successful, None if failed
        """
        try:
            # Reuse active stream if one already exists
            if self.is_stream_active(call_id):
                existing = self.active_streams[call_id]['stream_id']
                logger.debug("Streaming already active for call", call_id=call_id, stream_id=existing)
                return existing

            # Get session to determine target channel
            session = await self.session_store.get_by_call_id(call_id)
            if not session:
                logger.error("Cannot start streaming - call session not found",
                           call_id=call_id)
                return None
            
            # Generate stream ID
            stream_id = self._generate_stream_id(call_id, playback_type)
            
            # Initialize jitter buffer sized from config
            try:
                chunk_ms = max(1, int(self.chunk_size_ms))
                jb_ms = max(0, int(self.jitter_buffer_ms))
                jb_chunks = max(1, int(math.ceil(jb_ms / chunk_ms)))
            except Exception:
                jb_chunks = 10
            jitter_buffer = asyncio.Queue(maxsize=jb_chunks)
            self.jitter_buffers[call_id] = jitter_buffer
            # Derive per-stream warm-up thresholds so we never demand
            # more buffered chunks than the queue can hold.
            configured_min_start = (
                self.greeting_min_start_chunks if playback_type == "greeting" else self.min_start_chunks
            )
            # Always leave at least one spare slot so playback does not immediately
            # fall below the watermark on the first frame.
            max_startable = max(1, jb_chunks - 1)
            min_start_chunks = max(1, min(configured_min_start, max_startable))
            if configured_min_start > min_start_chunks:
                logger.debug(
                    "Streaming min_start clamped",
                    call_id=call_id,
                    playback_type=playback_type,
                    configured_chunks=configured_min_start,
                    jitter_chunks=jb_chunks,
                    applied_chunks=min_start_chunks,
                )

            configured_low_watermark = self.low_watermark_chunks
            low_watermark_chunks = 0
            if configured_low_watermark:
                max_low = max(0, min_start_chunks - 1)
                half_capacity = max(0, jb_chunks // 2)
                effective_cap = max(0, min(max_low, half_capacity))
                low_watermark_chunks = min(configured_low_watermark, effective_cap)
                if configured_low_watermark > low_watermark_chunks:
                    logger.debug(
                        "Streaming low_watermark clamped",
                        call_id=call_id,
                        playback_type=playback_type,
                        configured_chunks=configured_low_watermark,
                        jitter_chunks=jb_chunks,
                        applied_chunks=low_watermark_chunks,
                        min_start_chunks=min_start_chunks,
                    )

            # Mark streaming active in metrics and session
            _STREAMING_ACTIVE_GAUGE.labels(call_id).set(1)
            if session:
                session.streaming_started = True
                session.current_stream_id = stream_id
                await self.session_store.upsert_call(session)
            
            # Set TTS gating before starting stream
            gating_success = True
            if self.conversation_coordinator:
                gating_success = await self.conversation_coordinator.on_tts_start(call_id, stream_id)
            else:
                gating_success = await self.session_store.set_gating_token(call_id, stream_id)

            if not gating_success:
                logger.error("Failed to start streaming gating",
                           call_id=call_id,
                           stream_id=stream_id)
                return None
            
            # Start streaming task
            streaming_task = asyncio.create_task(
                self._stream_audio_loop(call_id, stream_id, audio_chunks, jitter_buffer)
            )
            
            # Start keepalive task
            keepalive_task = asyncio.create_task(
                self._keepalive_loop(call_id, stream_id)
            )
            self.keepalive_tasks[call_id] = keepalive_task
            
            src_encoding = self._canonicalize_encoding(source_encoding) or "slin16"
            try:
                src_rate = int(source_sample_rate) if source_sample_rate is not None else self.sample_rate
            except Exception:
                src_rate = self.sample_rate

            # Determine downstream target format/sample rate for this stream.
            resolved_target_format = (
                self._canonicalize_encoding(target_encoding)
                or self._canonicalize_encoding(self.audiosocket_format)
                or "ulaw"
            )
            try:
                resolved_target_rate = (
                    int(target_sample_rate)
                    if target_sample_rate is not None
                    else int(self.sample_rate)
                )
            except Exception:
                resolved_target_rate = self.sample_rate
            if resolved_target_rate <= 0:
                resolved_target_rate = self._default_sample_rate_for_format(
                    resolved_target_format,
                    self.sample_rate,
                )
            mulaw_transport = self._is_mulaw(self.audiosocket_format)
            if self.egress_force_mulaw and mulaw_transport:
                resolved_target_format = "ulaw"
                resolved_target_rate = 8000
            elif not self._is_mulaw(resolved_target_format) and mulaw_transport and target_sample_rate is None:
                resolved_target_rate = self._default_sample_rate_for_format(
                    self.audiosocket_format,
                    resolved_target_rate,
                )

            self._resample_states[call_id] = None
            # Store stream info
            # Determine if egress slin16 should be byteswapped based on mode and inbound probe
            mode = self.egress_swap_mode
            egress_swap_auto = False
            try:
                if self._canonicalize_encoding(self.audiosocket_format) in {"slin16", "linear16", "pcm16"}:
                    egress_swap_auto = bool(session.vad_state.get("pcm16_inbound_swap", False))
            except Exception:
                egress_swap_auto = False
            if mode == 'force_true':
                egress_swap = True
            elif mode == 'force_false':
                egress_swap = False
            else:
                egress_swap = egress_swap_auto

            self.active_streams[call_id] = {
                'stream_id': stream_id,
                'playback_type': playback_type,
                'streaming_task': streaming_task,
                'keepalive_task': keepalive_task,
                'start_time': time.time(),
                'chunks_sent': 0,
                'last_chunk_time': time.time(),
                'startup_ready': False,
                'first_frame_observed': False,
                'min_start_chunks': min_start_chunks,
                'low_watermark_chunks': low_watermark_chunks,
                'jitter_buffer_chunks': jb_chunks,
                'buffered_bytes': 0,
                'end_reason': None,
                'source_encoding': src_encoding,
                'source_sample_rate': src_rate,
                'egress_swap': egress_swap,
                'egress_swap_mode': mode,
                'target_format': resolved_target_format,
                'target_sample_rate': resolved_target_rate,
                'tx_bytes': 0,
                'egress_force_mulaw': self.egress_force_mulaw,
            }
            self._startup_ready[call_id] = False
            try:
                _STREAM_STARTED_TOTAL.labels(call_id, playback_type).inc()
            except Exception:
                pass
            
            logger.info("🎵 STREAMING PLAYBACK - Started",
                       call_id=call_id,
                       stream_id=stream_id,
                       playback_type=playback_type)

            # Outbound setup probe
            try:
                logger.info(
                    "🎵 STREAMING OUTBOUND - Setup",
                    call_id=call_id,
                    stream_id=stream_id,
                    source_encoding=src_encoding,
                    source_sample_rate=src_rate,
                    target_format=resolved_target_format,
                    target_sample_rate=resolved_target_rate,
                    egress_swap=egress_swap,
                    egress_swap_mode=mode,
                )
            except Exception:
                pass
            
            return stream_id
            
        except Exception as e:
            logger.error("Error starting streaming playback",
                        call_id=call_id,
                        playback_type=playback_type,
                        error=str(e),
                        exc_info=True)
            return None
    
    async def _stream_audio_loop(
        self, 
        call_id: str, 
        stream_id: str, 
        audio_chunks: asyncio.Queue,
        jitter_buffer: asyncio.Queue
    ) -> None:
        """Main streaming loop that processes audio chunks."""
        try:
            fallback_timeout = self.fallback_timeout_ms / 1000.0
            last_send_time = time.time()
            
            while True:
                try:
                    # Wait for audio chunk with timeout
                    chunk = await asyncio.wait_for(
                        audio_chunks.get(), 
                        timeout=fallback_timeout
                    )
                    
                    if chunk is None:  # End of stream signal
                        logger.info("🎵 STREAMING PLAYBACK - End of stream",
                                   call_id=call_id,
                                   stream_id=stream_id)
                        try:
                            if call_id in self.active_streams:
                                self.active_streams[call_id]['end_reason'] = 'end-of-stream'
                        except Exception:
                            pass
                        break
                    
                    # Update timing
                    now = time.time()
                    last_send_time = now
                    if call_id in self.active_streams:
                        self.active_streams[call_id]['last_chunk_time'] = now
                        self.active_streams[call_id]['chunks_sent'] += 1
                    # Update metrics and session counters for queued chunk
                    try:
                        _STREAMING_BYTES_TOTAL.labels(call_id).inc(len(chunk))
                        _STREAMING_JITTER_DEPTH.labels(call_id).set(jitter_buffer.qsize())
                        _STREAMING_LAST_CHUNK_AGE.labels(call_id).set(0.0)
                        sess = await self.session_store.get_by_call_id(call_id)
                        if sess:
                            sess.streaming_bytes_sent += len(chunk)
                            sess.streaming_jitter_buffer_depth = jitter_buffer.qsize()
                            await self.session_store.upsert_call(sess)
                    except Exception:
                        logger.debug("Streaming metrics update failed", call_id=call_id)

                    # Add to jitter buffer
                    await jitter_buffer.put(chunk)
                    try:
                        if call_id in self.active_streams:
                            info = self.active_streams[call_id]
                            info['buffered_bytes'] = int(info.get('buffered_bytes', 0)) + len(chunk)
                    except Exception:
                        pass

                    # Process jitter buffer
                    success = await self._process_jitter_buffer(call_id, stream_id, jitter_buffer)
                    if not success:
                        await self._record_fallback(call_id, "transport-failure")
                        await self._fallback_to_file_playback(call_id, stream_id)
                        try:
                            if call_id in self.active_streams:
                                self.active_streams[call_id]['end_reason'] = 'transport-failure'
                        except Exception:
                            pass
                        break
                    
                except asyncio.TimeoutError:
                    # No audio chunk received within timeout
                    if time.time() - last_send_time > fallback_timeout:
                        logger.warning("🎵 STREAMING PLAYBACK - Timeout, falling back to file playback",
                                     call_id=call_id,
                                     stream_id=stream_id,
                                     timeout=fallback_timeout)
                        await self._record_fallback(call_id, f"timeout>{fallback_timeout}s")
                        await self._fallback_to_file_playback(call_id, stream_id)
                        break
                    continue
                    
        except Exception as e:
            logger.error("Error in streaming audio loop",
                        call_id=call_id,
                        stream_id=stream_id,
                        error=str(e),
                        exc_info=True)
            await self._record_fallback(call_id, str(e))
            await self._fallback_to_file_playback(call_id, stream_id)
        finally:
            await self._cleanup_stream(call_id, stream_id)
    
    async def _process_jitter_buffer(
        self,
        call_id: str,
        stream_id: str,
        jitter_buffer: asyncio.Queue
    ) -> bool:
        """Process audio chunks from jitter buffer."""
        try:
            stream_info = self.active_streams.get(call_id, {}) if call_id in self.active_streams else {}
            target_fmt = (
                self._canonicalize_encoding(stream_info.get("target_format"))
                or self._canonicalize_encoding(self.audiosocket_format)
                or "ulaw"
            )
            try:
                target_rate = int(stream_info.get("target_sample_rate", self.sample_rate))
            except Exception:
                target_rate = int(self.sample_rate)
            if target_rate <= 0:
                target_rate = self._default_sample_rate_for_format(target_fmt, int(self.sample_rate))

            # Hold playback until jitter buffer has the minimum startup chunks
            ready = self._startup_ready.get(call_id, False)
            if not ready:
                min_need = self.min_start_chunks
                try:
                    if call_id in self.active_streams:
                        min_need = int(self.active_streams[call_id].get('min_start_chunks', self.min_start_chunks))
                except Exception:
                    min_need = self.min_start_chunks
                available_frames = self._estimate_available_frames(call_id, jitter_buffer, include_remainder=True)
                if available_frames < min_need:
                    return True
                self._startup_ready[call_id] = True
                if call_id in self.active_streams:
                    self.active_streams[call_id]['startup_ready'] = True
                logger.debug(
                    "Streaming jitter buffer warm-up complete",
                    call_id=call_id,
                    stream_id=stream_id,
                    buffered_chunks=jitter_buffer.qsize(),
                )

            # Process available chunks with pacing to avoid flooding Asterisk
            while not jitter_buffer.empty():
                # Low watermark check: if depth drops below threshold, pause to rebuild buffer
                low_watermark_chunks = self._get_low_watermark_frames(call_id)

                if (
                    low_watermark_chunks
                    and self._estimate_available_frames(call_id, jitter_buffer, include_remainder=True) <= low_watermark_chunks
                    and self._startup_ready.get(call_id, False)
                ):
                    logger.debug("Streaming jitter buffer low watermark pause",
                                 call_id=call_id,
                                 stream_id=stream_id,
                                 buffered_frames=self._estimate_available_frames(call_id, jitter_buffer, include_remainder=False),
                                 low_watermark=low_watermark_chunks)
                    await asyncio.sleep(self.chunk_size_ms / 1000.0)
                    _STREAMING_JITTER_DEPTH.labels(call_id).set(jitter_buffer.qsize())
                    break
                chunk = jitter_buffer.get_nowait()

                # Convert audio format if needed
                processed_chunk = await self._process_audio_chunk(call_id, chunk)
                if not processed_chunk:
                    self._decrement_buffered_bytes(call_id, len(chunk))
                    continue

                if self.audio_transport == "audiosocket":
                    # Segment to fixed 20ms frames and pace sends
                    fmt = target_fmt
                    bytes_per_sample = 1 if fmt in ("ulaw", "mulaw", "mu-law") else 2
                    frame_size = int(target_rate * (self.chunk_size_ms / 1000.0) * bytes_per_sample)
                    if frame_size <= 0:
                        frame_size = 160 if bytes_per_sample == 1 else 320  # 8k@20ms

                    pending = self.frame_remainders.get(call_id, b"") + processed_chunk
                    offset = 0
                    total_len = len(pending)
                    while (total_len - offset) >= frame_size:
                        frame = pending[offset:offset + frame_size]
                        offset += frame_size
                        success = await self._send_audio_chunk(
                            call_id,
                            stream_id,
                            frame,
                            target_fmt=target_fmt,
                            target_rate=target_rate,
                        )
                        if not success:
                            return False
                        self._decrement_buffered_bytes(call_id, frame_size)
                        # Pacing: sleep for chunk duration to avoid overrun
                        await asyncio.sleep(self.chunk_size_ms / 1000.0)

                    # Save remainder for next round
                    self.frame_remainders[call_id] = pending[offset:]
                else:
                    # ExternalMedia/RTP path: send as-is (RTP layer handles timing)
                    success = await self._send_audio_chunk(
                        call_id,
                        stream_id,
                        processed_chunk,
                        target_fmt=target_fmt,
                        target_rate=target_rate,
                    )
                    if not success:
                        return False
                    # Treat entire chunk as consumed bytes
                    self._decrement_buffered_bytes(call_id, len(processed_chunk))

        except Exception as e:
            logger.error("Error processing jitter buffer",
                        call_id=call_id,
                        error=str(e))
            return False

        return True
    
    async def _process_audio_chunk(self, call_id: str, chunk: bytes) -> Optional[bytes]:
        """Process audio chunk for streaming transport."""
        if not chunk:
            return None

        # ExternalMedia/RTP path: pass-through (conversion handled by RTP layer)
        if self.audio_transport != "audiosocket":
            return chunk

        try:
            stream_info = self.active_streams.get(call_id, {}) if call_id in self.active_streams else {}

            target_fmt = (
                self._canonicalize_encoding(stream_info.get("target_format"))
                or self._canonicalize_encoding(self.audiosocket_format)
                or "ulaw"
            )
            try:
                target_rate = int(stream_info.get("target_sample_rate", self.sample_rate))
            except Exception:
                target_rate = int(self.sample_rate)
            if target_rate <= 0:
                target_rate = self._default_sample_rate_for_format(target_fmt, int(self.sample_rate))

            src_encoding_raw = self._canonicalize_encoding(stream_info.get("source_encoding"))
            try:
                src_rate = int(stream_info.get("source_sample_rate") or target_rate)
            except Exception:
                src_rate = target_rate
            if not src_encoding_raw:
                src_encoding_raw = "slin16"

            # Determine if we must swap bytes for PCM16 egress
            egress_swap = bool(stream_info.get('egress_swap', False))
            mode = (stream_info.get('egress_swap_mode') or self.egress_swap_mode).lower()

            # Fast path: already matches target format and rate
            if (
                self._is_mulaw(src_encoding_raw)
                and self._is_mulaw(target_fmt)
                and src_rate == target_rate
            ):
                self._resample_states[call_id] = None
                return chunk
            if (
                src_encoding_raw in ("slin16", "linear16", "pcm16")
                and target_fmt in ("slin16", "linear16", "pcm16")
                and src_rate == target_rate
            ):
                # Fast path PCM16->PCM16: still apply egress swap if required (with auto-probe)
                self._resample_states[call_id] = None
                return self._apply_pcm_endianness(call_id, chunk, stream_info, mode)

            working = chunk
            resample_state = self._resample_states.get(call_id)

            # Convert source to PCM16 for resampling/format conversion when needed
            if self._is_mulaw(src_encoding_raw):
                working = mulaw_to_pcm16le(working)
                src_encoding = "pcm16"
            else:
                src_encoding = "pcm16"

            # Resample to target rate when necessary
            if src_rate != target_rate:
                working, resample_state = resample_audio(
                    working,
                    src_rate,
                    target_rate,
                    state=resample_state,
                )
            else:
                resample_state = None
            self._resample_states[call_id] = resample_state

            # Convert to target encoding
            if self._is_mulaw(target_fmt):
                return pcm16le_to_mulaw(working)
            # Otherwise target PCM16, with optional (or auto) egress byteswap
            return self._apply_pcm_endianness(call_id, working, stream_info, mode)
        except Exception as exc:
            logger.error(
                "Audio chunk processing failed",
                call_id=call_id,
                error=str(exc),
                exc_info=True,
            )
            return None

    async def _send_audio_chunk(
        self,
        call_id: str,
        stream_id: str,
        chunk: bytes,
        *,
        target_fmt: Optional[str] = None,
        target_rate: Optional[int] = None,
    ) -> bool:
        """Send audio chunk via configured streaming transport."""
        try:
            session = await self.session_store.get_by_call_id(call_id)
            if not session:
                logger.warning("Cannot stream audio - session not found", call_id=call_id)
                return False
            stream_info = self.active_streams.get(call_id, {})
            if self.audio_diag_callback:
                try:
                    effective_fmt = (
                        self._canonicalize_encoding(target_fmt)
                        or self._canonicalize_encoding(stream_info.get("target_format"))
                        or self._canonicalize_encoding(self.audiosocket_format)
                        or "ulaw"
                    )
                    try:
                        effective_rate = int(
                            target_rate
                            or stream_info.get("target_sample_rate")
                            or self.sample_rate
                        )
                    except Exception:
                        effective_rate = self.sample_rate
                    if effective_rate <= 0:
                        effective_rate = self._default_sample_rate_for_format(effective_fmt, self.sample_rate)
                    stage = f"transport_out:{stream_info.get('playback_type', 'response')}"
                    await self.audio_diag_callback(call_id, stage, chunk, effective_fmt, effective_rate)
                except Exception:
                    logger.debug("Streaming diagnostics callback failed", call_id=call_id, exc_info=True)

            if self.audio_transport == "externalmedia":
                if not self.rtp_server:
                    logger.warning("Streaming transport unavailable (no RTP server)", call_id=call_id)
                    return False

                ssrc = getattr(session, "ssrc", None)
                success = await self.rtp_server.send_audio(call_id, chunk, ssrc=ssrc)
                if not success:
                    logger.warning("RTP streaming send failed", call_id=call_id, stream_id=stream_id)
                else:
                    try:
                        _STREAM_TX_BYTES.labels(call_id).inc(len(chunk))
                        if call_id in self.active_streams:
                            self.active_streams[call_id]['tx_bytes'] = int(self.active_streams[call_id].get('tx_bytes', 0)) + len(chunk)
                    except Exception:
                        pass
                return success

            if self.audio_transport == "audiosocket":
                if not self.audiosocket_server:
                    logger.warning("Streaming transport unavailable (no AudioSocket server)", call_id=call_id)
                    return False
                conn_id = getattr(session, "audiosocket_conn_id", None)
                if not conn_id:
                    logger.warning("Streaming transport missing AudioSocket connection", call_id=call_id)
                    return False
                # One-time debug for first outbound frame to identify codec/format
                if call_id not in self._first_send_logged:
                    fmt = (
                        self._canonicalize_encoding(target_fmt)
                        or self._canonicalize_encoding(self.audiosocket_format)
                        or "ulaw"
                    )
                    try:
                        sample_rate = int(target_rate if target_rate is not None else self.sample_rate)
                    except Exception:
                        sample_rate = self.sample_rate
                    if sample_rate <= 0:
                        sample_rate = self._default_sample_rate_for_format(fmt, self.sample_rate)
                    try:
                        egress_swap = bool(self.active_streams.get(call_id, {}).get('egress_swap', False))
                    except Exception:
                        egress_swap = False
                    try:
                        egress_mode = str(self.active_streams.get(call_id, {}).get('egress_swap_mode', self.egress_swap_mode))
                    except Exception:
                        egress_mode = self.egress_swap_mode
                    logger.info(
                        "🎵 STREAMING OUTBOUND - First frame",
                        call_id=call_id,
                        stream_id=stream_id,
                        transport=self.audio_transport,
                        audiosocket_format=fmt,
                        frame_bytes=len(chunk),
                        sample_rate=sample_rate,
                        chunk_size_ms=self.chunk_size_ms,
                        egress_swap=egress_swap,
                        egress_swap_mode=egress_mode,
                        conn_id=conn_id,
                    )
                    self._first_send_logged.add(call_id)
                # Optional broadcast mode for diagnostics
                if self.audiosocket_broadcast_debug:
                    conns = list(set(getattr(session, 'audiosocket_conns', []) or []))
                    sent = 0
                    for cid in conns or [conn_id]:
                        if await self.audiosocket_server.send_audio(cid, chunk):
                            sent += 1
                    if sent == 0:
                        logger.warning("AudioSocket broadcast send failed (no recipients)", call_id=call_id, stream_id=stream_id)
                        return False
                    if len(conns) > 1:
                        logger.debug("AudioSocket broadcast sent", call_id=call_id, stream_id=stream_id, recipients=len(conns))
                    return True
                # Normal single-conn send
                success = await self.audiosocket_server.send_audio(conn_id, chunk)
                if not success:
                    logger.warning("AudioSocket streaming send failed", call_id=call_id, stream_id=stream_id)
                else:
                    try:
                        _STREAM_TX_BYTES.labels(call_id).inc(len(chunk))
                        if call_id in self.active_streams:
                            self.active_streams[call_id]['tx_bytes'] = int(self.active_streams[call_id].get('tx_bytes', 0)) + len(chunk)
                    except Exception:
                        pass
                # First-frame observability
                try:
                    if call_id in self.active_streams and not self.active_streams[call_id].get('first_frame_observed', False) and success:
                        start_time = float(self.active_streams[call_id].get('start_time', time.time()))
                        pb_type = str(self.active_streams[call_id].get('playback_type', 'response'))
                        first_s = max(0.0, time.time() - start_time)
                        _STREAM_FIRST_FRAME_SECONDS.labels(call_id, pb_type).observe(first_s)
                        self.active_streams[call_id]['first_frame_observed'] = True
                except Exception:
                    pass
                return success

            logger.warning("Streaming transport not implemented for audio_transport",
                           call_id=call_id,
                           audio_transport=self.audio_transport)
            return False

        except Exception as e:
            logger.error("Error sending streaming audio chunk",
                        call_id=call_id,
                        stream_id=stream_id,
                        error=str(e),
                        exc_info=True)
            return False

    def _apply_pcm_endianness(
        self,
        call_id: str,
        pcm_bytes: bytes,
        stream_info: Dict[str, Any],
        mode: str,
    ) -> bytes:
        """Ensure PCM16 egress matches the negotiated byte order with auto correction."""
        if not pcm_bytes:
            return pcm_bytes

        target_fmt = (
            self._canonicalize_encoding(stream_info.get('target_format'))
            or self._canonicalize_encoding(self.audiosocket_format)
            or "ulaw"
        )
        if target_fmt not in ("slin16", "linear16", "pcm16"):
            if call_id and not stream_info.get('egress_swap_skip_logged', False):
                stream_info['egress_swap_skip_logged'] = True
                try:
                    logger.info(
                        "Skipping PCM endianness check for non-PCM target",
                        call_id=call_id,
                        stream_id=stream_info.get('stream_id'),
                        target_format=target_fmt,
                    )
                except Exception:
                    pass
            return pcm_bytes

        mode = (mode or "auto").lower()
        egress_swap = bool(stream_info.get('egress_swap', False))
        stream_id = stream_info.get('stream_id')

        probe_needed = not stream_info.get('egress_probe_done', False)
        swapped_bytes: Optional[bytes] = None
        rms_native = rms_swapped = 0

        if probe_needed or mode == "force_true":
            try:
                rms_native = audioop.rms(pcm_bytes, 2)
            except Exception:
                rms_native = 0
            try:
                swapped_bytes = audioop.byteswap(pcm_bytes, 2)
                rms_swapped = audioop.rms(swapped_bytes, 2)
            except Exception:
                swapped_bytes = None
                rms_swapped = 0

            if probe_needed:
                stream_info['egress_probe_done'] = True
                try:
                    logger.info(
                        "🎵 STREAMING OUTBOUND - Probe",
                        call_id=call_id,
                        stream_id=stream_id,
                        audiosocket_format=target_fmt,
                        egress_swap=egress_swap,
                        egress_swap_mode=mode,
                        rms_native=rms_native,
                        rms_swapped=rms_swapped,
                        target_sample_rate=stream_info.get('target_sample_rate', self.sample_rate),
                    )
                except Exception:
                    pass

                if mode != "force_false" and swapped_bytes is not None:
                    threshold = max(512, 4 * max(1, rms_native))
                    if not egress_swap and rms_swapped >= threshold:
                        stream_info['egress_swap'] = True
                        stream_info['egress_swap_auto'] = True
                        egress_swap = True
                        try:
                            if call_id:
                                _STREAM_ENDIAN_CORRECTIONS_TOTAL.labels(call_id, mode).inc()
                        except Exception:
                            pass
                        try:
                            logger.warning(
                                "Auto-correcting PCM16 egress endianness",
                                call_id=call_id,
                                stream_id=stream_id,
                                egress_swap_mode=mode,
                                rms_native=rms_native,
                                rms_swapped=rms_swapped,
                                threshold=threshold,
                            )
                        except Exception:
                            pass
                        # If we already have swapped bytes from the probe, reuse it.
                        if swapped_bytes is not None:
                            return swapped_bytes

            if mode == "force_true" and not egress_swap:
                stream_info['egress_swap'] = True
                egress_swap = True
                if swapped_bytes is not None:
                    return swapped_bytes

        if egress_swap:
            try:
                return audioop.byteswap(pcm_bytes, 2)
            except Exception:
                logger.debug("PCM16 egress swap failed; sending native bytes", call_id=call_id)

        return pcm_bytes

    def _frame_size_bytes(self, call_id: Optional[str] = None) -> int:
        fmt = (
            self._canonicalize_encoding(self.audiosocket_format)
            or "ulaw"
        )
        sample_rate = self.sample_rate
        if call_id and call_id in self.active_streams:
            info = self.active_streams.get(call_id, {})
            fmt = (
                self._canonicalize_encoding(info.get('target_format'))
                or fmt
            )
            try:
                sr = int(info.get('target_sample_rate', sample_rate))
            except Exception:
                sr = sample_rate
            if sr > 0:
                sample_rate = sr
        bytes_per_sample = 1 if self._is_mulaw(fmt) else 2
        frame_size = int(sample_rate * (self.chunk_size_ms / 1000.0) * bytes_per_sample)
        if frame_size <= 0:
            frame_size = 160 if bytes_per_sample == 1 else 320
        return frame_size

    def _estimate_available_frames(
        self,
        call_id: str,
        jitter_buffer: asyncio.Queue,
        *,
        include_remainder: bool = False,
    ) -> int:
        frame_size = self._frame_size_bytes(call_id)
        try:
            info = self.active_streams.get(call_id, {})
            buffered_bytes = int(info.get('buffered_bytes', 0))
        except Exception:
            buffered_bytes = 0

        if buffered_bytes <= 0:
            # Approximate using queue depth when buffered_bytes not yet initialised
            buffered_bytes = jitter_buffer.qsize() * frame_size

        if include_remainder:
            remainder = self.frame_remainders.get(call_id, b"")
            if remainder:
                buffered_bytes += len(remainder)

        frames = int(buffered_bytes / max(1, frame_size))
        return max(0, frames)

    def _get_low_watermark_frames(self, call_id: str) -> int:
        try:
            info = self.active_streams.get(call_id, {})
            lw = int(info.get('low_watermark_chunks', self.low_watermark_chunks))
        except Exception:
            lw = self.low_watermark_chunks
        return max(0, lw)

    def _decrement_buffered_bytes(self, call_id: str, byte_count: int) -> None:
        if byte_count <= 0:
            return
        try:
            info = self.active_streams.get(call_id)
            if info is None:
                return
            current = int(info.get('buffered_bytes', 0))
            info['buffered_bytes'] = max(0, current - byte_count)
        except Exception:
            pass

    def set_transport(
        self,
        *,
        rtp_server: Optional[Any] = None,
        audiosocket_server: Optional[Any] = None,
        audio_transport: Optional[str] = None,
        audiosocket_format: Optional[str] = None,
    ) -> None:
        """Configure transport dependencies after engine initialization."""
        if rtp_server is not None:
            self.rtp_server = rtp_server
        if audiosocket_server is not None:
            self.audiosocket_server = audiosocket_server
        if audio_transport is not None:
            self.audio_transport = audio_transport
        if audiosocket_format is not None:
            self.audiosocket_format = audiosocket_format

    async def _record_fallback(self, call_id: str, reason: str) -> None:
        """Increment fallback counters and persist the last error."""
        try:
            _STREAMING_FALLBACKS_TOTAL.labels(call_id).inc()
            sess = await self.session_store.get_by_call_id(call_id)
            if sess:
                sess.streaming_fallback_count += 1
                sess.last_streaming_error = reason
                await self.session_store.upsert_call(sess)
        except Exception:
            logger.debug("Failed to record streaming fallback", call_id=call_id, reason=reason, exc_info=True)
    
    async def _fallback_to_file_playback(
        self, 
        call_id: str, 
        stream_id: str
    ) -> None:
        """Fallback to file-based playback when streaming fails."""
        try:
            if not self.fallback_playback_manager:
                logger.error("No fallback playback manager available",
                           call_id=call_id,
                           stream_id=stream_id)
                return
            
            # Get session
            session = await self.session_store.get_by_call_id(call_id)
            if not session:
                logger.error("Cannot fallback - session not found",
                           call_id=call_id)
                return
            
            # Collect any remaining audio chunks
            remaining_audio = bytearray()
            if call_id in self.jitter_buffers:
                jitter_buffer = self.jitter_buffers[call_id]
                while not jitter_buffer.empty():
                    chunk = jitter_buffer.get_nowait()
                    if chunk:
                        remaining_audio.extend(chunk)
                        self._decrement_buffered_bytes(call_id, len(chunk))
            
            if remaining_audio:
                raw_buf = bytes(remaining_audio)

                # Convert provider-encoded buffer to μ-law @ 8 kHz for Asterisk file playback
                try:
                    info = self.active_streams.get(call_id, {})
                    src_encoding = (
                        self._canonicalize_encoding(info.get('source_encoding'))
                        or 'slin16'
                    )
                    try:
                        src_rate = int(info.get('source_sample_rate') or 0) or self.sample_rate
                    except Exception:
                        src_rate = self.sample_rate

                    # Normalize to PCM16
                    if self._is_mulaw(src_encoding):
                        pcm = mulaw_to_pcm16le(raw_buf)
                        src_rate = 8000
                    else:
                        pcm = raw_buf

                    # Resample to 8 kHz for μ-law file playback
                    if src_rate != 8000:
                        pcm, _ = resample_audio(pcm, src_rate, 8000)

                    # Convert to μ-law
                    mulaw_audio = pcm16le_to_mulaw(pcm)
                except Exception:
                    logger.warning("Fallback conversion failed; passing raw bytes to file playback",
                                   call_id=call_id,
                                   stream_id=stream_id,
                                   exc_info=True)
                    mulaw_audio = raw_buf

                # Use fallback playback manager
                fallback_playback_id = await self.fallback_playback_manager.play_audio(
                    call_id,
                    mulaw_audio,
                    "streaming-fallback"
                )
                
                if fallback_playback_id:
                    logger.info("🎵 STREAMING FALLBACK - Switched to file playback",
                               call_id=call_id,
                               stream_id=stream_id,
                               fallback_id=fallback_playback_id)
                else:
                    logger.error("Failed to start fallback file playback",
                               call_id=call_id,
                               stream_id=stream_id)
            
        except Exception as e:
            logger.error("Error in fallback to file playback",
                        call_id=call_id,
                        stream_id=stream_id,
                        error=str(e),
                        exc_info=True)
    
    async def _keepalive_loop(self, call_id: str, stream_id: str) -> None:
        """Keepalive loop to maintain streaming connection."""
        try:
            while call_id in self.active_streams:
                await asyncio.sleep(self.keepalive_interval_ms / 1000.0)
                
                # Check if stream is still active
                if call_id not in self.active_streams:
                    break
                
                # Check for timeout
                stream_info = self.active_streams[call_id]
                time_since_last_chunk = time.time() - stream_info['last_chunk_time']
                _STREAMING_LAST_CHUNK_AGE.labels(call_id).set(max(0.0, time_since_last_chunk))
                _STREAMING_KEEPALIVES_SENT_TOTAL.labels(call_id).inc()
                try:
                    sess = await self.session_store.get_by_call_id(call_id)
                    if sess:
                        sess.streaming_keepalive_sent += 1
                        await self.session_store.upsert_call(sess)
                except Exception:
                    pass
                
                if time_since_last_chunk > (self.connection_timeout_ms / 1000.0):
                    logger.warning("🎵 STREAMING PLAYBACK - Connection timeout",
                                 call_id=call_id,
                                 stream_id=stream_id,
                                 time_since_last_chunk=time_since_last_chunk)
                    _STREAMING_KEEPALIVE_TIMEOUTS_TOTAL.labels(call_id).inc()
                    try:
                        if call_id in self.active_streams:
                            self.active_streams[call_id]['end_reason'] = 'keepalive-timeout'
                    except Exception:
                        pass
                    try:
                        sess = await self.session_store.get_by_call_id(call_id)
                        if sess:
                            sess.streaming_keepalive_timeouts += 1
                            sess.last_streaming_error = f"keepalive-timeout>{time_since_last_chunk:.2f}s"
                            await self.session_store.upsert_call(sess)
                    except Exception:
                        pass
                    await self._fallback_to_file_playback(call_id, stream_id)
                    break
                
                # Send keepalive (placeholder)
                logger.debug("🎵 STREAMING KEEPALIVE - Sending keepalive",
                           call_id=call_id,
                           stream_id=stream_id)
        
        except asyncio.CancelledError:
            logger.debug("Keepalive loop cancelled",
                        call_id=call_id,
                        stream_id=stream_id)
        except Exception as e:
            logger.error("Error in keepalive loop",
                        call_id=call_id,
                        stream_id=stream_id,
                        error=str(e))
    
    async def stop_streaming_playback(self, call_id: str) -> bool:
        """Stop streaming playback for a call."""
        try:
            if call_id not in self.active_streams:
                logger.warning("Cannot stop streaming - no active stream",
                             call_id=call_id)
                return False
            
            stream_info = self.active_streams[call_id]
            stream_id = stream_info['stream_id']
            
            # Cancel streaming task
            if 'streaming_task' in stream_info:
                stream_info['streaming_task'].cancel()
            
            # Cancel keepalive task
            if call_id in self.keepalive_tasks:
                self.keepalive_tasks[call_id].cancel()
                del self.keepalive_tasks[call_id]
            
            # Cleanup
            await self._cleanup_stream(call_id, stream_id)
            
            logger.info("🎵 STREAMING PLAYBACK - Stopped",
                       call_id=call_id,
                       stream_id=stream_id)
            
            return True
            
        except Exception as e:
            logger.error("Error stopping streaming playback",
                        call_id=call_id,
                        error=str(e),
                        exc_info=True)
            return False
    
    async def _cleanup_stream(self, call_id: str, stream_id: str) -> None:
        """Clean up streaming resources."""
        try:
            # Before clearing gating/state, give provider a grace period and flush any remaining audio
            # to avoid chopping off the tail of the playback.
            try:
                if self.provider_grace_ms:
                    await asyncio.sleep(self.provider_grace_ms / 1000.0)
            except Exception:
                pass

            # Flush any remainder bytes as a final frame
            try:
                rem = self.frame_remainders.get(call_id, b"") or b""
                if rem:
                    self._decrement_buffered_bytes(call_id, len(rem))
                    if self.audio_transport == "audiosocket":
                        fmt = (
                            self._canonicalize_encoding(self.audiosocket_format)
                            or "ulaw"
                        )
                        info = self.active_streams.get(call_id, {})
                        fmt = (
                            self._canonicalize_encoding(info.get('target_format'))
                            or fmt
                        )
                        try:
                            sr = int(info.get('target_sample_rate', self.sample_rate))
                        except Exception:
                            sr = self.sample_rate
                        if sr <= 0:
                            sr = self._default_sample_rate_for_format(fmt, self.sample_rate)
                        bytes_per_sample = 1 if self._is_mulaw(fmt) else 2
                        frame_size = int(sr * (self.chunk_size_ms / 1000.0) * bytes_per_sample) or (160 if bytes_per_sample == 1 else 320)
                        # Zero-pad to a full frame boundary to avoid truncation artifacts
                        if len(rem) < frame_size:
                            rem = rem + (b"\x00" * (frame_size - len(rem)))
                        await self._send_audio_chunk(call_id, stream_id, rem[:frame_size], target_fmt=fmt, target_rate=sr)
                        # small pacing to let Asterisk play the last frame
                        await asyncio.sleep(self.chunk_size_ms / 1000.0)
                    else:
                        await self._send_audio_chunk(call_id, stream_id, rem)
            except Exception:
                logger.debug("Remainder flush failed", call_id=call_id, stream_id=stream_id)

            # Clear TTS gating after flushing
            if self.conversation_coordinator:
                await self.conversation_coordinator.on_tts_end(
                    call_id, stream_id, "streaming-ended"
                )
                await self.conversation_coordinator.update_conversation_state(
                    call_id, "listening"
                )
            else:
                await self.session_store.clear_gating_token(call_id, stream_id)
            
            # Observe segment duration and end reason
            try:
                if call_id in self.active_streams:
                    info = self.active_streams[call_id]
                    pb_type = str(info.get('playback_type', 'response'))
                    dur = max(0.0, time.time() - float(info.get('start_time', time.time())))
                    _STREAM_SEGMENT_DURATION_SECONDS.labels(call_id, pb_type).observe(dur)
                    reason = str(info.get('end_reason') or 'streaming-ended')
                    _STREAM_END_REASON_TOTAL.labels(call_id, reason).inc()
            except Exception:
                pass

            # Emit tuning summary for observability BEFORE removing stream info
            try:
                if call_id in self.active_streams:
                    info = self.active_streams[call_id]
                    try:
                        fmt = (
                            self._canonicalize_encoding(self.audiosocket_format)
                            or "ulaw"
                        )
                        bps = 1 if self._is_mulaw(fmt) else 2
                        sr = max(1, int(self.sample_rate))
                        tx = int(info.get('tx_bytes', 0))
                        eff_seconds = float(tx) / float(max(1, bps * sr))
                    except Exception:
                        eff_seconds = 0.0
                    try:
                        start_ts = float(info.get('start_time', time.time()))
                        wall_seconds = max(0.0, time.time() - start_ts)
                    except Exception:
                        wall_seconds = 0.0
                    try:
                        drift_pct = 0.0 if wall_seconds <= 0.0 else ((eff_seconds - wall_seconds) / wall_seconds) * 100.0
                    except Exception:
                        drift_pct = 0.0
                    logger.info(
                        "🎛️ STREAMING TUNING SUMMARY",
                        call_id=call_id,
                        stream_id=stream_id,
                        bytes_sent=tx,
                        effective_seconds=round(eff_seconds, 3),
                        wall_seconds=round(wall_seconds, 3),
                        drift_pct=round(drift_pct, 1),
                        low_watermark=self.low_watermark_ms,
                        min_start=self.min_start_ms,
                        provider_grace_ms=self.provider_grace_ms,
                    )
            except Exception:
                logger.debug("Streaming tuning summary unavailable", call_id=call_id)
            # Remove from active streams
            if call_id in self.active_streams:
                del self.active_streams[call_id]
            
            # Clean up jitter buffer
            if call_id in self.jitter_buffers:
                del self.jitter_buffers[call_id]
            self._startup_ready.pop(call_id, None)
            self._resample_states.pop(call_id, None)
            # Reset metrics
            try:
                _STREAMING_ACTIVE_GAUGE.labels(call_id).set(0)
                _STREAMING_JITTER_DEPTH.labels(call_id).set(0)
                _STREAMING_LAST_CHUNK_AGE.labels(call_id).set(0)
            except Exception:
                pass
            
            # Reset session streaming flags
            try:
                sess = await self.session_store.get_by_call_id(call_id)
                if sess:
                    sess.streaming_started = False
                    sess.current_stream_id = None
                    await self.session_store.upsert_call(sess)
            except Exception:
                pass
            # Clear any remainder record after flushing
            self.frame_remainders.pop(call_id, None)
            
            
            
            logger.debug("Streaming cleanup completed",
                        call_id=call_id,
                        stream_id=stream_id)
            
        except Exception as e:
            logger.error("Error cleaning up stream",
                        call_id=call_id,
                        stream_id=stream_id,
                        error=str(e))
    
    def _generate_stream_id(self, call_id: str, playback_type: str) -> str:
        """Generate deterministic stream ID."""
        timestamp = int(time.time() * 1000)
        return f"stream:{playback_type}:{call_id}:{timestamp}"
    
    def is_stream_active(self, call_id: str) -> bool:
        """Return True if a streaming playback is active for the call."""
        info = self.active_streams.get(call_id)
        if not info:
            return False
        task = info.get('streaming_task')
        return task is not None and not task.done()

    async def get_active_streams(self) -> Dict[str, Dict[str, Any]]:
        """Get information about active streams."""
        return dict(self.active_streams)
    
    async def cleanup_expired_streams(self, max_age_seconds: float = 300) -> int:
        """Clean up expired streams."""
        current_time = time.time()
        expired_calls = []
        
        for call_id, stream_info in self.active_streams.items():
            age = current_time - stream_info['start_time']
            if age > max_age_seconds:
                expired_calls.append(call_id)
        
        for call_id in expired_calls:
            stream_info = self.active_streams[call_id]
            await self._cleanup_stream(call_id, stream_info['stream_id'])
        
        return len(expired_calls)

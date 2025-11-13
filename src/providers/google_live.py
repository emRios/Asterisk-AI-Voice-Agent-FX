"""
Google Gemini Live API provider implementation.

This module integrates Google's Gemini Live API (bidirectional streaming) into the
Asterisk AI Voice Agent. Audio from AudioSocket is resampled to PCM16 @ 16 kHz,
streamed to Gemini Live API, and PCM16 output is resampled to the configured
downstream AudioSocket format (µ-law or PCM16 8 kHz).

Key features:
- Real-time bidirectional voice streaming
- Native audio processing (no separate STT/TTS)
- Built-in Voice Activity Detection (VAD)
- Barge-in support
- Function calling / tool use
- Session management for long conversations
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import struct
import audioop
from typing import Any, Dict, Optional, List
from collections import deque

import websockets
from websockets import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from structlog import get_logger
from prometheus_client import Gauge, Counter

from .base import AIProviderInterface, ProviderCapabilities
from ..audio import (
    convert_pcm16le_to_target_format,
    mulaw_to_pcm16le,
    resample_audio,
)
from ..config import GoogleProviderConfig

# Tool calling support
from src.tools.registry import tool_registry
from src.tools.adapters.google import GoogleToolAdapter

logger = get_logger(__name__)

# Constants
_GEMINI_INPUT_RATE = 16000  # Gemini requires 16kHz input
_GEMINI_OUTPUT_RATE = 24000  # Gemini outputs 24kHz audio
_COMMIT_INTERVAL_SEC = 0.02  # 20ms chunks (320 bytes at 16kHz)
_KEEPALIVE_INTERVAL_SEC = 15.0
_WEBSOCKET_ENDPOINT = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"

# Metrics
_GOOGLE_LIVE_SESSIONS = Gauge(
    "ai_agent_google_live_active_sessions",
    "Number of active Google Live API sessions",
)
_GOOGLE_LIVE_AUDIO_SENT = Counter(
    "ai_agent_google_live_audio_bytes_sent",
    "Total audio bytes sent to Google Live API",
    labelnames=("call_id",),
)
_GOOGLE_LIVE_AUDIO_RECEIVED = Counter(
    "ai_agent_google_live_audio_bytes_received",
    "Total audio bytes received from Google Live API",
    labelnames=("call_id",),
)


class GoogleLiveProvider(AIProviderInterface):
    """
    Google Gemini Live API provider using bidirectional WebSocket streaming.

    Lifecycle:
    1. start_session(call_id) -> establishes WebSocket, sends setup message
    2. send_audio(bytes) -> converts AudioSocket frames to PCM16 16kHz, streams to Gemini
    3. Receive server responses: audio, text transcription, tool calls
    4. stop_session() -> closes WebSocket and cancels background tasks

    Audio flow:
    - Input: 8kHz µ-law → 16kHz PCM16 → Gemini Live API
    - Output: 24kHz PCM16 from Gemini → 8kHz µ-law/PCM16 → AudioSocket
    """

    def __init__(
        self,
        config: GoogleProviderConfig,
        on_event,
        gating_manager=None,
    ):
        super().__init__(on_event)
        self.config = config
        self.websocket: Optional[WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._send_lock = asyncio.Lock()
        self._gating_manager = gating_manager

        self._call_id: Optional[str] = None
        self._session_id: Optional[str] = None
        self._setup_complete: bool = False
        self._greeting_completed: bool = False
        self._in_audio_burst: bool = False
        
        # Audio buffering for resampling
        self._input_buffer = bytearray()
        self._output_buffer = bytearray()
        
        # Tool adapter
        self._tool_adapter: Optional[GoogleToolAdapter] = None
        
        # Conversation state
        self._conversation_history: List[Dict[str, Any]] = []
        
        # Metrics tracking
        self._session_start_time: Optional[float] = None

    @staticmethod
    def get_capabilities() -> Optional[ProviderCapabilities]:
        """Return capabilities of Google Live provider for transport orchestration."""
        return ProviderCapabilities(
            input_encodings=["ulaw", "pcm16"],  # μ-law or PCM16
            input_sample_rates_hz=[8000, 16000],  # Telephony or wideband
            output_encodings=["ulaw", "pcm16"],  # Output resampled to telephony
            output_sample_rates_hz=[8000, 16000, 24000],  # Gemini native is 24kHz
            preferred_chunk_ms=20,  # 20ms chunks for smooth streaming
            can_negotiate=True,  # Can adapt to different formats
        )
    
    @property
    def supported_codecs(self) -> List[str]:
        """Return list of supported audio codecs (μ-law for telephony)."""
        return ["ulaw"]

    async def start_session(
        self,
        call_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Start a Google Gemini Live API session.

        Args:
            call_id: Unique identifier for the call
            context: Optional context including system prompt, tools, etc.
        """
        self._call_id = call_id
        self._session_start_time = time.time()
        self._conversation_history = []
        self._setup_complete = False
        self._greeting_completed = False

        logger.info(
            "Starting Google Live session",
            call_id=call_id,
            model=self.config.llm_model,
        )

        # Build WebSocket URL with API key
        api_key = self.config.api_key or ""
        
        # Debug: Check API key
        if not api_key:
            logger.error(
                "GOOGLE_API_KEY not found! Cannot connect to Google Live API.",
                call_id=call_id,
            )
            raise ValueError("GOOGLE_API_KEY is required for Google Live provider")
        
        api_key_preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "<too_short>"
        logger.debug(
            "Connecting to Google Live API",
            call_id=call_id,
            endpoint=_WEBSOCKET_ENDPOINT,
            api_key_preview=api_key_preview,
        )
        
        ws_url = f"{_WEBSOCKET_ENDPOINT}?key={api_key}"

        try:
            # Establish WebSocket connection
            self.websocket = await websockets.connect(
                ws_url,
                subprotocols=["gemini-live"],
                max_size=10 * 1024 * 1024,  # 10MB max message size
            )
            
            _GOOGLE_LIVE_SESSIONS.inc()
            
            logger.info(
                "Google Live WebSocket connected",
                call_id=call_id,
            )

            # Send setup message to configure session
            await self._send_setup(context)

            # Start background tasks
            self._receive_task = asyncio.create_task(
                self._receive_loop(),
                name=f"google-live-receive-{call_id}",
            )
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name=f"google-live-keepalive-{call_id}",
            )

            logger.info(
                "Google Live session started",
                call_id=call_id,
            )

        except Exception as e:
            logger.error(
                "Failed to start Google Live session",
                call_id=call_id,
                error=str(e),
                exc_info=True,
            )
            await self.stop_session()
            raise

    async def _send_setup(self, context: Optional[Dict[str, Any]]) -> None:
        """Send session setup message to Gemini Live API."""
        system_prompt = None
        if context:
            system_prompt = context.get("system_prompt") or context.get("prompt")

        # Build generation config (per REAL working wire examples)
        # https://gist.github.com/quartzjer/9636066e96b4f904162df706210770e4
        # NOTE: responseModalities is a STRING not array, lowercase not uppercase
        generation_config = {
            "responseModalities": "audio",  # STRING (lowercase), not ["AUDIO", "TEXT"]
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": self.config.tts_voice_name or "Aoede"
                    }
                }
            },
        }

        # Detailed debug logging for speech configuration
        speech_cfg = generation_config.get("speechConfig", {})
        voice_cfg = speech_cfg.get("voiceConfig", {}).get("prebuiltVoiceConfig", {})
        logger.debug(
            "Google Live speech configuration",
            call_id=self._call_id,
            voice_name=voice_cfg.get("voiceName"),
            response_modalities=generation_config.get("responseModalities"),
        )

        # Build tools config if tools are available
        tools = []
        if context and context.get("tools"):
            self._tool_adapter = GoogleToolAdapter()
            tools = self._tool_adapter.format_tools(context["tools"])

        # Setup message
        setup_msg = {
            "setup": {
                "model": f"models/{self.config.llm_model}",
                "generation_config": generation_config,
            }
        }

        if system_prompt:
            setup_msg["setup"]["system_instruction"] = {
                "parts": [{"text": system_prompt}]
            }

        if tools:
            setup_msg["setup"]["tools"] = tools

        # Debug: Log setup message structure
        logger.debug(
            "Sending Google Live setup message",
            call_id=self._call_id,
            setup_keys=list(setup_msg.get("setup", {}).keys()),
            model=setup_msg.get("setup", {}).get("model"),
            has_system_instruction=bool(system_prompt),
            tools_count=len(tools),
            generation_config=generation_config,
        )

        await self._send_message(setup_msg)
        
        logger.info(
            "Sent Google Live setup",
            call_id=self._call_id,
            has_system_prompt=bool(system_prompt),
            tools_count=len(tools),
        )

    async def _send_message(self, message: Dict[str, Any]) -> None:
        """Send a message to Gemini Live API."""
        if not self.websocket:
            return

        async with self._send_lock:
            try:
                await self.websocket.send(json.dumps(message))
            except Exception as e:
                logger.error(
                    "Failed to send message to Google Live",
                    call_id=self._call_id,
                    error=str(e),
                )

    async def send_audio(self, audio_chunk: bytes, sample_rate: int = 8000) -> None:
        """
        Send audio chunk to Gemini Live API.

        Args:
            audio_chunk: Raw audio bytes (µ-law or PCM16)
            sample_rate: Sample rate of input audio (default 8000 Hz)
        """
        if not self.websocket or not self._setup_complete:
            return

        try:
            # Convert µ-law to PCM16 if needed
            if sample_rate == 8000 and len(audio_chunk) > 0:
                # Assume µ-law for 8kHz
                pcm16_8k = mulaw_to_pcm16le(audio_chunk)
            else:
                pcm16_8k = audio_chunk

            # Resample from 8kHz to 16kHz
            pcm16_16k = resample_audio(
                pcm16_8k,
                source_rate=sample_rate,
                target_rate=_GEMINI_INPUT_RATE,
            )

            # Add to buffer
            self._input_buffer.extend(pcm16_16k)

            # Send in chunks (20ms at 16kHz = 640 bytes)
            chunk_size = int(_GEMINI_INPUT_RATE * 2 * _COMMIT_INTERVAL_SEC)  # 2 bytes per sample
            
            while len(self._input_buffer) >= chunk_size:
                chunk_to_send = bytes(self._input_buffer[:chunk_size])
                self._input_buffer = self._input_buffer[chunk_size:]

                # Encode as base64
                audio_b64 = base64.b64encode(chunk_to_send).decode("utf-8")

                # Send realtime input
                message = {
                    "realtime_input": {
                        "media_chunks": [
                            {
                                "mime_type": "audio/pcm",
                                "data": audio_b64,
                            }
                        ]
                    }
                }

                await self._send_message(message)
                
                _GOOGLE_LIVE_AUDIO_SENT.labels(call_id=self._call_id).inc(len(chunk_to_send))

        except Exception as e:
            logger.error(
                "Error sending audio to Google Live",
                call_id=self._call_id,
                error=str(e),
                exc_info=True,
            )

    async def _receive_loop(self) -> None:
        """Continuously receive and process messages from Gemini Live API."""
        if not self.websocket:
            return

        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    await self._handle_server_message(data)
                except json.JSONDecodeError as e:
                    logger.error(
                        "Failed to decode Google Live message",
                        call_id=self._call_id,
                        error=str(e),
                    )
                except Exception as e:
                    logger.error(
                        "Error handling Google Live message",
                        call_id=self._call_id,
                        error=str(e),
                        exc_info=True,
                    )
        except (ConnectionClosedError, ConnectionClosedOK) as e:
            # Enhanced logging for WebSocket close
            close_reason = e.reason if hasattr(e, 'reason') else "No reason provided"
            close_code = e.code if hasattr(e, 'code') else None
            
            # Decode close code meaning
            close_code_meanings = {
                1000: "Normal closure",
                1001: "Going away",
                1002: "Protocol error",
                1003: "Unsupported data",
                1006: "Abnormal closure (no close frame)",
                1007: "Invalid frame payload data",
                1008: "Policy violation (likely auth/permission issue)",
                1009: "Message too big",
                1010: "Mandatory extension missing",
                1011: "Internal server error",
            }
            close_meaning = close_code_meanings.get(close_code, "Unknown")
            
            logger.warning(
                "Google Live WebSocket closed",
                call_id=self._call_id,
                code=close_code,
                meaning=close_meaning,
                reason=close_reason,
            )
            
            # Specific guidance for common errors
            if close_code == 1008:
                logger.error(
                    "Policy violation (1008) - Check API key permissions and Gemini Live API access",
                    call_id=self._call_id,
                    hint="Verify: 1) GOOGLE_API_KEY is correct 2) Gemini API is enabled 3) API key has generativelanguage.liveapi.user role",
                )
        except Exception as e:
            logger.error(
                "Google Live receive loop error",
                call_id=self._call_id,
                error=str(e),
                exc_info=True,
            )

    async def _handle_server_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming message from Gemini Live API."""
        message_type = None
        
        # Determine message type
        if "setupComplete" in data:
            message_type = "setupComplete"
        elif "serverContent" in data:
            message_type = "serverContent"
        elif "toolCall" in data:
            message_type = "toolCall"
        elif "toolCallCancellation" in data:
            message_type = "toolCallCancellation"
        elif "inputTranscription" in data:
            message_type = "inputTranscription"
        elif "outputTranscription" in data:
            message_type = "outputTranscription"
        elif "goAway" in data:
            message_type = "goAway"

        logger.debug(
            "Received Google Live message",
            call_id=self._call_id,
            message_type=message_type,
        )

        # Handle by type
        if message_type == "setupComplete":
            await self._handle_setup_complete(data)
        elif message_type == "serverContent":
            await self._handle_server_content(data)
        elif message_type == "toolCall":
            await self._handle_tool_call(data)
        elif message_type == "inputTranscription":
            await self._handle_input_transcription(data)
        elif message_type == "outputTranscription":
            await self._handle_output_transcription(data)
        elif message_type == "goAway":
            await self._handle_go_away(data)

    async def _handle_setup_complete(self, data: Dict[str, Any]) -> None:
        """Handle setupComplete message."""
        self._setup_complete = True
        logger.info(
            "Google Live setup complete",
            call_id=self._call_id,
        )

        # Play greeting if configured
        if self.config.greeting:
            await self._send_greeting()

    async def _send_greeting(self) -> None:
        """Send greeting message to start conversation."""
        greeting_msg = {
            "client_content": {
                "turns": [
                    {
                        "role": "user",
                        "parts": [{"text": "Start conversation"}]
                    }
                ],
                "turn_complete": True,
            }
        }
        await self._send_message(greeting_msg)
        
        logger.info(
            "Sent greeting prompt to Google Live",
            call_id=self._call_id,
        )

    async def _handle_server_content(self, data: Dict[str, Any]) -> None:
        """Handle serverContent message (audio, text, etc.)."""
        content = data.get("serverContent", {})
        
        # Check if model turn is complete
        turn_complete = content.get("turnComplete", False)
        
        # Extract parts
        for part in content.get("model_turn", {}).get("parts", []):
            # Handle audio output
            if "inline_data" in part:
                inline_data = part["inline_data"]
                if inline_data.get("mime_type") == "audio/pcm":
                    await self._handle_audio_output(inline_data["data"])
            
            # Handle text output (for debugging/logging)
            if "text" in part:
                text = part["text"]
                logger.debug(
                    "Google Live text response",
                    call_id=self._call_id,
                    text_preview=text[:100],
                )

        # Handle turn completion
        if turn_complete:
            await self._handle_turn_complete()

    async def _handle_audio_output(self, audio_b64: str) -> None:
        """
        Handle audio output from Gemini.

        Args:
            audio_b64: Base64-encoded PCM16 audio at 24kHz
        """
        try:
            # Decode base64
            pcm16_24k = base64.b64decode(audio_b64)
            
            _GOOGLE_LIVE_AUDIO_RECEIVED.labels(call_id=self._call_id).inc(len(pcm16_24k))

            # Resample from 24kHz to 8kHz for AudioSocket
            pcm16_8k = resample_audio(
                pcm16_24k,
                source_rate=_GEMINI_OUTPUT_RATE,
                target_rate=8000,
            )

            # Convert to target format (µ-law or PCM16)
            output_audio = convert_pcm16le_to_target_format(
                pcm16_8k,
                target_format="mulaw",  # Default to µ-law for telephony
                sample_rate=8000,
            )

            # Emit audio event
            if not self._in_audio_burst:
                self._in_audio_burst = True
                self._emit_event(
                    "agent_audio_start",
                    {
                        "call_id": self._call_id,
                        "format": "mulaw",
                        "sample_rate": 8000,
                    },
                )

            self._emit_event(
                "agent_audio",
                {
                    "call_id": self._call_id,
                    "audio": output_audio,
                    "format": "mulaw",
                    "sample_rate": 8000,
                },
            )

        except Exception as e:
            logger.error(
                "Error handling Google Live audio output",
                call_id=self._call_id,
                error=str(e),
                exc_info=True,
            )

    async def _handle_turn_complete(self) -> None:
        """Handle turn completion."""
        if self._in_audio_burst:
            self._in_audio_burst = False
            self._emit_event(
                "agent_audio_done",
                {
                    "call_id": self._call_id,
                },
            )

        # Mark greeting as complete after first turn
        if not self._greeting_completed:
            self._greeting_completed = True
            logger.info(
                "Google Live greeting completed",
                call_id=self._call_id,
            )

    async def _handle_tool_call(self, data: Dict[str, Any]) -> None:
        """Handle toolCall message."""
        tool_call = data.get("toolCall", {})
        
        if not self._tool_adapter:
            logger.warning(
                "Received tool call but no tool adapter configured",
                call_id=self._call_id,
            )
            return

        try:
            # Extract function call details
            function_calls = tool_call.get("function_calls", [])
            
            for func_call in function_calls:
                func_name = func_call.get("name")
                func_args = func_call.get("args", {})
                call_id = func_call.get("id")

                logger.info(
                    "Google Live tool call",
                    call_id=self._call_id,
                    function=func_name,
                    tool_call_id=call_id,
                )

                # Execute tool
                result = await self._tool_adapter.execute_tool(
                    func_name,
                    func_args,
                )

                # Send tool response
                tool_response = {
                    "tool_response": {
                        "function_responses": [
                            {
                                "id": call_id,
                                "name": func_name,
                                "response": result,
                            }
                        ]
                    }
                }
                await self._send_message(tool_response)

                logger.info(
                    "Sent Google Live tool response",
                    call_id=self._call_id,
                    function=func_name,
                )

        except Exception as e:
            logger.error(
                "Error handling Google Live tool call",
                call_id=self._call_id,
                error=str(e),
                exc_info=True,
            )

    async def _handle_input_transcription(self, data: Dict[str, Any]) -> None:
        """Handle input transcription (user speech recognized)."""
        transcription = data.get("inputTranscription", {}).get("text", "")
        
        if transcription:
            logger.info(
                "Google Live input transcription",
                call_id=self._call_id,
                transcription=transcription[:100],
            )
            
            # Emit transcript event for monitoring
            self._emit_event(
                "transcript",
                {
                    "call_id": self._call_id,
                    "text": transcription,
                    "is_final": True,
                    "source": "user",
                },
            )

    async def _handle_output_transcription(self, data: Dict[str, Any]) -> None:
        """Handle output transcription (agent speech transcribed)."""
        transcription = data.get("outputTranscription", {}).get("text", "")
        
        if transcription:
            logger.debug(
                "Google Live output transcription",
                call_id=self._call_id,
                transcription=transcription[:100],
            )

    async def _handle_go_away(self, data: Dict[str, Any]) -> None:
        """Handle goAway message (server disconnect warning)."""
        logger.warning(
            "Google Live server sending goAway",
            call_id=self._call_id,
        )
        # Prepare for reconnection if needed

    async def _keepalive_loop(self) -> None:
        """Send periodic keepalive messages."""
        while self.websocket and not self.websocket.closed:
            try:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SEC)
                # Send empty realtime input as keepalive
                if self._setup_complete:
                    await self._send_message({"realtime_input": {}})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Keepalive error",
                    call_id=self._call_id,
                    error=str(e),
                )

    async def cancel_response(self) -> None:
        """
        Cancel the current response (barge-in).
        
        Note: Gemini Live API supports interruption natively via VAD.
        When user starts speaking, the model automatically stops generating.
        """
        # Gemini handles this automatically, but we can log it
        logger.info(
            "Barge-in detected (handled by Gemini VAD)",
            call_id=self._call_id,
        )

    async def stop_session(self) -> None:
        """Stop the Google Live session and cleanup resources."""
        if not self._call_id:
            return

        logger.info(
            "Stopping Google Live session",
            call_id=self._call_id,
        )

        # Cancel background tasks
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive_task

        # Close WebSocket
        if self.websocket and not self.websocket.closed:
            await self.websocket.close()
            _GOOGLE_LIVE_SESSIONS.dec()

        # Clear state
        self._call_id = None
        self._session_id = None
        self._setup_complete = False
        self._input_buffer.clear()
        self._conversation_history.clear()

        if self._session_start_time:
            duration = time.time() - self._session_start_time
            logger.info(
                "Google Live session ended",
                duration_seconds=round(duration, 2),
            )

        logger.info("Google Live session stopped")

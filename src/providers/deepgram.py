import asyncio
import json
import audioop
import websockets
from typing import Callable, Optional, List, Dict, Any
import websockets.exceptions

from structlog import get_logger
from ..audio.resampler import (
    mulaw_to_pcm16le,
    resample_audio,
)
from ..config import LLMConfig
from .base import AIProviderInterface

logger = get_logger(__name__)

class DeepgramProvider(AIProviderInterface):
    def __init__(self, config: Dict[str, Any], llm_config: LLMConfig, on_event: Callable[[Dict[str, Any]], None]):
        super().__init__(on_event)
        self.config = config
        self.llm_config = llm_config
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        self._keep_alive_task: Optional[asyncio.Task] = None
        self._is_audio_flowing = False
        self.request_id: Optional[str] = None
        self.call_id: Optional[str] = None
        self._in_audio_burst: bool = False
        self._first_output_chunk_logged: bool = False
        self._closing: bool = False
        self._closed: bool = False
        # Maintain resample state for smoother conversion
        self._input_resample_state = None
        # Cache declared Deepgram input settings
        try:
            self._dg_input_rate = int(getattr(self.config, 'input_sample_rate_hz', 8000) or 8000)
        except Exception:
            self._dg_input_rate = 8000
        # Cache provider output settings for downstream conversion/metadata
        self._dg_output_encoding = (getattr(self.config, 'output_encoding', None) or 'mulaw').lower()
        try:
            self._dg_output_rate = int(getattr(self.config, 'output_sample_rate_hz', 8000) or 8000)
        except Exception:
            self._dg_output_rate = 8000

    @property
    def supported_codecs(self) -> List[str]:
        return ["ulaw"]

    async def start_session(self, call_id: str):
        ws_url = f"wss://agent.deepgram.com/v1/agent/converse"
        headers = {'Authorization': f'Token {self.config.api_key}'}

        try:
            logger.info("Connecting to Deepgram Voice Agent...", url=ws_url)
            self.websocket = await websockets.connect(ws_url, extra_headers=list(headers.items()))
            logger.info("✅ Successfully connected to Deepgram Voice Agent.")

            # Persist call context for downstream events
            self.call_id = call_id

            await self._configure_agent()

            asyncio.create_task(self._receive_loop())
            self._keep_alive_task = asyncio.create_task(self._keep_alive())

        except Exception as e:
            logger.error("Failed to connect to Deepgram Voice Agent", exc_info=True)
            if self._keep_alive_task:
                self._keep_alive_task.cancel()
            raise

    async def _configure_agent(self):
        """Builds and sends the V1 Settings message to the Deepgram Voice Agent."""
        # Derive codec settings from config with safe defaults
        input_encoding = getattr(self.config, 'input_encoding', None) or 'linear16'
        input_sample_rate = int(getattr(self.config, 'input_sample_rate_hz', 8000) or 8000)
        output_encoding = getattr(self.config, 'output_encoding', None) or 'mulaw'
        output_sample_rate = int(getattr(self.config, 'output_sample_rate_hz', 8000) or 8000)
        self._dg_output_encoding = output_encoding.lower()
        self._dg_output_rate = output_sample_rate

        # Determine greeting precedence: provider override > global LLM greeting > safe default
        try:
            greeting_val = (getattr(self.config, 'greeting', None) or "").strip()
        except Exception:
            greeting_val = ""
        if not greeting_val:
            try:
                greeting_val = (getattr(self.llm_config, 'initial_greeting', None) or "").strip()
            except Exception:
                greeting_val = ""
        if not greeting_val:
            greeting_val = "Hello, how can I help you today?"

        settings = {
            "type": "Settings",
            "audio": {
                "input": { "encoding": input_encoding, "sample_rate": input_sample_rate },
                "output": { "encoding": output_encoding, "sample_rate": output_sample_rate, "container": "none" }
            },
            "agent": {
                "greeting": greeting_val,
                "language": "en",
                "listen": { "provider": { "type": "deepgram", "model": self.config.model, "smart_format": True } },
                "think": { "provider": { "type": "open_ai", "model": self.llm_config.model }, "prompt": self.llm_config.prompt },
                "speak": { "provider": { "type": "deepgram", "model": self.config.tts_model } }
            }
        }
        await self.websocket.send(json.dumps(settings))
        logger.info(
            "Deepgram agent configured.",
            input_encoding=input_encoding,
            input_sample_rate=input_sample_rate,
            output_encoding=output_encoding,
            output_sample_rate=output_sample_rate,
        )

    async def send_audio(self, audio_chunk: bytes):
        """Send caller audio to Deepgram in the declared input format.

        Engine upstream uses AudioSocket with μ-law 8 kHz by default. Convert to
        linear16 at the configured Deepgram input sample rate before sending.
        """
        if self.websocket and audio_chunk:
            try:
                self._is_audio_flowing = True
                chunk_len = len(audio_chunk)
                if chunk_len not in (0, 160, 320):
                    logger.debug(
                        "Deepgram provider unexpected chunk size",
                        bytes=chunk_len,
                    )

                input_encoding = (getattr(self.config, "input_encoding", None) or "linear16").strip().lower()
                target_rate = int(getattr(self.config, "input_sample_rate_hz", 8000) or 8000)
                # Infer actual inbound format and source rate from canonical 20 ms frame sizes
                #  - 160 B ≈ μ-law @ 8 kHz (20 ms)
                #  - 320 B ≈ PCM16 @ 8 kHz (20 ms)
                #  - 640 B ≈ PCM16 @ 16 kHz (20 ms)
                if chunk_len == 160:
                    actual_format = "ulaw"
                    src_rate = 8000
                elif chunk_len == 320:
                    actual_format = "pcm16"
                    src_rate = 8000
                elif chunk_len == 640:
                    actual_format = "pcm16"
                    src_rate = 16000
                else:
                    actual_format = "pcm16" if input_encoding in ("slin16", "linear16", "pcm16") else "ulaw"
                    try:
                        src_rate = int(getattr(self.config, "input_sample_rate_hz", 0) or 0) or (16000 if actual_format == "pcm16" else 8000)
                    except Exception:
                        src_rate = 8000

                payload: bytes = audio_chunk
                pcm_for_rms: Optional[bytes] = None

                if input_encoding in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
                    if actual_format == "pcm16":
                        try:
                            payload = audioop.lin2ulaw(audio_chunk, 2)
                        except Exception:
                            logger.warning("Failed to convert PCM to μ-law for Deepgram", exc_info=True)
                            payload = audio_chunk
                    else:
                        payload = audio_chunk

                    pcm_for_rms = mulaw_to_pcm16le(payload)
                    if target_rate and target_rate != 8000:
                        pcm_resampled, self._input_resample_state = resample_audio(
                            pcm_for_rms,
                            8000,
                            target_rate,
                            state=self._input_resample_state,
                        )
                        try:
                            payload = audioop.lin2ulaw(pcm_resampled, 2)
                        except Exception:
                            logger.warning("Failed to convert resampled PCM back to μ-law", exc_info=True)
                            payload = audio_chunk
                        pcm_for_rms = pcm_resampled
                    else:
                        self._input_resample_state = None

                elif input_encoding in ("slin16", "linear16", "pcm16"):
                    # Normalize inbound to PCM16 and resample from detected source rate to target_rate
                    if actual_format == "ulaw":
                        pcm_src = mulaw_to_pcm16le(audio_chunk)
                        src_rate = 8000
                    else:
                        pcm_src = audio_chunk
                    pcm_for_rms = pcm_src
                    if target_rate and target_rate != src_rate:
                        pcm_dst, self._input_resample_state = resample_audio(
                            pcm_src,
                            src_rate,
                            target_rate,
                            state=self._input_resample_state,
                        )
                        payload = pcm_dst
                    else:
                        self._input_resample_state = None
                        payload = pcm_src
                else:
                    logger.warning(
                        "Unsupported Deepgram input_encoding",
                        input_encoding=input_encoding,
                    )
                    payload = audio_chunk
                    pcm_for_rms = None
                    self._input_resample_state = None

                if pcm_for_rms is not None:
                    try:
                        rms = audioop.rms(pcm_for_rms, 2)
                        if rms < 100:
                            logger.warning(
                                "Deepgram provider low RMS detected; possible codec mismatch",
                                rms=rms,
                                input_encoding=input_encoding,
                                bytes=chunk_len,
                            )
                    except Exception:
                        logger.debug("Deepgram RMS check failed", exc_info=True)

                await self.websocket.send(payload)
            except websockets.exceptions.ConnectionClosed as e:
                logger.debug("Could not send audio packet: Connection closed.", code=e.code, reason=e.reason)
            except Exception:
                logger.error("An unexpected error occurred while sending audio chunk", exc_info=True)

    async def stop_session(self):
        # Prevent duplicate disconnect logs/ops
        if self._closed or self._closing:
            return
        self._closing = True
        try:
            if self._keep_alive_task:
                self._keep_alive_task.cancel()
            if self.websocket and not self.websocket.closed:
                await self.websocket.close()
            if not self._closed:
                logger.info("Disconnected from Deepgram Voice Agent.")
            self._closed = True
        finally:
            self._closing = False

    async def _keep_alive(self):
        while True:
            try:
                await asyncio.sleep(10)
                if self.websocket and not self.websocket.closed:
                    if not self._is_audio_flowing:
                        await self.websocket.send(json.dumps({"type": "KeepAlive"}))
                    self._is_audio_flowing = False
                else:
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Error in keep-alive task", exc_info=True)
                break

    def describe_alignment(
        self,
        *,
        audiosocket_format: str,
        streaming_encoding: str,
        streaming_sample_rate: int,
    ) -> List[str]:
        issues: List[str] = []
        cfg_enc = (getattr(self.config, "input_encoding", None) or "").lower()
        try:
            cfg_rate = int(getattr(self.config, "input_sample_rate_hz", 0) or 0)
        except Exception:
            cfg_rate = 0

        if cfg_enc in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
            issues.append(
                "Deepgram send_audio converts μ-law frames to PCM16 before forwarding to the agent API. "
                "With input_encoding=ulaw this results in silence. Set input_encoding=linear16 or update "
                "the provider to pass μ-law through untouched."
            )
            if cfg_rate and cfg_rate != 8000:
                issues.append(
                    f"Deepgram configuration declares μ-law at {cfg_rate} Hz; μ-law transport must be 8000 Hz."
                )
        if cfg_enc in ("slin16", "linear16", "pcm16") and audiosocket_format != "slin16":
            issues.append(
                f"Deepgram expects PCM16 input but audiosocket.format is {audiosocket_format}. "
                "Set audiosocket.format=slin16 or change deepgram.input_encoding."
            )
        if streaming_encoding not in ("ulaw", "mulaw", "g711_ulaw", "mu-law"):
            issues.append(
                f"Streaming manager emits {streaming_encoding} frames but Deepgram output_encoding is μ-law. "
                "Ensure downstream playback converts the provider audio back to μ-law."
            )
        if streaming_sample_rate != 8000:
            issues.append(
                f"Streaming sample rate is {streaming_sample_rate} Hz but Deepgram output_sample_rate is 8000 Hz."
            )
        return issues

    async def _receive_loop(self):
        if not self.websocket:
            return
        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    try:
                        event_data = json.loads(message)
                        # If we were in an audio burst, a JSON control/event frame marks a boundary
                        if self._in_audio_burst and self.on_event:
                            await self.on_event({
                                'type': 'AgentAudioDone',
                                'streaming_done': True,
                                'call_id': self.call_id
                            })
                            self._in_audio_burst = False

                        if self.on_event:
                            await self.on_event(event_data)
                    except json.JSONDecodeError:
                        logger.error("Failed to parse JSON message from Deepgram", message=message)
                elif isinstance(message, bytes):
                    audio_event = {
                        'type': 'AgentAudio',
                        'data': message,
                        'streaming_chunk': True,
                        'call_id': self.call_id,
                        'encoding': self._dg_output_encoding,
                        'sample_rate': self._dg_output_rate,
                    }
                    if not self._first_output_chunk_logged:
                        logger.info(
                            "Deepgram AgentAudio first chunk",
                            bytes=len(message)
                        )
                        self._first_output_chunk_logged = True
                    self._in_audio_burst = True
                    if self.on_event:
                        await self.on_event(audio_event)
        except websockets.exceptions.ConnectionClosed as e:
            # Only warn once; avoid info duplicate from stop_session
            if not self._closed:
                logger.warning("Deepgram Voice Agent connection closed", reason=str(e))
        except Exception:
            logger.error("Error receiving events from Deepgram Voice Agent", exc_info=True)
        finally:
            # If socket ends mid-burst, close the burst cleanly
            if self._in_audio_burst and self.on_event:
                try:
                    await self.on_event({
                        'type': 'AgentAudioDone',
                        'streaming_done': True,
                        'call_id': self.call_id
                    })
                except Exception:
                    pass
            self._in_audio_burst = False

    async def speak(self, text: str):
        if not text or not self.websocket:
            return
        inject_message = {"type": "InjectAgentMessage", "message": text}
        try:
            await self.websocket.send(json.dumps(inject_message))
        except websockets.exceptions.ConnectionClosed as e:
            logger.error("Failed to send inject agent message: Connection is closed.", exc_info=True, code=e.code, reason=e.reason)
    
    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about the provider and its capabilities."""
        return {
            "name": "DeepgramProvider",
            "type": "cloud",
            "supported_codecs": self.supported_codecs,
            "model": self.config.model,
            "tts_model": self.config.tts_model
        }
    
    def is_ready(self) -> bool:
        """Check if the provider is ready to process audio."""
        # Configuration readiness: we consider the provider ready when it's properly
        # configured and wired to emit events. A live websocket is only established
        # after start_session(call_id) during an actual call.
        try:
            api_key_ok = bool(getattr(self.config, 'api_key', None))
        except Exception:
            api_key_ok = False
        return api_key_ok and (self.on_event is not None)

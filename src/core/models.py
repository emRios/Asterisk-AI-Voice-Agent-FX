"""
Core data models for the Asterisk AI Voice Agent.

This module defines the typed data structures that replace the dict soup
in the original engine.py implementation.
"""

from dataclasses import dataclass, field
from typing import Optional, Set, Dict, Any
import time


@dataclass
class PlaybackRef:
    """Reference to an active audio playback."""
    playback_id: str
    call_id: str
    channel_id: str
    bridge_id: Optional[str]
    media_uri: str
    audio_file: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class CallSession:
    """Complete session state for a call."""
    # Core identifiers
    call_id: str              # canonical (caller_channel_id)
    caller_channel_id: str
    local_channel_id: Optional[str] = None
    external_media_id: Optional[str] = None
    external_media_call_id: Optional[str] = None
    audiosocket_channel_id: Optional[str] = None
    audiosocket_conn_id: Optional[str] = None
    audiosocket_uuid: Optional[str] = None
    provider_session_active: bool = False
    bridge_id: Optional[str] = None
    
    # Provider and conversation state
    provider_name: str = "local"
    pipeline_name: Optional[str] = None
    pipeline_components: Dict[str, str] = field(default_factory=dict)
    conversation_state: str = "greeting"  # greeting | listening | processing
    status: str = "initializing"
    last_transcript: Optional[str] = None
    last_agent_response: Optional[str] = None
    
    # Audio capture and TTS gating
    audio_capture_enabled: bool = False
    tts_playing: bool = False
    tts_tokens: Set[str] = field(default_factory=set)
    tts_active_count: int = 0
    # TTS timing for barge-in/protection windows
    tts_started_ts: float = 0.0
    tts_ended_ts: float = 0.0
    # Barge-in detection accumulators
    barge_in_candidate_ms: int = 0
    last_barge_in_ts: float = 0.0
    barge_start_ts: float = 0.0
    
    # VAD and audio processing state
    vad_state: Dict[str, Any] = field(default_factory=dict)
    fallback_state: Dict[str, Any] = field(default_factory=dict)
    enhanced_vad_enabled: bool = False
    enhanced_vad_frames: int = 0
    enhanced_vad_speech_frames: int = 0
    last_provider_audio_ts: float = 0.0
    
    # Cleanup and lifecycle
    cleanup_after_tts: bool = False
    cleanup_in_progress: bool = False
    cleanup_completed: bool = False
    pending_local_channel_id: Optional[str] = None
    pending_external_media_id: Optional[str] = None
    ssrc: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    agent_audio_buffer: bytearray = field(default_factory=bytearray)
    last_agent_audio_ts: float = 0.0
    # Latency instrumentation (all values in absolute unix seconds)
    last_user_speech_end_ts: float = 0.0
    last_transcription_ts: float = 0.0
    last_agent_response_ts: float = 0.0
    last_response_start_ts: float = 0.0
    # Cached latency readings for observability (seconds)
    last_turn_latency_s: float = 0.0
    last_transcription_latency_s: float = 0.0
    
    # Streaming state and observability
    streaming_ready: bool = False
    streaming_response: bool = False
    streaming_started: bool = False
    current_stream_id: Optional[str] = None
    streaming_bytes_sent: int = 0
    streaming_fallback_count: int = 0
    streaming_jitter_buffer_depth: int = 0
    streaming_keepalive_sent: int = 0
    streaming_keepalive_timeouts: int = 0
    last_streaming_error: Optional[str] = None
    
    def __post_init__(self):
        """Initialize default VAD and fallback state."""
        if not self.vad_state:
            self.vad_state = {
                "state": "listening",
                "speaking": False,
                "speech_real_start_fired": False,
                "pre_roll_buffer": b"",
                "utterance_buffer": b"",
                "utterance_id": 0,
                "last_utterance_end_ms": 0,
                "webrtc_speech_frames": 0,
                "webrtc_silence_frames": 0,
                "webrtc_last_decision": False,
                "audio_buffer": b"",
                "frame_buffer": b"",  # ARCHITECT FIX: Add frame_buffer for 20ms frame buffering
                "frame_count": 0,     # ARCHITECT FIX: Add frame_count for VAD processing
                "last_voice_ms": 0,
                "tts_playing": False,
                "consecutive_silence_frames": 0,
            }
        
        if not self.fallback_state:
            self.fallback_state = {
                "audio_buffer": b"",
                "last_vad_speech_time": time.time(),
                "buffer_start_time": None,
                "frame_count": 0
            }


@dataclass
class ProviderSession:
    """Session state for a provider connection."""
    call_id: str
    provider_name: str
    websocket_connected: bool = False
    input_mode: str = "pcm16_16k"  # pcm16_16k | pcm16_8k
    created_at: float = field(default_factory=time.time)


@dataclass
class TransportConfig:
    """Configuration for ExternalMedia transport settings."""
    transport_type: str = "externalmedia"  # Only ExternalMedia supported
    rtp_host: str = "0.0.0.0"
    rtp_port: int = 18080
    codec: str = "ulaw"
    direction: str = "both"
    jitter_buffer_ms: int = 20

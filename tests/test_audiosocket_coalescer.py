import asyncio
import json
import types

import pytest
from pydantic import ValidationError

import src.engine as engine_module
import src.config as config_package
from src.config import AudioSocketConfig
from src.core.models import CallSession
from src.engine import Engine


class RecordingProvider:
    def __init__(self, *, delay: float = 0.0, behavior: str = "ok") -> None:
        self.delay = delay
        self.behavior = behavior
        self.sent: list[bytes] = []
        self.events: list[str] = []
        self.active_sends = 0
        self.max_active_sends = 0

    async def send_audio(self, payload: bytes) -> None:
        self.active_sends += 1
        self.max_active_sends = max(self.max_active_sends, self.active_sends)
        try:
            if self.behavior == "timeout":
                await asyncio.Event().wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.behavior == "error":
                raise RuntimeError("simulated send failure")
            self.sent.append(bytes(payload))
            self.events.append("send")
        finally:
            self.active_sends -= 1

    async def stop_session(self) -> None:
        self.events.append("stop")


class SessionStoreStub:
    def __init__(self, session) -> None:
        self.session = session
        self.saved = []
        self.unindexed = []

    async def get_by_call_id(self, _call_id: str):
        return self.session

    async def upsert_call(self, session) -> None:
        self.saved.append(session)

    async def unindex_channel(self, channel_id: str) -> None:
        self.unindexed.append(channel_id)


class AriClientStub:
    async def add_channel_to_bridge(self, _bridge_id: str, _channel_id: str) -> bool:
        return True

    async def remove_channel_from_bridge(self, _bridge_id: str, _channel_id: str) -> bool:
        return True

    async def hangup_channel(self, _channel_id: str) -> bool:
        return True

    async def send_command(self, **_kwargs):
        return {}


class StreamingPlaybackStub:
    async def stop_streaming_playback(self, _call_id: str) -> None:
        return None


def make_engine(frame_ms: int) -> Engine:
    engine = Engine.__new__(Engine)
    engine._audiosocket_frame_ms = frame_ms
    engine._audiosocket_coalesce_states = {}
    engine._call_providers = {}
    return engine


def make_terminal_engine(session: CallSession, provider: RecordingProvider) -> Engine:
    engine = make_engine(60)
    engine._call_providers[session.call_id] = provider
    engine.session_store = SessionStoreStub(session)
    engine.ari_client = AriClientStub()
    engine.streaming_playback_manager = StreamingPlaybackStub()
    engine.conversation_coordinator = None
    engine.pipeline_orchestrator = types.SimpleNamespace(enabled=False)
    engine._provider_start_tasks = {}
    engine._pipeline_tasks = {}
    engine._pipeline_queues = {}
    engine._pipeline_forced = {}
    engine.bridges = {}
    engine.pending_local_channels = {}
    engine.pending_audiosocket_channels = {}
    engine.local_channels = {}
    engine.audiosocket_channels = {}
    engine.uuidext_to_channel = {}

    async def disable_pipeline_talk_detect(_session) -> None:
        return None

    engine._disable_pipeline_talk_detect = disable_pipeline_talk_detect
    return engine


async def send_frame(engine: Engine, call_id: str, provider: RecordingProvider, frame: bytes) -> None:
    await engine._send_or_coalesce_provider_audio(
        call_id,
        provider,
        frame,
        "ulaw",
        "deepgram",
    )


@pytest.mark.asyncio
async def test_paridad_bytes_20_vs_60():
    frames = [bytes([index % 256]) * 160 for index in range(300)]
    engine_20 = make_engine(20)
    engine_60 = make_engine(60)
    provider_20 = RecordingProvider()
    provider_60 = RecordingProvider()

    for frame in frames:
        await send_frame(engine_20, "call-20", provider_20, frame)
        await send_frame(engine_60, "call-60", provider_60, frame)

    assert b"".join(provider_60.sent) == b"".join(provider_20.sent)
    assert len(provider_20.sent) == 300
    assert len(provider_60.sent) == 100
    assert engine_20._audiosocket_coalesce_states == {}


@pytest.mark.asyncio
async def test_orden_preservado():
    engine = make_engine(60)
    provider = RecordingProvider()
    frames = [bytes([index]) * 160 for index in range(9)]

    for frame in frames:
        await send_frame(engine, "ordered", provider, frame)

    assert provider.sent == [b"".join(frames[index:index + 3]) for index in range(0, 9, 3)]


@pytest.mark.asyncio
async def test_aislamiento_entre_llamadas():
    engine = make_engine(60)
    provider_a = RecordingProvider()
    provider_b = RecordingProvider()
    frames_a = [bytes([0xA0 + index]) * 160 for index in range(3)]
    frames_b = [bytes([0xB0 + index]) * 160 for index in range(3)]

    for frame_a, frame_b in zip(frames_a, frames_b):
        await send_frame(engine, "call-a", provider_a, frame_a)
        await send_frame(engine, "call-b", provider_b, frame_b)

    assert provider_a.sent == [b"".join(frames_a)]
    assert provider_b.sent == [b"".join(frames_b)]


@pytest.mark.asyncio
async def test_no_adelanta_audio():
    engine = make_engine(60)
    provider = RecordingProvider()

    await send_frame(engine, "call", provider, b"a" * 160)
    await send_frame(engine, "call", provider, b"b" * 160)
    assert provider.sent == []

    await send_frame(engine, "call", provider, b"c" * 160)
    assert provider.sent == [b"a" * 160 + b"b" * 160 + b"c" * 160]


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_count", [1, 2])
async def test_flush_en_cleanup_terminal_antes_de_stop(frame_count):
    engine = make_engine(60)
    provider = RecordingProvider()
    engine._call_providers["call"] = provider
    session = types.SimpleNamespace(provider_session_active=True)

    for index in range(frame_count):
        await send_frame(engine, "call", provider, bytes([index]) * 160)
    await engine._stop_call_provider("call", session)

    assert provider.events == ["send", "stop"]
    assert provider.sent == [b"".join(bytes([index]) * 160 for index in range(frame_count))]
    assert "call" not in engine._audiosocket_coalesce_states
    assert "call" not in engine._call_providers


@pytest.mark.asyncio
async def test_transfer_answered_legacy_flushea_parcial_antes_de_stop_y_elimina_estado():
    session = CallSession(
        call_id="call",
        caller_channel_id="call",
        bridge_id="bridge",
        provider_name="deepgram",
        provider_session_active=True,
        current_action={"type": "warm-transfer"},
    )
    provider = RecordingProvider()
    engine = make_terminal_engine(session, provider)
    await send_frame(engine, "call", provider, b"h" * 160)

    await engine._handle_transfer_answered("human-channel", ["warm-transfer", "call", "100"])

    assert provider.events == ["send", "stop"]
    assert provider.sent == [b"h" * 160]
    assert "call" not in engine._audiosocket_coalesce_states
    assert "call" not in engine._call_providers


@pytest.mark.asyncio
async def test_handoff_humano_flushea_parcial_antes_de_stop_y_elimina_estado():
    session = CallSession(
        call_id="call",
        caller_channel_id="call",
        bridge_id="bridge",
        audiosocket_channel_id="audio-channel",
        human_channel_id="human-channel",
        human_audio_mode="talk",
        provider_name="deepgram",
        provider_session_active=True,
        audio_capture_enabled=True,
        current_action={"type": "conference-participant", "answered": True},
    )
    provider = RecordingProvider()
    engine = make_terminal_engine(session, provider)
    await send_frame(engine, "call", provider, b"h" * 160)

    result = await engine.handoff_to_human("call")

    assert result["status"] == "success"
    assert result["provider_stopped"] is True
    assert provider.events == ["send", "stop"]
    assert provider.sent == [b"h" * 160]
    assert "call" not in engine._audiosocket_coalesce_states
    assert "call" not in engine._call_providers


@pytest.mark.asyncio
async def test_desconexion_ai_flushea_parcial_antes_de_stop_y_elimina_estado():
    session = CallSession(
        call_id="call",
        caller_channel_id="call",
        bridge_id="bridge",
        audiosocket_channel_id="audio-channel",
        audiosocket_uuid="audio-uuid",
        provider_name="deepgram",
        provider_session_active=True,
        audio_capture_enabled=True,
    )
    provider = RecordingProvider()
    engine = make_terminal_engine(session, provider)
    await send_frame(engine, "call", provider, b"d" * 160)

    result = await engine.disconnect_ai_from_call("call")

    assert result["status"] == "success"
    assert result["provider_stopped"] is True
    assert provider.events == ["send", "stop"]
    assert provider.sent == [b"d" * 160]
    assert "call" not in engine._audiosocket_coalesce_states
    assert "call" not in engine._call_providers


@pytest.mark.asyncio
async def test_transferencia_atendida_flushea_parcial_antes_de_stop_y_elimina_estado():
    session = CallSession(
        call_id="call",
        caller_channel_id="call",
        bridge_id="bridge",
        provider_name="deepgram",
        provider_session_active=True,
        current_action={"type": "attended_transfer"},
    )
    provider = RecordingProvider()
    engine = make_terminal_engine(session, provider)
    await send_frame(engine, "call", provider, b"t" * 160)

    await engine._attended_transfer_finalize_bridge(
        session,
        agent_channel_id="agent-channel",
        destination_description="100",
        caller_connected_prompt="",
        tts_timeout=1.0,
    )

    assert provider.events == ["send", "stop"]
    assert provider.sent == [b"t" * 160]
    assert "call" not in engine._audiosocket_coalesce_states
    assert "call" not in engine._call_providers


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["error", "timeout"])
async def test_cleanup_terminal_limpia_tras_error_o_timeout(monkeypatch, behavior):
    monkeypatch.setattr(engine_module, "_COALESCE_FLUSH_TIMEOUT_SECONDS", 0.01)
    engine = make_engine(60)
    provider = RecordingProvider(behavior=behavior)
    engine._call_providers["call"] = provider
    session = types.SimpleNamespace(provider_session_active=True)
    await send_frame(engine, "call", provider, b"x" * 160)

    await engine._stop_call_provider("call", session)

    assert provider.events == ["stop"]
    assert "call" not in engine._audiosocket_coalesce_states
    assert "call" not in engine._call_providers


@pytest.mark.asyncio
async def test_disconnect_no_terminal_hasta_ultima_conexion():
    engine = make_engine(60)
    provider = RecordingProvider()
    session = types.SimpleNamespace(provider_session_active=True, audiosocket_conn_id=None)
    engine.session_store = SessionStoreStub(session)
    engine.conn_to_channel = {"conn-1": "call", "conn-2": "call"}
    engine.channel_to_conns = {"call": {"conn-1", "conn-2"}}
    engine.audiosocket_primary_conn = {"call": "conn-1"}
    engine._call_providers["call"] = provider
    await send_frame(engine, "call", provider, b"a" * 160)
    await send_frame(engine, "call", provider, b"b" * 160)

    await engine._audiosocket_handle_disconnect("conn-1")
    assert provider.sent == []
    assert "call" in engine._audiosocket_coalesce_states

    await engine._audiosocket_handle_disconnect("conn-2")
    assert provider.sent == [b"a" * 160 + b"b" * 160]
    assert "call" not in engine._audiosocket_coalesce_states


@pytest.mark.asyncio
async def test_dos_conexiones_misma_llamada_serializan_envios():
    engine = make_engine(60)
    provider = RecordingProvider(delay=0.005)
    frames = [bytes([index]) * 160 for index in range(6)]

    await asyncio.gather(*(send_frame(engine, "call", provider, frame) for frame in frames))

    assert provider.sent == [b"".join(frames[:3]), b"".join(frames[3:])]
    assert provider.max_active_sends == 1


@pytest.mark.asyncio
async def test_stall_flush():
    engine = make_engine(60)
    provider = RecordingProvider()
    await send_frame(engine, "call", provider, b"a" * 160)
    engine._audiosocket_coalesce_states["call"].first_frame_ts -= 0.200

    await send_frame(engine, "call", provider, b"b" * 160)

    assert provider.sent == [b"a" * 160]
    state = engine._audiosocket_coalesce_states["call"]
    assert bytes(state.buffer) == b"b" * 160
    assert state.frame_count == 1


@pytest.mark.asyncio
async def test_bypass_20ms_no_crea_estado():
    engine = make_engine(20)
    provider = RecordingProvider()

    await send_frame(engine, "call", provider, b"x" * 160)

    assert provider.sent == [b"x" * 160]
    assert engine._audiosocket_coalesce_states == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding,provider_name",
    [("slin16", "deepgram"), ("ulaw", "openai_realtime")],
)
async def test_rama_no_aplicable_intacta(encoding, provider_name):
    engine = make_engine(60)
    provider = RecordingProvider()

    await engine._send_or_coalesce_provider_audio(
        "call", provider, b"x" * 160, encoding, provider_name
    )

    assert provider.sent == [b"x" * 160]
    assert engine._audiosocket_coalesce_states == {}


@pytest.mark.asyncio
async def test_provider_inactivo_no_acumula_y_cleanup_descarta():
    engine = make_engine(60)
    provider = RecordingProvider()

    await engine._send_or_coalesce_provider_audio(
        "call", provider, b"x" * 160, "ulaw", "deepgram", provider_active=False
    )
    assert provider.sent == []
    assert engine._audiosocket_coalesce_states == {}

    await send_frame(engine, "call", provider, b"x" * 160)
    await engine._send_or_coalesce_provider_audio(
        "call", provider, provider_active=False, flush_reason="terminal_cleanup"
    )
    assert provider.sent == []
    assert engine._audiosocket_coalesce_states == {}


def test_validador_frame_ms():
    assert AudioSocketConfig(frame_ms=20).frame_ms == 20
    assert AudioSocketConfig(frame_ms=60).frame_ms == 60
    for invalid in (30, 40, 70, 80, 100, 0, -20):
        with pytest.raises(ValidationError):
            AudioSocketConfig(frame_ms=invalid)


@pytest.mark.asyncio
async def test_reload_difiere_frame_ms(monkeypatch):
    engine = make_engine(60)
    old_config = types.SimpleNamespace(
        audiosocket=types.SimpleNamespace(frame_ms=60),
        providers={},
        contexts={},
        mcp=None,
    )
    new_config = types.SimpleNamespace(
        audiosocket=types.SimpleNamespace(frame_ms=20),
        providers={},
        contexts={},
        mcp=None,
        dict=lambda: {},
    )
    engine.config = old_config
    engine.providers = {}
    engine._is_request_authorized = lambda _request: True
    engine._compute_config_hash = lambda: "hash"
    monkeypatch.setattr(config_package, "load_config", lambda: new_config)
    monkeypatch.setattr(engine_module, "TransportOrchestrator", lambda _config: object())

    response = await engine._reload_handler(object())
    payload = json.loads(response.body)

    assert engine.config.audiosocket.frame_ms == 60
    assert (
        "AudioSocket frame_ms=20 deferred until restart; effective value remains 60"
        in payload["changes"]
    )


@pytest.mark.asyncio
async def test_metricas_de_mensaje_y_flush_parcial():
    engine = make_engine(60)
    provider = RecordingProvider()
    msg_before = engine_module._COALESCE_MSGS_OUT_TOTAL._value.get()
    partial = engine_module._COALESCE_PARTIAL_FLUSH_TOTAL.labels(reason="terminal_cleanup")
    partial_before = partial._value.get()

    await send_frame(engine, "call", provider, b"x" * 160)
    await engine._send_or_coalesce_provider_audio(
        "call", provider, provider_active=True, flush_reason="terminal_cleanup"
    )

    assert engine_module._COALESCE_MSGS_OUT_TOTAL._value.get() == msg_before + 1
    assert partial._value.get() == partial_before + 1

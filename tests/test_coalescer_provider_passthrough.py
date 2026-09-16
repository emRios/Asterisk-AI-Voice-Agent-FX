import asyncio
import time
import types

import pytest

import src.core.streaming_playback_manager as playback_module
from src.config import AudioSocketConfig
from src.core.streaming_playback_manager import StreamingPlaybackManager
from src.engine import Engine
from src.providers.deepgram import DeepgramProvider


class WebSocketStub:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(bytes(payload))


def make_deepgram_provider(*, settings_acked: bool) -> tuple[DeepgramProvider, WebSocketStub]:
    provider = DeepgramProvider(
        {
            "input_encoding": "mulaw",
            "input_sample_rate_hz": 8000,
            "output_encoding": "mulaw",
            "output_sample_rate_hz": 8000,
        },
        types.SimpleNamespace(),
        lambda _event: None,
    )
    websocket = WebSocketStub()
    provider.websocket = websocket
    provider._settings_acked = settings_acked
    provider._ready_to_stream = settings_acked
    return provider, websocket


def make_engine() -> Engine:
    engine = Engine.__new__(Engine)
    engine._audiosocket_frame_ms = 60
    engine._audiosocket_coalesce_states = {}
    return engine


@pytest.mark.asyncio
async def test_provider_no_retrocea_mulaw_480_con_firma_productiva():
    engine = make_engine()
    provider, websocket = make_deepgram_provider(settings_acked=True)
    frames = [bytes([index + 1]) * 160 for index in range(3)]

    for frame in frames:
        await engine._send_or_coalesce_provider_audio(
            "call", provider, frame, "ulaw", "deepgram"
        )

    assert websocket.sent == [b"".join(frames)]
    assert len(websocket.sent[0]) == 480


@pytest.mark.asyncio
async def test_ruta_continua_deepgram_llega_al_coalescer_productivo():
    engine = make_engine()
    provider, websocket = make_deepgram_provider(settings_acked=True)
    frames = [bytes([index + 1]) * 160 for index in range(3)]

    for frame in frames:
        await engine._send_continuous_provider_audio(
            "call", provider, frame, "ulaw", "deepgram", 8000
        )

    assert websocket.sent == [b"".join(frames)]
    assert len(websocket.sent[0]) == 480


@pytest.mark.asyncio
async def test_prestream_queue_con_480_se_vacia_en_siguiente_send():
    provider, websocket = make_deepgram_provider(settings_acked=False)
    payloads = [bytes([index]) * 480 for index in range(12)]

    for payload in payloads:
        await provider.send_audio(payload)

    assert provider._prestream_queue == payloads[-10:]
    assert websocket.sent == []

    provider._settings_acked = True
    provider._ready_to_stream = True
    current = b"z" * 480
    await provider.send_audio(current)

    assert websocket.sent == payloads[-10:] + [current]
    assert provider._prestream_queue == []


@pytest.mark.asyncio
@pytest.mark.parametrize("input_frame_ms", [20, 60])
async def test_pacer_tts_independiente_de_frame_ms_entrada(monkeypatch, input_frame_ms):
    clock = [0.0]

    def perf_counter() -> float:
        return clock[0]

    async def advance_clock(delay: float) -> None:
        clock[0] += delay

    monkeypatch.setattr(playback_module.time, "perf_counter", perf_counter)
    monkeypatch.setattr(playback_module.asyncio, "sleep", advance_clock)
    audiosocket = AudioSocketConfig(frame_ms=input_frame_ms)
    manager = StreamingPlaybackManager(
        session_store=types.SimpleNamespace(),
        ari_client=types.SimpleNamespace(),
        conversation_coordinator=None,
        fallback_playback_manager=None,
        streaming_config={
            "continuous_stream": True,
            "chunk_size_ms": 20,
            "sample_rate": 8000,
            "min_start_ms": 0,
            "low_watermark_ms": 0,
        },
        audio_transport="audiosocket",
    )
    call_id = f"call-{audiosocket.frame_ms}"
    stream_id = f"stream-{audiosocket.frame_ms}"
    manager.active_streams[call_id] = {"stream_id": stream_id}
    observed: list[float] = []

    async def drain_next_frame(*_args):
        observed.append(time.perf_counter())
        return "finished" if len(observed) == 4 else "real"

    manager._drain_next_frame = drain_next_frame
    await manager._pacer_loop(call_id, stream_id, asyncio.Queue())

    assert manager.chunk_size_ms == 20
    assert len(observed) == 4
    intervals = [later - earlier for earlier, later in zip(observed, observed[1:])]
    assert intervals == pytest.approx([0.020, 0.020, 0.020])

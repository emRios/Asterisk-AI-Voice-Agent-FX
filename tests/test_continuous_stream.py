import asyncio
import math
import time
import pytest

from src.core.streaming_playback_manager import StreamingPlaybackManager


class Dummy:
    pass


def make_manager(**overrides):
    cfg = {
        'continuous_stream': True,
        'min_start_ms': 120,
        'low_watermark_ms': 80,
        'chunk_size_ms': 20,
        'sample_rate': 8000,
        'normalizer': {'enabled': True, 'target_rms': 1400, 'max_gain_db': 9.0},
    }
    cfg.update(overrides)
    return StreamingPlaybackManager(
        session_store=Dummy(),
        ari_client=Dummy(),
        conversation_coordinator=None,
        fallback_playback_manager=None,
        streaming_config=cfg,
        audio_transport="audiosocket",
    )


def test_continuous_stream_skips_warmup_for_non_first_segment():
    mgr = make_manager()
    call_id = "test-call-1"
    stream_id = "stream:resp:test-call-1:1"
    # Simulate active stream entry minimal fields
    mgr.active_streams[call_id] = {
        'stream_id': stream_id,
        'min_start_chunks': mgr.min_start_chunks,
    }
    mgr._startup_ready[call_id] = False

    # Non-first segment
    stream_info = {
        'segments_played': 1,
        'min_start_chunks': mgr.min_start_chunks,
    }
    jitter = asyncio.Queue()

    ready = mgr._ensure_startup_ready(call_id, stream_id, jitter, stream_info)
    assert ready is True
    assert mgr._startup_ready.get(call_id) is True
    assert stream_info.get('startup_ready') is True


def test_first_segment_requires_min_start_when_empty():
    mgr = make_manager()
    call_id = "test-call-2"
    stream_id = "stream:resp:test-call-2:1"
    mgr._startup_ready[call_id] = False
    stream_info = {
        'segments_played': 0,
        'min_start_chunks': 4,
    }
    jitter = asyncio.Queue()
    # empty jitter buffer -> available_frames = 0 < 4
    ready = mgr._ensure_startup_ready(call_id, stream_id, jitter, stream_info)
    assert ready is False
    assert mgr._startup_ready.get(call_id) is False


@pytest.mark.asyncio
async def test_mark_segment_boundary_increments_and_resets_attack():
    mgr = make_manager()
    call_id = "test-call-3"
    # Prepare active stream with sample rate and existing fields
    mgr.active_streams[call_id] = {
        'stream_id': "stream:resp:test-call-3:1",
        'target_sample_rate': 8000,
        'segments_played': 0,
    }
    # attack bytes expected: sr * (attack_ms/1000) * 2
    expected_attack = int(max(0, int(8000 * (mgr.attack_ms / 1000.0)) * 2))

    await mgr.mark_segment_boundary(call_id)

    info = mgr.active_streams[call_id]
    assert info['segments_played'] == 1
    assert info.get('attack_bytes_remaining') == expected_attack


def test_monitoring_snapshot_serializes_active_stream():
    mgr = make_manager()
    call_id = "test-call-4"
    now = time.time()
    mgr.active_streams[call_id] = {
        'stream_id': "stream:resp:test-call-4:1",
        'playback_type': "streaming-response",
        'start_time': now - 5.0,
        'source_encoding': "slin16",
        'source_sample_rate': 16000,
        'target_format': "ulaw",
        'target_sample_rate': 8000,
        'queued_bytes': 320,
        'queued_total_bytes': 1280,
        'tx_bytes': 640,
        'tx_total_bytes': 2560,
        'provider_bytes': 960,
        'provider_total_bytes': 3840,
        'frames_sent': 12,
        'underflow_events': 1,
        'filler_frames': 2,
        'jitter_depth': 3,
        'last_chunk_age_s': 0.12,
        'buffer_depth_max_frames': 4,
        'buffer_depth_min_frames': 1,
        'segments_played': 2,
        'min_start_chunks': 3,
        'idle_ticks': 1,
        'idle_cutoff_ticks': 6,
        'last_real_emit_ts': now - 0.05,
        'last_emit_was_filler': False,
        'startup_ready': True,
        'first_frame_observed': True,
    }

    snapshot = mgr.get_monitoring_snapshot()

    assert snapshot["streaming"]["active_streams"] == 1
    assert snapshot["streaming"]["total_tx_bytes"] == 640
    assert snapshot["streaming"]["total_provider_bytes"] == 960
    detail = snapshot["streaming_details"][0]
    assert detail["call_id"] == call_id
    assert detail["playback_type"] == "streaming-response"
    assert detail["target_format"] == "ulaw"
    assert detail["target_sample_rate"] == 8000
    assert detail["tx_total_bytes"] == 2560
    assert detail["provider_total_bytes"] == 3840
    assert detail["underflow_events"] == 1
    assert detail["stream_active"] is False

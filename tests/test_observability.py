"""Tests for src/observability.py facade.

Verifies:
1. Import and use without Logfire installed / token / enabled -> no exception.
2. ARI resource normalization.
3. ari_command records status class, duration, and uses normalized labels.
4. Media connect / disconnect / first_audio.
5. Double disconnect does not leave counter negative.
6. First audio normalizes negative latency to 0.
7. Media sampler with fake RTP server (success, failure, stalled, CancelledError).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict
from unittest.mock import patch

import pytest

# Reset singleton before each test
import src.observability as obs_mod


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Ensure each test starts with a fresh Observability instance."""
    obs_mod._instance = None
    monkeypatch.setenv("LOGFIRE_ENABLED", "0")
    yield
    obs_mod._instance = None


# ──────────────────────────────────────────────────────────────────────
# 1. No-op without Logfire
# ──────────────────────────────────────────────────────────────────────
class TestNoOpWithoutLogfire:
    def test_configure_returns_instance(self):
        obs = obs_mod.configure_observability(ari_base_url="http://127.0.0.1:8088/ari")
        assert obs is not None
        assert obs._logfire_ok is False

    def test_get_observability_lazy(self):
        obs = obs_mod.get_observability()
        assert obs is not None

    def test_all_methods_callable_without_exception(self):
        obs = obs_mod.configure_observability()
        obs.ari_connect(success=True)
        obs.ari_connect(success=False, error="timeout")
        obs.ari_command(method="POST", resource="channels/abc/answer", status=200, duration=0.1)
        obs.ari_error(method="GET", resource="channels/xyz", error_type="client_error")
        obs.ari_ws_connected()
        obs.ari_ws_disconnected()
        obs.ari_ws_reconnect(attempt=3)
        obs.ari_ws_event_received()
        obs.media_connected(transport="rtp", call_id="c1")
        obs.media_rejected(transport="rtp", reason="test")
        obs.media_first_audio(transport="rtp", call_id="c1")
        obs.media_disconnected(transport="rtp", call_id="c1")
        obs.sample_media(transport="rtp", packet_loss=5, stalled=1)


# ──────────────────────────────────────────────────────────────────────
# 2. ARI resource normalization
# ──────────────────────────────────────────────────────────────────────
class TestNormalizeAriResource:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("channels/1234-5678/answer", "channels/{id}/answer"),
            ("channels/abcdef", "channels/{id}"),
            ("bridges/br-99/addChannel", "bridges/{id}/addChannel"),
            ("playbacks/pb-42", "playbacks/{id}"),
            ("channels/externalMedia", "channels/externalMedia"),
            ("channels/c1/play", "channels/{id}/play"),
            ("channels/c1/record", "channels/{id}/record"),
            ("channels/c1/continue", "channels/{id}/continue"),
            ("channels/c1/variable", "channels/{id}/variable"),
            ("channels/c1/mute", "channels/{id}/mute"),
            ("channels/c1/applications/Dial", "channels/{id}/applications/{app}"),
            ("bridges/b1/removeChannel", "bridges/{id}/removeChannel"),
            ("bridges/b1/play", "bridges/{id}/play"),
            ("bridges/b1", "bridges/{id}"),
            ("asterisk/info", "asterisk/info"),  # unknown -> passthrough
        ],
    )
    def test_normalize(self, raw, expected):
        assert obs_mod.normalize_ari_resource(raw) == expected


# ──────────────────────────────────────────────────────────────────────
# 3. ari_command records metrics correctly
# ──────────────────────────────────────────────────────────────────────
class TestAriCommand:
    def test_status_class_and_duration(self):
        obs = obs_mod.configure_observability()
        # Should not raise
        obs.ari_command(
            method="POST",
            resource="channels/real-channel-id-123/answer",
            status=200,
            duration=0.042,
        )
        # Verify Prometheus metric was incremented with normalized resource
        sample = obs_mod._ARI_REQUESTS_TOTAL.labels(
            method="POST", resource="channels/{id}/answer", status_class="2xx"
        )
        assert sample._value.get() >= 1

    def test_error_status_increments_error_counter(self):
        obs = obs_mod.configure_observability()
        obs.ari_command(
            method="DELETE",
            resource="channels/some-id",
            status=404,
            duration=0.01,
        )
        sample = obs_mod._ARI_ERRORS_TOTAL.labels(
            method="DELETE", resource="channels/{id}", error_type="http_error"
        )
        assert sample._value.get() >= 1

    def test_no_real_ids_in_prometheus_labels(self):
        """Real IDs must only appear in Logfire attributes, not Prometheus labels."""
        obs = obs_mod.configure_observability()
        obs.ari_command(
            method="GET",
            resource="channels/my-secret-channel-id-999",
            status=200,
            duration=0.01,
            channel_id="my-secret-channel-id-999",
        )
        # The Prometheus counter should use normalized resource
        sample = obs_mod._ARI_REQUESTS_TOTAL.labels(
            method="GET", resource="channels/{id}", status_class="2xx"
        )
        assert sample._value.get() >= 1


# ──────────────────────────────────────────────────────────────────────
# 4. Media connect / disconnect / first_audio
# ──────────────────────────────────────────────────────────────────────
class TestMediaLifecycle:
    def test_connect_disconnect(self):
        obs = obs_mod.configure_observability()
        obs.media_connected(transport="rtp", call_id="c1")
        val = obs_mod._MEDIA_ACTIVE.labels(transport="rtp")._value.get()
        assert val >= 1

        obs.media_disconnected(transport="rtp", call_id="c1")
        val_after = obs_mod._MEDIA_ACTIVE.labels(transport="rtp")._value.get()
        assert val_after < val

    def test_first_audio_records_latency(self):
        obs = obs_mod.configure_observability()
        obs.media_connected(transport="audiosocket", conn_id="conn1")
        time.sleep(0.01)  # small delay
        obs.media_first_audio(transport="audiosocket", conn_id="conn1")
        # Histogram should have at least 1 observation
        h = obs_mod._MEDIA_FIRST_AUDIO.labels(transport="audiosocket")
        assert h._sum.get() > 0


# ──────────────────────────────────────────────────────────────────────
# 5. Double disconnect does not leave counter negative
# ──────────────────────────────────────────────────────────────────────
class TestPacketLossDelta:
    def test_sample_media_uses_delta_not_cumulative(self):
        obs = obs_mod.configure_observability()
        before = obs_mod._MEDIA_PACKET_LOSS.labels(transport="rtp")._value.get()
        obs.sample_media(transport="rtp", packet_loss=10, stalled=0)
        after1 = obs_mod._MEDIA_PACKET_LOSS.labels(transport="rtp")._value.get()
        assert after1 - before == 10
        # Second call with same total should NOT increment again
        obs.sample_media(transport="rtp", packet_loss=10, stalled=0)
        after2 = obs_mod._MEDIA_PACKET_LOSS.labels(transport="rtp")._value.get()
        assert after2 == after1
        # Third call with higher total should increment by delta only
        obs.sample_media(transport="rtp", packet_loss=15, stalled=0)
        after3 = obs_mod._MEDIA_PACKET_LOSS.labels(transport="rtp")._value.get()
        assert after3 - after1 == 5


class TestDoubleDisconnect:
    def test_no_negative_gauge(self):
        obs = obs_mod.configure_observability()
        obs.media_connected(transport="rtp", call_id="c1")
        obs.media_disconnected(transport="rtp", call_id="c1")
        obs.media_disconnected(transport="rtp", call_id="c1")  # double
        val = obs_mod._MEDIA_ACTIVE.labels(transport="rtp")._value.get()
        assert val >= 0


# ──────────────────────────────────────────────────────────────────────
# 6. First audio normalizes negative latency to 0
# ──────────────────────────────────────────────────────────────────────
class TestNegativeLatency:
    def test_negative_connect_time_clamps_to_zero(self):
        obs = obs_mod.configure_observability()
        # Simulate a connect time in the future (which would give negative latency)
        obs._media_connect_times["conn-future"] = time.time() + 999
        obs.media_first_audio(transport="rtp", conn_id="conn-future")
        # Should have observed 0.0, not a negative value
        h = obs_mod._MEDIA_FIRST_AUDIO.labels(transport="rtp")
        # The sum should be >= 0 (no negative contributions)
        assert h._sum.get() >= 0


# ──────────────────────────────────────────────────────────────────────
# 7. Media sampler with fake RTP server
# ──────────────────────────────────────────────────────────────────────
@dataclass
class FakeRTPSession:
    last_packet_at: float = 0.0


@dataclass
class FakeRTPServer:
    sessions: Dict[str, FakeRTPSession] = field(default_factory=dict)
    _fail_get_stats: bool = False

    def get_stats(self) -> Dict[str, Any]:
        if self._fail_get_stats:
            raise RuntimeError("fake stats error")
        return {
            "running": True,
            "sessions_total": len(self.sessions),
            "packet_loss_total": 5,
        }


class TestMediaSampler:
    @pytest.mark.asyncio
    async def test_sampler_success(self):
        obs = obs_mod.configure_observability()
        server = FakeRTPServer()
        now = time.time()
        server.sessions["c1"] = FakeRTPSession(last_packet_at=now)
        server.sessions["c2"] = FakeRTPSession(last_packet_at=now - 60)  # stalled

        task = asyncio.create_task(
            obs.run_media_sampler(rtp_server=server, interval=0.05)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        stalled_val = obs_mod._MEDIA_SESSIONS_STALLED.labels(transport="rtp")._value.get()
        assert stalled_val >= 1

    @pytest.mark.asyncio
    async def test_sampler_get_stats_failure_does_not_break(self):
        obs = obs_mod.configure_observability()
        server = FakeRTPServer(_fail_get_stats=True)

        task = asyncio.create_task(
            obs.run_media_sampler(rtp_server=server, interval=0.05)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # No exception propagated -> test passes

    @pytest.mark.asyncio
    async def test_sampler_stalled_sessions(self):
        obs = obs_mod.configure_observability()
        server = FakeRTPServer()
        now = time.time()
        for i in range(3):
            server.sessions[f"stalled-{i}"] = FakeRTPSession(last_packet_at=now - 120)

        task = asyncio.create_task(
            obs.run_media_sampler(rtp_server=server, interval=0.05)
        )
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        stalled_val = obs_mod._MEDIA_SESSIONS_STALLED.labels(transport="rtp")._value.get()
        assert stalled_val >= 3

    @pytest.mark.asyncio
    async def test_sampler_cancellation_propagates(self):
        obs = obs_mod.configure_observability()
        task = asyncio.create_task(
            obs.run_media_sampler(rtp_server=None, interval=0.05)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

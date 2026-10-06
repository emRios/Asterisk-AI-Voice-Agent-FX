"""§15.A2 — no-media watchdog: hangs up an answered line that never carries audio."""

import asyncio
import types

import pytest
from structlog.testing import capture_logs

from src.core.hangup_service import HangupOutcome, HangupResult
from src.core.models import CallSession
from src.engine import Engine


class Store:
    def __init__(self):
        self.sessions = {}

    async def get_by_call_id(self, call_id):
        return self.sessions.get(call_id)


class FakeHangup:
    def __init__(self):
        self.requests = []

    async def request_hangup(self, channel_id, reason, *, revalidate=None):
        ok = True if revalidate is None else await revalidate()
        self.requests.append((channel_id, reason, ok))
        outcome = HangupOutcome.CONFIRMED if ok else HangupOutcome.SKIPPED
        return HangupResult(outcome, channel_id, reason)


def make_engine(mode="active", secs=0.05):
    e = Engine.__new__(Engine)
    e.session_store = Store()
    e.hangup_service = FakeHangup()
    e._no_media_mode = mode
    e._no_media_secs = secs
    e._no_media_watchdogs = {}
    return e


def session(cid="ch1", **kw):
    s = CallSession(call_id=cid, caller_channel_id=cid)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


async def run_watchdog(e, cid="ch1"):
    e._arm_no_media_watchdog(cid)
    task = e._no_media_watchdogs[cid]
    await task
    return task


async def test_hangs_up_when_no_media_arrives():
    e = make_engine()
    e.session_store.sessions["ch1"] = session()
    await run_watchdog(e)
    assert e.hangup_service.requests == [("ch1", "no_media", True)]
    assert e._no_media_watchdogs == {}


async def test_media_with_bytes_stands_down():
    e = make_engine()
    e.session_store.sessions["ch1"] = session(media_rx_bytes_seen=True)
    await run_watchdog(e)
    assert e.hangup_service.requests == []


async def test_talk_detection_alone_is_not_media():
    # media_rx_confirmed can be set by ChannelTalkingStarted without any payload.
    e = make_engine()
    e.session_store.sessions["ch1"] = session(media_rx_confirmed=True)
    await run_watchdog(e)
    assert e.hangup_service.requests == [("ch1", "no_media", True)]


@pytest.mark.parametrize("flag", ["ai_detached", "transfer_active", "handoff_in_progress"])
async def test_transfers_and_handoffs_are_never_hung_up(flag):
    e = make_engine()
    e.session_store.sessions["ch1"] = session(**{flag: True})
    await run_watchdog(e)
    assert e.hangup_service.requests == []


async def test_revalidation_sees_media_arriving_at_the_deadline():
    e = make_engine()
    s = session()
    e.session_store.sessions["ch1"] = s
    real = e._no_media_hangup_blocker
    calls = {"n": 0}

    async def blocker(cid):
        calls["n"] += 1
        if calls["n"] == 2:  # first check passed; media lands before the lock
            s.media_rx_bytes_seen = True
        return await real(cid)

    e._no_media_hangup_blocker = blocker
    await run_watchdog(e)
    assert e.hangup_service.requests == [("ch1", "no_media", False)]


async def test_setup_without_session_still_hangs_up():
    # Slow or broken setup: the clock runs from the answer, not from session creation.
    e = make_engine()
    await run_watchdog(e)
    assert e.hangup_service.requests == [("ch1", "no_media", True)]


async def test_observe_mode_only_logs():
    e = make_engine(mode="observe")
    e.session_store.sessions["ch1"] = session()
    with capture_logs() as logs:
        await run_watchdog(e)
    assert e.hangup_service.requests == []
    assert any(l["event"] == "No-media watchdog would hang up (observe mode)" for l in logs)


async def test_off_mode_does_not_arm():
    e = make_engine(mode="off")
    e._arm_no_media_watchdog("ch1")
    assert e._no_media_watchdogs == {}


async def test_cleanup_cancels_the_watchdog():
    e = make_engine(secs=5)
    e.session_store.sessions["ch1"] = session()
    e._arm_no_media_watchdog("ch1")
    task = e._no_media_watchdogs["ch1"]
    e._cancel_no_media_watchdog("ch1")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert e._no_media_watchdogs == {}
    assert e.hangup_service.requests == []


async def test_arming_twice_keeps_one_watchdog():
    e = make_engine(secs=5)
    e._arm_no_media_watchdog("ch1")
    first = e._no_media_watchdogs["ch1"]
    e._arm_no_media_watchdog("ch1")
    assert e._no_media_watchdogs["ch1"] is first
    e._cancel_no_media_watchdog("ch1")


async def test_deadline_is_measured_from_arming():
    e = make_engine(secs=0.3)
    e.session_store.sessions["ch1"] = session()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await run_watchdog(e)
    assert 0.25 <= loop.time() - t0 < 1.0


async def test_cleanup_call_cancels_watchdog():
    e = make_engine(secs=5)
    e._arm_no_media_watchdog("ch1")
    task = e._no_media_watchdogs["ch1"]

    class EmptyStore(Store):
        async def get_by_channel_id(self, _):
            return None

    e.session_store = EmptyStore()
    e._attended_transfer_agent_channel_to_call_id = {}
    try:
        await e._cleanup_call("ch1")
    except Exception:
        pass  # the rest of cleanup needs a full engine; only the cancel matters here
    await asyncio.sleep(0)
    assert task.cancelled()


async def test_audiosocket_empty_frame_does_not_count_as_media():
    import inspect

    src = inspect.getsource(Engine)
    # both inbound media paths gate the flag on a non-empty payload
    assert "if audio_bytes and not session.media_rx_bytes_seen:" in src
    assert "if pcm_16k and not session.media_rx_bytes_seen:" in src


async def test_hybrid_caller_path_arms_the_watchdog_after_answer():
    import inspect

    src = inspect.getsource(Engine._handle_caller_stasis_start_hybrid)
    answer = src.index("Step 1: Skipping answer (outbound)")
    arm = src.index("self._arm_no_media_watchdog(caller_channel_id)")
    bridge = src.index("Step 2: Creating bridge immediately")
    assert answer < arm < bridge

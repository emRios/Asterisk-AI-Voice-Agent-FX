"""§15.A1 — verifiable hangup: HangupService, immediate route from the server, engine wiring."""

import asyncio
import json
import types

import pytest

from src.core.hangup_service import HangupOutcome, HangupService
from src.engine import Engine
from src.providers.deepgram import DeepgramProvider


class FakeARI:
    """DELETE/GET with scripted statuses; optionally reports the channel gone after a DELETE."""

    def __init__(self, delete_statuses, get_statuses=(), *, on_delete=None, delete_delay=0.0):
        self.delete_statuses = list(delete_statuses)
        self.get_statuses = list(get_statuses)
        self.on_delete = on_delete
        self.delete_delay = delete_delay
        self.calls = []

    async def hangup_channel(self, channel_id):
        self.calls.append(("DELETE", channel_id))
        if self.delete_delay:
            await asyncio.sleep(self.delete_delay)
        status = self.delete_statuses.pop(0)
        if isinstance(status, BaseException):
            raise status
        if self.on_delete:
            self.on_delete(channel_id)
        return {"status": status}

    async def send_command(self, method, resource, tolerate_statuses=None, **_):
        self.calls.append((method, resource))
        status = self.get_statuses.pop(0)
        return {"status": status} if status != 200 else {"id": resource.split("/")[-1], "state": "Up"}


def svc(ari, **kw):
    kw.setdefault("request_timeout_secs", 0.2)
    kw.setdefault("confirm_timeout_secs", 0.2)
    return HangupService(ari, **kw)


def gone_soon(service, delay=0.01):
    loop = asyncio.get_running_loop()
    return lambda cid: loop.call_later(delay, service.notify_channel_gone, cid)


async def test_confirmed_when_ari_reports_channel_gone():
    ari = FakeARI([204])
    s = svc(ari)
    ari.on_delete = gone_soon(s)
    r = await s.request_hangup("ch1", "no_media")
    assert r.outcome is HangupOutcome.CONFIRMED
    assert ari.calls == [("DELETE", "ch1")]


async def test_gone_event_before_delete_returns_is_still_confirmed():
    # StasisEnd can race ahead of the DELETE response: the waiter is registered first.
    ari = FakeARI([204])
    s = svc(ari)
    ari.on_delete = lambda cid: s.notify_channel_gone(cid)
    r = await s.request_hangup("ch1", "x")
    assert r.outcome is HangupOutcome.CONFIRMED


async def test_404_on_delete_is_already_gone():
    ari = FakeARI([404])
    r = await svc(ari).request_hangup("ch1", "x")
    assert r.outcome is HangupOutcome.ALREADY_GONE
    assert r.attempts == 1


async def test_no_event_but_get_404_is_already_gone():
    ari = FakeARI([204], [404])
    r = await svc(ari).request_hangup("ch1", "x")
    assert r.outcome is HangupOutcome.ALREADY_GONE
    assert ari.calls == [("DELETE", "ch1"), ("GET", "channels/ch1")]


async def test_5xx_then_retry_confirms():
    ari = FakeARI([500, 204], [200])
    s = svc(ari)
    calls = {"n": 0}

    def second_delete_kills(cid):
        calls["n"] += 1
        if calls["n"] == 2:
            gone_soon(s)(cid)

    ari.on_delete = second_delete_kills
    r = await s.request_hangup("ch1", "x")
    assert r.outcome is HangupOutcome.CONFIRMED
    assert r.attempts == 2


async def test_stuck_channel_fails_after_one_retry():
    ari = FakeARI([204, 204], [200, 200])
    r = await svc(ari).request_hangup("stuck", "no_media")
    assert r.outcome is HangupOutcome.FAILED
    assert r.attempts == 2
    assert [c[0] for c in ari.calls] == ["DELETE", "GET", "DELETE", "GET"]


async def test_delete_timeout_counts_as_unconfirmed_and_retries():
    ari = FakeARI([204, 204], [200, 200], delete_delay=0.5)  # > request timeout
    r = await svc(ari).request_hangup("ch1", "x")
    assert r.statuses == ["timeout", "GET:200", "timeout", "GET:200"]
    assert r.outcome is HangupOutcome.FAILED


async def test_delete_exception_is_contained():
    ari = FakeARI([ConnectionError("boom"), 404], [200])
    r = await svc(ari).request_hangup("ch1", "x")
    assert r.statuses[0] == "error:ConnectionError"
    assert r.outcome is HangupOutcome.ALREADY_GONE


async def test_concurrent_requests_share_one_hangup():
    ari = FakeARI([204])
    s = svc(ari)
    ari.on_delete = gone_soon(s, 0.05)
    r1, r2 = await asyncio.gather(s.request_hangup("ch1", "a"), s.request_hangup("ch1", "b"))
    assert r1 is r2
    assert ari.calls == [("DELETE", "ch1")]


async def test_revalidate_false_skips_without_touching_ari():
    ari = FakeARI([])

    async def no():
        return False

    r = await svc(ari).request_hangup("ch1", "no_media", revalidate=no)
    assert r.outcome is HangupOutcome.SKIPPED
    assert ari.calls == []


async def test_caller_cancellation_does_not_abort_hangup():
    ari = FakeARI([204])
    s = svc(ari, confirm_timeout_secs=1.0)
    caller = asyncio.create_task(s.request_hangup("ch1", "x"))
    await asyncio.sleep(0.02)
    assert s.in_progress("ch1")
    hangup = s._inflight["ch1"]
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not hangup.cancelled()
    s.notify_channel_gone("ch1")
    r = await hangup
    assert r.outcome is HangupOutcome.CONFIRMED
    assert ari.calls == [("DELETE", "ch1")]


async def test_notify_without_waiter_is_noop():
    s = svc(FakeARI([]))
    s.notify_channel_gone("nobody")
    s.notify_channel_gone(None)


async def test_explicit_error_skips_confirmation_wait():
    ari = FakeARI([500, 500], [200, 200])
    s = svc(ari, confirm_timeout_secs=5.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await s.request_hangup("ch1", "x")
    assert r.outcome is HangupOutcome.FAILED
    assert loop.time() - t0 < 1.0  # no 5 s waits after explicit 500s


async def test_get_timeout_and_malformed_responses():
    class SlowGet(FakeARI):
        async def send_command(self, method, resource, tolerate_statuses=None, **_):
            self.calls.append((method, resource))
            await asyncio.sleep(1)

    ari = SlowGet([None, None])
    ari.hangup_channel = None

    async def malformed(cid):
        ari.calls.append(("DELETE", cid))
        return None

    ari.hangup_channel = malformed
    r = await svc(ari).request_hangup("ch1", "x")
    assert r.statuses == ["malformed", "GET:timeout", "malformed", "GET:timeout"]
    assert r.outcome is HangupOutcome.FAILED


async def test_failed_logs_error():
    from structlog.testing import capture_logs

    ari = FakeARI([204, 204], [200, 200])
    with capture_logs() as logs:
        await svc(ari).request_hangup("stuck", "no_media")
    assert any(l["log_level"] == "error" and l["event"] == "Channel could not be hung up" for l in logs)


@pytest.mark.parametrize("mode", ["false", "raises", "cancelled"])
async def test_lock_entry_released_when_no_task_is_created(mode):
    s = svc(FakeARI([]))

    async def revalidate():
        if mode == "raises":
            raise RuntimeError("boom")
        if mode == "cancelled":
            await asyncio.sleep(10)
        return False

    req = asyncio.create_task(s.request_hangup("ch1", "x", revalidate=revalidate))
    if mode == "cancelled":
        await asyncio.sleep(0.01)
        req.cancel()
    try:
        await req
    except (RuntimeError, asyncio.CancelledError):
        pass
    assert s._locks == {}
    assert s._inflight == {}


async def test_lock_entry_released_after_hangup():
    ari = FakeARI([404])
    s = svc(ari)
    await s.request_hangup("ch1", "x")
    await asyncio.sleep(0)
    assert s._locks == {} and s._inflight == {}


# ------------------------------------------------------------ provider route
class WS:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


def make_provider(events):
    async def on_event(e):
        events.append(e)

    p = DeepgramProvider({}, types.SimpleNamespace(), on_event)
    p.call_id = "call-1"
    p.websocket = WS()
    return p


def fcr(args, name="hangup_call"):
    return {"type": "FunctionCallRequest",
            "functions": [{"id": "fc1", "name": name, "arguments": json.dumps(args)}]}


async def test_server_hangup_message_emits_immediate_hangup():
    events = []
    p = make_provider(events)
    await p._handle_server_hangup({"type": "ServerHangup", "reason": "no_media"})
    assert events == [{"type": "HangupImmediate", "call_id": "call-1", "reason": "no_media"}]
    assert p._hangup_pending is False


async def test_server_hangup_is_dispatched_by_the_receive_loop():
    import inspect

    src = inspect.getsource(DeepgramProvider._receive_loop)
    assert 'et == "ServerHangup"' in src and "_handle_server_hangup" in src


@pytest.mark.parametrize("args", [{"immediate": True}, {}, {"immediate": True, "reason": "x"}, {"farewell_message": "adiós"}])
async def test_llm_hangup_call_always_goes_through_the_tool_adapter(args):
    # An LLM tool call can never trigger the immediate path, whatever its arguments.
    events = []
    p = make_provider(events)
    seen = []

    async def adapter(event, ctx):
        seen.append(event)
        return {"status": "error", "function_name": "hangup_call", "function_call_id": "fc1"}

    p.tool_adapter.handle_tool_call_event = adapter
    await p._handle_function_call(fcr(args))
    assert len(seen) == 1
    assert not any(e.get("type") == "HangupImmediate" for e in events)


# --------------------------------------------------------------- engine wiring
class Store:
    def __init__(self, session):
        self.session = session

    async def get_by_call_id(self, call_id):
        return self.session if call_id == "call-1" else None


def make_engine(ari):
    e = Engine.__new__(Engine)
    e.hangup_service = HangupService(ari, request_timeout_secs=0.2, confirm_timeout_secs=0.5)
    e._hangup_tasks = set()
    e.session_store = Store(types.SimpleNamespace(caller_channel_id="chan-9", call_id="call-1"))
    e._outbound_awaiting_amd_channel_ids = set()
    cleaned = []

    async def cleanup(cid):
        cleaned.append(cid)

    async def outbound_destroyed(_ev):
        return None

    e._cleanup_call = cleanup
    e._handle_outbound_channel_destroyed = outbound_destroyed
    return e, cleaned


async def test_engine_hangup_immediate_confirmed_by_channel_destroyed():
    ari = FakeARI([204])
    e, cleaned = make_engine(ari)
    await e.on_provider_event({"type": "HangupImmediate", "call_id": "call-1", "reason": "no_media"})
    assert len(e._hangup_tasks) == 1
    task = next(iter(e._hangup_tasks))
    await asyncio.sleep(0.02)
    await e._handle_channel_destroyed({"channel": {"id": "chan-9"}})
    r = await task
    assert r.outcome is HangupOutcome.CONFIRMED and r.reason == "no_media"
    assert cleaned == ["chan-9"]
    await asyncio.sleep(0)
    assert e._hangup_tasks == set()


@pytest.mark.parametrize("amd_hop", [False, True])
async def test_engine_stasis_end_never_confirms(amd_hop):
    # Transfer via continue / AMD hop leave Stasis with the channel alive.
    ari = FakeARI([204, 204], [200, 200])
    e, _ = make_engine(ari)
    if amd_hop:
        e._outbound_awaiting_amd_channel_ids.add("chan-9")
    await e.on_provider_event({"type": "HangupImmediate", "call_id": "call-1", "reason": "x"})
    task = next(iter(e._hangup_tasks))
    await asyncio.sleep(0.02)
    await e._handle_stasis_end({"channel": {"id": "chan-9"}})
    r = await task
    assert r.outcome is HangupOutcome.FAILED  # the GET still sees the channel


async def test_engine_delete_500_with_stasis_end_does_not_confirm():
    ari = FakeARI([500, 500], [200, 200])
    e, _ = make_engine(ari)
    ari.on_delete = lambda cid: asyncio.get_running_loop().create_task(
        e._handle_stasis_end({"channel": {"id": cid}}))
    await e.on_provider_event({"type": "HangupImmediate", "call_id": "call-1"})
    r = await next(iter(e._hangup_tasks))
    assert r.outcome is HangupOutcome.FAILED


async def test_channel_destroyed_during_get_confirms():
    class GetRace(FakeARI):
        async def send_command(self, method, resource, tolerate_statuses=None, **_):
            self.calls.append((method, resource))
            self.service.notify_channel_gone("ch1")  # destroyed while the GET is in flight
            return {"id": "ch1", "state": "Up"}

    ari = GetRace([204])
    s = svc(ari)
    ari.service = s
    r = await s.request_hangup("ch1", "x")
    assert r.outcome is HangupOutcome.CONFIRMED
    assert r.attempts == 1

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


async def test_immediate_hangup_bypasses_farewell_path():
    events = []
    p = make_provider(events)

    async def must_not_run(*_a, **_k):
        raise AssertionError("tool adapter must not run for immediate hangup")

    p.tool_adapter.handle_tool_call_event = must_not_run
    await p._handle_function_call(fcr({"immediate": True, "reason": "no_media"}))
    assert events == [{"type": "HangupImmediate", "call_id": "call-1", "reason": "no_media"}]
    assert p._hangup_pending is False
    ack = json.loads(p.websocket.sent[0])
    assert ack["type"] == "FunctionCallResponse" and ack["id"] == "fc1" and ack["name"] == "hangup_call"


@pytest.mark.parametrize("args", [{}, {"immediate": False}, {"immediate": "true"}, {"farewell_message": "adiós"}])
async def test_normal_hangup_keeps_farewell_path(args):
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


async def test_other_tools_untouched():
    events = []
    p = make_provider(events)
    seen = []

    async def adapter(event, ctx):
        seen.append(event)
        return {"status": "success", "function_name": "transfer", "function_call_id": "fc1"}

    p.tool_adapter.handle_tool_call_event = adapter
    await p._handle_function_call(fcr({"immediate": True}, name="transfer"))
    assert len(seen) == 1 and events == []


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


async def test_engine_hangup_immediate_confirmed_by_stasis_end():
    ari = FakeARI([204])
    e, cleaned = make_engine(ari)
    await e.on_provider_event({"type": "HangupImmediate", "call_id": "call-1", "reason": "no_media"})
    assert len(e._hangup_tasks) == 1
    task = next(iter(e._hangup_tasks))
    await asyncio.sleep(0.02)
    await e._handle_stasis_end({"channel": {"id": "chan-9"}})
    r = await task
    assert r.outcome is HangupOutcome.CONFIRMED and r.reason == "no_media"
    assert cleaned == ["chan-9"]
    await asyncio.sleep(0)
    assert e._hangup_tasks == set()


async def test_engine_amd_hop_stasis_end_does_not_confirm():
    ari = FakeARI([204], [404])
    e, cleaned = make_engine(ari)
    e._outbound_awaiting_amd_channel_ids.add("chan-9")
    await e.on_provider_event({"type": "HangupImmediate", "call_id": "call-1", "reason": "x"})
    task = next(iter(e._hangup_tasks))
    await asyncio.sleep(0.02)
    await e._handle_stasis_end({"channel": {"id": "chan-9"}})
    r = await task
    assert r.outcome is HangupOutcome.ALREADY_GONE  # resolved by the GET, not by the hop
    assert cleaned == []


async def test_engine_channel_destroyed_confirms():
    ari = FakeARI([204])
    e, _ = make_engine(ari)
    await e.on_provider_event({"type": "HangupImmediate", "call_id": "call-1"})
    task = next(iter(e._hangup_tasks))
    await asyncio.sleep(0.02)
    await e._handle_channel_destroyed({"channel": {"id": "chan-9"}})
    r = await task
    assert r.outcome is HangupOutcome.CONFIRMED and r.reason == "server_immediate"

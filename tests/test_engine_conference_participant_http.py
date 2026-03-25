import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.models import CallSession
from src.engine import Engine
import src.tools.registry as registry_module


class _DummyTransport:
    def get_extra_info(self, name):
        return None


class _DummyRequest:
    def __init__(self, *, call_id: str, payload):
        self.match_info = {"call_id": call_id}
        self._payload = payload
        self.headers = {}
        self.transport = _DummyTransport()

    async def json(self):
        return self._payload


class _FakeTool:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def execute(self, parameters, context):
        self.calls.append({"parameters": parameters, "context": context})
        return self.result


@pytest.mark.asyncio
async def test_conference_participant_http_handler_executes_tool(monkeypatch):
    eng = Engine.__new__(Engine)
    eng._is_request_authorized = lambda request: True
    eng.session_store = SimpleNamespace(get_by_call_id=AsyncMock())
    eng.ari_client = AsyncMock()
    eng.config = {"tools": {"conference_participant": {"enabled": True}}}

    session = CallSession(
        call_id="1760000000.2000",
        caller_channel_id="caller-chan-1",
        bridge_id="bridge-1",
    )
    eng.session_store.get_by_call_id.return_value = session

    fake_tool = _FakeTool(
        {
            "status": "success",
            "message": "Please hold while I add Support agent to the call.",
            "type": "conference_participant",
        }
    )
    monkeypatch.setattr(registry_module.tool_registry, "get", lambda name: fake_tool if name == "conference_participant" else None)

    request = _DummyRequest(
        call_id="1760000000.2000",
        payload={"destination": "support_agent", "mode": "talk"},
    )

    response = await eng._conference_participant_http_handler(request)

    assert response.status == 200
    body = json.loads(response.text)
    assert body["status"] == "success"
    assert fake_tool.calls
    assert fake_tool.calls[0]["parameters"] == {
        "destination": "support_agent",
        "mode": "talk",
        "channel_id": None,
    }
    assert fake_tool.calls[0]["context"].call_id == "1760000000.2000"
    assert fake_tool.calls[0]["context"].bridge_id == "bridge-1"


@pytest.mark.asyncio
async def test_conference_participant_http_handler_requires_authorization():
    eng = Engine.__new__(Engine)
    eng._is_request_authorized = lambda request: False

    request = _DummyRequest(
        call_id="1760000000.2001",
        payload={"destination": "support_agent", "mode": "talk"},
    )

    response = await eng._conference_participant_http_handler(request)

    assert response.status == 403
    body = json.loads(response.text)
    assert body["status"] == "error"

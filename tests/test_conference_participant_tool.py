import pytest

from src.core.models import CallSession
from src.tools.telephony.conference_participant import ConferenceParticipantTool


class _FakeEngine:
    def __init__(self):
        self.timeout_calls = []

    def start_conference_participant_timeout_guard(self, call_id: str, participant_channel_id: str, *, timeout_sec: float) -> None:
        self.timeout_calls.append(
            {
                "call_id": call_id,
                "participant_channel_id": participant_channel_id,
                "timeout_sec": timeout_sec,
            }
        )


class _FakeAriClient:
    def __init__(self, *, originate_response=None, engine=None):
        self.calls = []
        self._originate_response = originate_response
        self.engine = engine

    async def send_command(self, *, method: str, resource: str, data=None, params=None):
        self.calls.append(
            {
                "method": method,
                "resource": resource,
                "data": data,
                "params": params,
            }
        )
        if method == "POST" and resource == "channels":
            return self._originate_response
        return {"ok": True}


class _FakeSessionStore:
    def __init__(self):
        self.sessions = {}

    async def upsert_call(self, session: CallSession) -> None:
        self.sessions[session.call_id] = session

    async def get_by_call_id(self, call_id: str):
        return self.sessions.get(call_id)


class _FakeContext:
    def __init__(self, *, config: dict, caller_channel_id: str, ari_client: _FakeAriClient, session: CallSession):
        self._config = config
        self.caller_channel_id = caller_channel_id
        self.ari_client = ari_client
        self.session_store = _FakeSessionStore()
        self._session = session

    def get_config_value(self, key: str, default=None):
        cur = self._config
        for part in (key or "").split("."):
            if not part:
                continue
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur

    async def get_session(self) -> CallSession:
        return self._session


def test_definition():
    tool = ConferenceParticipantTool()
    definition = tool.definition

    assert definition.name == "conference_participant"
    assert definition.category.value == "telephony"
    assert definition.requires_channel is True
    assert definition.max_execution_time == 30
    assert len(definition.parameters) == 3
    assert [param.name for param in definition.parameters] == [
        "destination",
        "mode",
        "channel_id",
    ]


@pytest.mark.asyncio
async def test_execute_success_prepares_originate_contract():
    tool = ConferenceParticipantTool()
    call_id = "1760000000.1000"
    session = CallSession(call_id=call_id, caller_channel_id=call_id, bridge_id="bridge-1")
    engine = _FakeEngine()
    ari = _FakeAriClient(originate_response={"id": "participant-chan-1"}, engine=engine)
    context = _FakeContext(
        config={
            "asterisk": {"app_name": "asterisk-ai-voice-agent"},
            "tools": {
                "ai_identity": {"name": "Ava", "number": "6789"},
                "conference_participant": {"enabled": True, "dial_timeout_seconds": 20},
                "transfer": {
                    "technology": "PJSIP",
                    "destinations": {
                        "support_agent": {
                            "type": "extension",
                            "target": "6000",
                            "description": "Support agent",
                            "timeout": 18,
                        }
                    },
                },
            },
        },
        caller_channel_id=call_id,
        ari_client=ari,
        session=session,
    )

    result = await tool.execute({"destination": "support_agent", "mode": "listen"}, context)

    assert result["status"] == "success"
    assert result["type"] == "conference_participant"
    assert result["mode"] == "listen"
    assert result["bridge_id"] == "bridge-1"
    assert result["channel_id"] is None
    assert session.current_action and session.current_action["type"] == "conference-participant"
    assert session.current_action["target"] == "6000"
    assert session.current_action["channel_id"] is None
    assert session.current_action["mode"] == "listen"
    assert result["originate"]["endpoint"] == "PJSIP/6000"
    assert result["originate"]["app"] == "asterisk-ai-voice-agent"
    assert result["originate"]["appArgs"] == f"conference-participant,{call_id},6000,mode=listen"
    assert result["originate"]["variables"]["AGENT_CALL_ID"] == call_id

    assert ari.calls == []
    assert engine.timeout_calls == []


@pytest.mark.asyncio
async def test_execute_supports_legacy_internal_extension_mapping():
    tool = ConferenceParticipantTool()
    call_id = "1760000000.1001"
    session = CallSession(call_id=call_id, caller_channel_id=call_id, bridge_id="bridge-1")
    ari = _FakeAriClient(originate_response={"id": "participant-chan-2"})
    context = _FakeContext(
        config={
            "tools": {
                "conference_participant": {"enabled": True},
                "extensions": {
                    "internal": {
                        "6001": {
                            "name": "Sales Department",
                            "aliases": ["sales"],
                            "dial_string": "SIP/6001",
                            "timeout": 25,
                        }
                    }
                },
            },
        },
        caller_channel_id=call_id,
        ari_client=ari,
        session=session,
    )

    result = await tool.execute({"destination": "sales"}, context)

    assert result["status"] == "success"
    assert result["originate"]["endpoint"] == "SIP/6001"
    assert result["originate"]["appArgs"] == f"conference-participant,{call_id},6001,mode=talk"
    assert ari.calls == []


@pytest.mark.asyncio
async def test_execute_can_register_external_channel_id_and_schedule_timeout_guard():
    tool = ConferenceParticipantTool()
    call_id = "1760000000.1002"
    session = CallSession(call_id=call_id, caller_channel_id=call_id, bridge_id="bridge-1")
    engine = _FakeEngine()
    ari = _FakeAriClient(originate_response=None, engine=engine)
    context = _FakeContext(
        config={
            "tools": {
                "conference_participant": {"enabled": True},
                "transfer": {
                    "destinations": {
                        "support_agent": {
                            "type": "extension",
                            "target": "6000",
                            "description": "Support agent",
                        }
                    }
                },
            },
        },
        caller_channel_id=call_id,
        ari_client=ari,
        session=session,
    )

    result = await tool.execute({"destination": "support_agent", "channel_id": "PJSIP/6000-00000001"}, context)

    assert result["status"] == "success"
    assert session.current_action is not None
    assert session.current_action["channel_id"] == "PJSIP/6000-00000001"
    assert result["channel_id"] == "PJSIP/6000-00000001"
    assert engine.timeout_calls == [
        {
            "call_id": call_id,
            "participant_channel_id": "PJSIP/6000-00000001",
            "timeout_sec": 30,
        }
    ]


@pytest.mark.asyncio
async def test_execute_rejects_when_another_action_is_active():
    tool = ConferenceParticipantTool()
    call_id = "1760000000.1003"
    session = CallSession(call_id=call_id, caller_channel_id=call_id, bridge_id="bridge-1")
    session.current_action = {"type": "attended_transfer", "answered": False}
    ari = _FakeAriClient(originate_response={"id": "participant-chan-3"})
    context = _FakeContext(
        config={"tools": {"conference_participant": {"enabled": True}}},
        caller_channel_id=call_id,
        ari_client=ari,
        session=session,
    )

    result = await tool.execute({"destination": "6000"}, context)

    assert result["status"] == "failed"
    assert "already in progress" in result["message"].lower()
    assert not ari.calls


@pytest.mark.asyncio
async def test_execute_allows_registering_existing_conference_participant_action():
    tool = ConferenceParticipantTool()
    call_id = "1760000000.1004"
    session = CallSession(call_id=call_id, caller_channel_id=call_id, bridge_id="bridge-1")
    session.current_action = {
        "type": "conference-participant",
        "target": "6000",
        "target_name": "Support agent",
        "dial_endpoint": "PJSIP/6000",
        "dial_timeout_seconds": 18,
        "mode": "talk",
        "started_at": 123.0,
        "channel_id": None,
        "answered": False,
    }
    engine = _FakeEngine()
    ari = _FakeAriClient(engine=engine)
    context = _FakeContext(
        config={
            "tools": {
                "conference_participant": {"enabled": True},
                "transfer": {
                    "destinations": {
                        "support_agent": {
                            "type": "extension",
                            "target": "6000",
                            "description": "Support agent",
                            "timeout": 18,
                        }
                    }
                },
            },
        },
        caller_channel_id=call_id,
        ari_client=ari,
        session=session,
    )

    result = await tool.execute(
        {"destination": "support_agent", "channel_id": "PJSIP/6000-00000009"},
        context,
    )

    assert result["status"] == "success"
    assert session.current_action["channel_id"] == "PJSIP/6000-00000009"
    assert session.current_action["started_at"] == 123.0
    assert engine.timeout_calls == [
        {
            "call_id": call_id,
            "participant_channel_id": "PJSIP/6000-00000009",
            "timeout_sec": 18,
        }
    ]

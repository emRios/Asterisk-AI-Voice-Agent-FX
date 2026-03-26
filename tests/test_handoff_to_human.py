"""Tests for handoff_to_human engine method and tool."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field
from typing import Optional, Set, Dict, Any, List


@dataclass
class FakeSession:
    call_id: str = "call-1"
    caller_channel_id: str = "call-1"
    bridge_id: Optional[str] = "bridge-1"
    ai_detached: bool = False
    human_channel_id: Optional[str] = None
    human_audio_mode: str = "talk"
    current_action: Optional[Dict[str, Any]] = None
    provider_session_active: bool = True
    audio_capture_enabled: bool = True
    tts_playing: bool = False
    tts_active_count: int = 0
    tts_tokens: Set[str] = field(default_factory=set)
    external_media_id: Optional[str] = None
    pending_external_media_id: Optional[str] = None
    audiosocket_channel_id: Optional[str] = "audiosocket-1"
    audiosocket_conn_id: Optional[str] = None
    audiosocket_uuid: Optional[str] = None
    local_channel_id: Optional[str] = None
    external_media_port: Optional[int] = None
    status: str = "connected"


class FakeSessionStore:
    def __init__(self, session):
        self._session = session

    async def get_by_call_id(self, call_id):
        if self._session and self._session.call_id == call_id:
            return self._session
        return None

    async def upsert_call(self, session):
        self._session = session

    async def unindex_channel(self, channel_id):
        pass


class FakeAriClient:
    def __init__(self):
        self.unmute_channel = AsyncMock(return_value=True)
        self.mute_channel = AsyncMock(return_value=True)
        self.remove_channel_from_bridge = AsyncMock(return_value=True)
        self.hangup_channel = AsyncMock(return_value=True)

    async def send_command(self, *args, **kwargs):
        return {"status": 204}


def make_engine(session):
    """Build a minimal engine-like object with just the methods under test."""
    store = FakeSessionStore(session)
    ari = FakeAriClient()

    class MinimalEngine:
        def __init__(self):
            self.session_store = store
            self.ari_client = ari
            self.streaming_playback_manager = MagicMock()
            self.streaming_playback_manager.stop_streaming_playback = AsyncMock()
            self._provider_start_tasks = {}
            self._call_providers = {}
            self._pipeline_tasks = {}
            self._pipeline_queues = {}
            self._pipeline_forced = {}
            self.bridges = {}
            self.pending_local_channels = {}
            self.pending_audiosocket_channels = {}
            self.local_channels = {}
            self.audiosocket_channels = {}
            self.uuidext_to_channel = {}
            self.pipeline_orchestrator = MagicMock()
            self.pipeline_orchestrator.enabled = False

        async def _save_session(self, s):
            await self.session_store.upsert_call(s)

        async def _disable_pipeline_talk_detect(self, s):
            pass

    engine = MinimalEngine()

    # Bind the real methods from the Engine class
    import types
    from src.engine import Engine

    for method_name in ("set_human_audio_mode", "handoff_to_human", "disconnect_ai_from_call"):
        method = getattr(Engine, method_name)
        bound = types.MethodType(method, engine)
        setattr(engine, method_name, bound)

    return engine


@pytest.mark.asyncio
async def test_handoff_success_from_listen():
    """Human in listen mode gets promoted to talk, then AI disconnects."""
    session = FakeSession(
        human_channel_id="human-ch-1",
        human_audio_mode="listen",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "success"
    assert result["human_promoted"] is True
    assert result["ai_disconnected"] is True
    # Unmute was called (promotion from listen to talk)
    engine.ari_client.unmute_channel.assert_called()
    assert session.ai_detached is True
    assert session.human_audio_mode == "talk"


@pytest.mark.asyncio
async def test_handoff_success_already_talk():
    """Human already in talk mode — promotion skipped, AI still disconnects."""
    session = FakeSession(
        human_channel_id="human-ch-1",
        human_audio_mode="talk",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "success"
    assert result["human_promoted"] is True
    assert result["ai_disconnected"] is True
    # No unmute call needed — already in talk
    engine.ari_client.unmute_channel.assert_not_called()


@pytest.mark.asyncio
async def test_handoff_fails_no_human():
    """Handoff fails when no human channel is connected."""
    session = FakeSession(
        human_channel_id=None,
        current_action={"type": "conference-participant", "answered": False},
    )
    engine = make_engine(session)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "error"
    assert result["human_promoted"] is False
    assert result["ai_disconnected"] is False
    assert session.ai_detached is False


@pytest.mark.asyncio
async def test_handoff_fails_promotion_error():
    """If unmute fails, AI stays connected."""
    session = FakeSession(
        human_channel_id="human-ch-1",
        human_audio_mode="listen",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)
    engine.ari_client.unmute_channel = AsyncMock(return_value=False)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "error"
    assert result["human_promoted"] is False
    assert result["ai_disconnected"] is False
    assert session.ai_detached is False


@pytest.mark.asyncio
async def test_handoff_fails_ai_already_detached():
    """Handoff fails when AI is already disconnected."""
    session = FakeSession(
        ai_detached=True,
        human_channel_id="human-ch-1",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "error"
    assert result["ai_disconnected"] is True
    assert result["human_promoted"] is False


@pytest.mark.asyncio
async def test_disconnect_ai_reports_error_when_all_detach_fail():
    """disconnect_ai_from_call must return error and NOT mutate session when all detaches fail."""
    session = FakeSession(
        audiosocket_channel_id="audiosocket-1",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)
    engine.ari_client.remove_channel_from_bridge = AsyncMock(return_value=False)

    result = await engine.disconnect_ai_from_call("call-1")

    assert result["status"] == "error"
    assert result["ai_disconnected"] is False
    assert "audiosocket-1" in result["failed_detach_channels"]
    # Session must NOT be mutated when all detaches fail
    assert session.ai_detached is False
    assert session.audiosocket_channel_id == "audiosocket-1"
    assert session.status == "connected"


@pytest.mark.asyncio
async def test_disconnect_ai_session_mutated_on_success():
    """disconnect_ai_from_call must mutate session when detach succeeds."""
    session = FakeSession(
        audiosocket_channel_id="audiosocket-1",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)
    engine.ari_client.remove_channel_from_bridge = AsyncMock(return_value=True)

    result = await engine.disconnect_ai_from_call("call-1")

    assert result["status"] == "success"
    assert result["ai_disconnected"] is True
    assert session.ai_detached is True
    assert session.audiosocket_channel_id is None
    assert session.status == "ai_detached"


@pytest.mark.asyncio
async def test_disconnect_partial_selective_cleanup():
    """In partial disconnect, only detached channels get their maps cleaned.
    audiosocket detaches OK → session field + call-level maps cleared.
    local fails detach → session field + call-level maps preserved."""
    session = FakeSession(
        audiosocket_channel_id="audiosocket-1",
        audiosocket_uuid="uuid-1",
        local_channel_id="local-1",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)
    engine.audiosocket_channels["call-1"] = "audiosocket-1"
    engine.local_channels["call-1"] = "local-1"
    engine.uuidext_to_channel["uuid-1"] = "call-1"

    async def remove_side_effect(bridge_id, channel_id):
        # audiosocket succeeds, local fails
        return channel_id == "audiosocket-1"

    engine.ari_client.remove_channel_from_bridge = AsyncMock(side_effect=remove_side_effect)

    result = await engine.disconnect_ai_from_call("call-1")

    assert result["status"] == "partial"
    # audiosocket was detached: session field and call-level maps cleared
    assert session.audiosocket_channel_id is None
    assert "call-1" not in engine.audiosocket_channels
    assert "uuid-1" not in engine.uuidext_to_channel
    # local was NOT detached: session field and call-level map preserved
    assert session.local_channel_id == "local-1"
    assert engine.local_channels.get("call-1") == "local-1"


@pytest.mark.asyncio
async def test_handoff_reports_partial_when_detach_partial():
    """handoff_to_human must report partial when disconnect_ai had partial failures."""
    session = FakeSession(
        human_channel_id="human-ch-1",
        human_audio_mode="talk",
        audiosocket_channel_id="audiosocket-1",
        local_channel_id="local-1",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)

    call_count = 0
    async def remove_side_effect(bridge_id, channel_id):
        nonlocal call_count
        call_count += 1
        return call_count == 1  # first succeeds, second fails

    engine.ari_client.remove_channel_from_bridge = AsyncMock(side_effect=remove_side_effect)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "partial"
    assert result["human_promoted"] is True
    assert result["ai_disconnected"] is True
    assert len(result["failed_detach_channels"]) > 0


@pytest.mark.asyncio
async def test_handoff_reports_error_when_disconnect_fully_fails():
    """handoff_to_human must report error when disconnect_ai fails completely.
    Provider and session must be preserved for retryability."""
    session = FakeSession(
        human_channel_id="human-ch-1",
        human_audio_mode="talk",
        audiosocket_channel_id="audiosocket-1",
        current_action={"type": "conference-participant", "answered": True, "channel_id": "human-ch-1"},
    )
    engine = make_engine(session)
    engine.ari_client.remove_channel_from_bridge = AsyncMock(return_value=False)

    result = await engine.handoff_to_human("call-1")

    assert result["status"] == "error"
    assert result["human_promoted"] is True
    assert result["ai_disconnected"] is False
    # Provider/session must NOT have been torn down
    assert session.ai_detached is False
    assert session.provider_session_active is True
    assert session.audiosocket_channel_id == "audiosocket-1"


@pytest.mark.asyncio
async def test_tool_returns_ai_should_speak_false_on_partial():
    """Tool must silence AI even on partial disconnect (human already promoted)."""
    from src.tools.telephony.handoff_to_human import HandoffToHumanTool

    mock_engine = AsyncMock()
    mock_engine.handoff_to_human = AsyncMock(return_value={
        "status": "partial",
        "message": "Partially disconnected.",
        "human_promoted": True,
        "ai_disconnected": True,
        "human_channel_id": "human-ch-1",
        "detached_channels": ["audiosocket-1"],
        "failed_detach_channels": ["local-1"],
        "provider_stopped": True,
    })

    mock_ari = MagicMock()
    mock_ari.engine = mock_engine

    context = MagicMock()
    context.call_id = "call-1"
    context.ari_client = mock_ari

    tool = HandoffToHumanTool()
    result = await tool.execute({}, context)

    assert result["status"] == "partial"
    assert result["ai_should_speak"] is False
    assert result["human_promoted"] is True


@pytest.mark.asyncio
async def test_tool_returns_ai_should_speak_false():
    """The tool must return ai_should_speak=False on success."""
    from src.tools.telephony.handoff_to_human import HandoffToHumanTool

    mock_engine = AsyncMock()
    mock_engine.handoff_to_human = AsyncMock(return_value={
        "status": "success",
        "message": "Handoff complete.",
        "human_promoted": True,
        "ai_disconnected": True,
        "human_channel_id": "human-ch-1",
        "detached_channels": ["audiosocket-1"],
        "provider_stopped": True,
    })

    mock_ari = MagicMock()
    mock_ari.engine = mock_engine

    context = MagicMock()
    context.call_id = "call-1"
    context.ari_client = mock_ari

    tool = HandoffToHumanTool()
    result = await tool.execute({"cleanup_media_channels": True}, context)

    assert result["status"] == "success"
    assert result["ai_should_speak"] is False
    assert result["human_promoted"] is True
    assert result["ai_disconnected"] is True


@pytest.mark.asyncio
async def test_tool_returns_ai_should_speak_true_on_error():
    """The tool must return ai_should_speak=True on failure."""
    from src.tools.telephony.handoff_to_human import HandoffToHumanTool

    mock_engine = AsyncMock()
    mock_engine.handoff_to_human = AsyncMock(return_value={
        "status": "error",
        "message": "No human participant.",
        "human_promoted": False,
        "ai_disconnected": False,
    })

    mock_ari = MagicMock()
    mock_ari.engine = mock_engine

    context = MagicMock()
    context.call_id = "call-1"
    context.ari_client = mock_ari

    tool = HandoffToHumanTool()
    result = await tool.execute({}, context)

    assert result["status"] == "error"
    assert result["ai_should_speak"] is True
    assert result["human_promoted"] is False

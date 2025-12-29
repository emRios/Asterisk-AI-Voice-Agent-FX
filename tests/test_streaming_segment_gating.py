import pytest
from unittest.mock import AsyncMock

from src.core.conversation_coordinator import ConversationCoordinator
from src.core.models import CallSession
from src.core.session_store import SessionStore
from src.core.streaming_playback_manager import StreamingPlaybackManager


class _DummyARI:
    pass


@pytest.mark.asyncio
async def test_end_segment_gating_only_clears_once_with_coordinator(monkeypatch):
    """
    Regression test: end_segment_gating must not clear gating twice when a
    ConversationCoordinator is present.
    """
    session_store = SessionStore()
    call_id = "call-1"
    stream_id = "stream-1"

    await session_store.upsert_call(
        CallSession(call_id=call_id, caller_channel_id="caller-1", provider_name="local")
    )

    coordinator = ConversationCoordinator(session_store)
    await coordinator.on_tts_start(call_id, stream_id)

    original_clear = session_store.clear_gating_token
    mocked_clear = AsyncMock(side_effect=original_clear)
    monkeypatch.setattr(session_store, "clear_gating_token", mocked_clear)

    mgr = StreamingPlaybackManager(
        session_store=session_store,
        ari_client=_DummyARI(),
        conversation_coordinator=coordinator,
        streaming_config={},
    )
    mgr.active_streams[call_id] = {"stream_id": stream_id}

    await mgr.end_segment_gating(call_id)

    assert mocked_clear.await_count == 1


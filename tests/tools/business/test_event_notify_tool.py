"""
Unit tests for CallEventNotification tool.

Covers:
- Parameter validation
- High-level execute() behaviour
- Interaction with Redis backend (mocked client)
"""

import pytest
from unittest.mock import AsyncMock, Mock

from src.tools.business.event_notify import CallEventNotification
from src.tools.context import ToolExecutionContext


class FakeRedisClient:
    """Simple async fake for Redis client used in tests."""

    def __init__(self, xadd_result: str = "123-0") -> None:
        self.xadd_calls = []
        self._xadd_result = xadd_result

    async def xadd(self, stream_name, payload, maxlen=None, approximate=None, id=None):
        """Mimic Redis xadd, recording calls for assertions."""
        self.xadd_calls.append(
            {
                "stream_name": stream_name,
                "payload": payload,
                "maxlen": maxlen,
                "approximate": approximate,
                "id": id,
            }
        )
        return self._xadd_result


@pytest.fixture
def call_event_tool() -> CallEventNotification:
    """Create CallEventNotification tool instance."""
    return CallEventNotification()


@pytest.fixture
def base_tool_config():
    """
    Minimal tool config similar to ai-agent.yaml structure for this tool.

    This mirrors:

    tools:
      CallEventNotification:
        queue_backend: redis
        redis:
          url: ...
          stream_name: call_events
          max_stream_length: 10000
        enabled_event_types:
          - PURCHASE_INTENT_HIGH
          ...
    """
    return {
        "tools": {
            "CallEventNotification": {
                "queue_backend": "redis",
                "redis": {
                    "url": "redis://127.0.0.1:6379/0",
                    "stream_name": "call_events",
                    "max_stream_length": 10000,
                },
                "enabled_event_types": [
                    "PURCHASE_INTENT_HIGH",
                    "TRANSFER_REQUESTED",
                    "HARD_REJECTION",
                    "SOFT_REJECTION",
                    "ESCALATION_REQUIRED",
                    "POSITIVE_FEEDBACK",
                    "NEGATIVE_FEEDBACK",
                ],
            }
        }
    }


@pytest.fixture
def basic_context(base_tool_config) -> ToolExecutionContext:
    """
    Context-like object with the minimal API the tool expects.

    We use a Mock with ToolExecutionContext as spec so attribute names are
    checked, but we freely attach the extra methods the tool calls:
    - get_config()
    - get_redis_client()
    """
    context = Mock(spec=ToolExecutionContext)
    context.call_id = "test-call-123"
    context.provider_name = "deepgram"
    context.agent_id = "agent-001"
    context.logger = Mock()

    # get_config should return the root config dict (including "tools").
    context.get_config = Mock(return_value=base_tool_config)

    # No session store by default for these tests.
    context.session_store = None

    # Redis client is injected per-test when needed.
    context.get_redis_client = AsyncMock()

    return context


class TestCallEventNotificationDefinition:
    """Definition-level tests."""

    def test_definition_basic(self, call_event_tool):
        definition = call_event_tool.definition

        assert definition.name == "CallEventNotification"
        # Business tool category
        assert definition.category.value == "business"
        assert "Notifica eventos críticos" in definition.description

        # event_type must be a required parameter
        event_type_param = next(p for p in definition.parameters if p.name == "event_type")
        assert event_type_param.required is True


class TestCallEventNotificationValidation:
    """Tests for validate_parameters custom logic."""

    def test_validate_parameters_valid_purchase_intent(self, call_event_tool):
        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 95,
            "priority": "high",
        }

        assert call_event_tool.validate_parameters(params) is True

    def test_validate_parameters_missing_intent_score(self, call_event_tool):
        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            # intent_score missing
            "priority": "medium",
        }

        with pytest.raises(ValueError) as exc:
            call_event_tool.validate_parameters(params)

        assert "intent_score es obligatorio" in str(exc.value)

    def test_validate_parameters_intent_score_out_of_range(self, call_event_tool):
        params_low = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": -1,
        }
        params_high = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 101,
        }

        with pytest.raises(ValueError):
            call_event_tool.validate_parameters(params_low)
        with pytest.raises(ValueError):
            call_event_tool.validate_parameters(params_high)

    def test_validate_parameters_invalid_priority(self, call_event_tool):
        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 80,
            "priority": "urgent",  # not in allowed enum
        }

        with pytest.raises(ValueError) as exc:
            call_event_tool.validate_parameters(params)

        assert "priority inválida" in str(exc.value)


class TestCallEventNotificationExecute:
    """High-level execute() behaviour tests (with mocked Redis)."""

    @pytest.mark.asyncio
    async def test_execute_ignored_when_event_type_not_enabled(
        self, call_event_tool, basic_context, base_tool_config
    ):
        # Remove PURCHASE_INTENT_HIGH from enabled types to force ignore.
        base_tool_config["tools"]["CallEventNotification"]["enabled_event_types"] = [
            "HARD_REJECTION"
        ]

        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 90,
        }

        result = await call_event_tool.execute(params, basic_context)

        assert result["status"] == "ignored"
        assert "no habilitado" in result["message"]

    @pytest.mark.asyncio
    async def test_execute_ignored_when_backend_none(
        self, call_event_tool, basic_context, base_tool_config
    ):
        base_tool_config["tools"]["CallEventNotification"]["queue_backend"] = "none"

        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 90,
        }

        result = await call_event_tool.execute(params, basic_context)

        assert result["status"] == "ignored"
        assert "Backend deshabilitado" in result["message"]

    @pytest.mark.asyncio
    async def test_execute_success_with_redis_backend(
        self, call_event_tool, basic_context, base_tool_config
    ):
        # Ensure backend is redis and event type is enabled
        base_tool_config["tools"]["CallEventNotification"]["queue_backend"] = "redis"
        base_tool_config["tools"]["CallEventNotification"]["enabled_event_types"] = [
            "PURCHASE_INTENT_HIGH"
        ]

        fake_client = FakeRedisClient(xadd_result="1699999999-0")
        basic_context.get_redis_client.return_value = fake_client

        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 90,
            "product_name": "Seguro de vida",
            "product_id": "SEG-123",
            "notes": "Cliente muy interesado",
            "priority": "high",
        }

        result = await call_event_tool.execute(params, basic_context)

        assert result["status"] == "success"
        assert result["backend"] == "redis"
        assert result["queue_message_id"] == "1699999999-0"
        assert "event_id" in result

        # A message should have been sent to the configured stream
        assert len(fake_client.xadd_calls) == 1
        call = fake_client.xadd_calls[0]
        assert call["stream_name"] == "call_events"
        # event_id is reused as Redis Stream ID
        assert call["id"] == result["event_id"]

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_validation_failure(
        self, call_event_tool, basic_context, base_tool_config
    ):
        # queue_backend still redis, but parameters are invalid
        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            # Missing intent_score
        }

        result = await call_event_tool.execute(params, basic_context)

        assert result["status"] == "error"
        assert "Validación" in result["message"]

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_unknown_backend(
        self, call_event_tool, basic_context, base_tool_config
    ):
        base_tool_config["tools"]["CallEventNotification"]["queue_backend"] = "kafka"

        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 90,
        }

        result = await call_event_tool.execute(params, basic_context)

        assert result["status"] == "error"
        assert "Backend desconocido" in result["message"]
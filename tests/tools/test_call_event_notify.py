import pytest
from types import SimpleNamespace

from src.tools.business.event_notify import CallEventNotification


class TestCallEventNotificationValidation:
    def setup_method(self):
        self.tool = CallEventNotification()

    def test_validate_parameters_missing_intent_score_for_high_intent(self):
        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "priority": "high",
        }
        with pytest.raises(ValueError) as exc:
            self.tool.validate_parameters(params)
        assert "intent_score" in str(exc.value).lower()

    def test_validate_parameters_intent_score_out_of_range(self):
        params_low = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": -1,
            "priority": "high",
        }
        params_high = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 101,
            "priority": "high",
        }
        with pytest.raises(ValueError):
            self.tool.validate_parameters(params_low)
        with pytest.raises(ValueError):
            self.tool.validate_parameters(params_high)

    def test_validate_parameters_invalid_priority(self):
        params = {
            "event_type": "TRANSFER_REQUESTED",
            "priority": "invalid",
        }
        with pytest.raises(ValueError) as exc:
            self.tool.validate_parameters(params)
        assert "priority" in str(exc.value).lower()

    def test_validate_parameters_requires_extracted_data_for_data_extraction(self):
        params = {
            "event_type": "DATA_EXTRACTION",
            "priority": "medium",
        }
        with pytest.raises(ValueError) as exc:
            self.tool.validate_parameters(params)
        assert "extracted_data" in str(exc.value).lower()

    def test_validate_parameters_rejects_non_object_extracted_data(self):
        params = {
            "event_type": "DATA_EXTRACTION",
            "extracted_data": ["invalid"],
            "priority": "medium",
        }
        with pytest.raises(ValueError) as exc:
            self.tool.validate_parameters(params)
        assert "extracted_data" in str(exc.value).lower()


class TestCallEventNotificationInternals:
    def setup_method(self):
        self.tool = CallEventNotification()

    def test_generate_event_id_deterministic_with_frozen_time(self, freeze_utcnow):
        eid1 = self.tool._generate_event_id("call-123", "PURCHASE_INTENT_HIGH")
        eid2 = self.tool._generate_event_id("call-123", "PURCHASE_INTENT_HIGH")
        assert eid1 == eid2
        assert eid1.startswith("evt_")
        assert len(eid1) == 4 + 12

    def test_build_payload_includes_session_fields_and_new_data_extraction_fields(
        self, tool_execution_context_stub, freeze_utcnow
    ):
        session = SimpleNamespace(caller_id="5551234", turn_index=7)
        params = {
            "event_type": "DATA_EXTRACTION",
            "product_name": None,
            "notes": "Testing",
            "event_description": "Se extrajeron respuestas de cierre",
            "extracted_data": {"p1": ["Regular"], "p2": ["Bueno"]},
        }

        payload = self.tool._build_payload(params, tool_execution_context_stub, session)

        assert payload["call_id"] == tool_execution_context_stub.call_id
        assert payload["event_type"] == "DATA_EXTRACTION"
        assert payload["notes"] == "Testing"
        assert payload["event_description"] == "Se extrajeron respuestas de cierre"
        assert payload["extracted_data"] == {"p1": ["Regular"], "p2": ["Bueno"]}
        assert "product_name" not in payload
        assert payload["caller_id"] == "5551234"
        assert payload["metadata"]["provider"] == "unittest"
        assert payload["metadata"]["conversation_step"] == 7
        assert "timestamp" in payload

    def test_build_payload_omits_optional_event_description_when_not_provided(
        self, tool_execution_context_stub, freeze_utcnow
    ):
        session = SimpleNamespace(caller_id="5551234", turn_index=7)
        params = {
            "event_type": "TRANSFER_REQUESTED",
            "notes": "Testing",
        }

        payload = self.tool._build_payload(params, tool_execution_context_stub, session)

        assert payload["notes"] == "Testing"
        assert "event_description" not in payload
        assert "extracted_data" not in payload


class TestCallEventNotificationRedis:
    @pytest.mark.asyncio
    async def test_publish_to_redis_success(
        self, tool_execution_context_stub, fake_redis, monkeypatch
    ):
        tool = CallEventNotification()
        fake_redis._xadd_result = "1720000000-1"

        params = {"event_type": "TRANSFER_REQUESTED"}
        payload = tool._build_payload(params, tool_execution_context_stub, None)
        tool_config = tool_execution_context_stub.get_config_value(
            "tools.CallEventNotification", {}
        )

        monkeypatch.setattr(tool, "_get_redis_client", lambda redis_url, ctx: fake_redis)
        msg_id = await tool._publish_to_redis(
            payload, tool_execution_context_stub, tool_config
        )

        assert msg_id == "1720000000-1"
        assert fake_redis.last_xadd["stream"] == "call_events"
        assert fake_redis.last_xadd["id"] == payload["event_id"]

    @pytest.mark.asyncio
    async def test_publish_to_redis_duplicate_returns_duplicate(
        self, tool_execution_context_stub, fake_redis, monkeypatch
    ):
        tool = CallEventNotification()
        fake_redis.raise_duplicate = True

        params = {"event_type": "TRANSFER_REQUESTED"}
        payload = tool._build_payload(params, tool_execution_context_stub, None)
        tool_config = tool_execution_context_stub.get_config_value(
            "tools.CallEventNotification", {}
        )

        monkeypatch.setattr(tool, "_get_redis_client", lambda redis_url, ctx: fake_redis)
        msg_id = await tool._publish_to_redis(
            payload, tool_execution_context_stub, tool_config
        )

        assert msg_id == "DUPLICATE"


class TestCallEventNotificationExecute:
    @pytest.mark.asyncio
    async def test_execute_ignored_when_backend_none(self, tool_execution_context_stub):
        tool = CallEventNotification()

        cfg = {
            "tools": {
                "CallEventNotification": {
                    "queue_backend": "none",
                    "enabled_event_types": [
                        "PURCHASE_INTENT_HIGH",
                        "TRANSFER_REQUESTED",
                        "DATA_EXTRACTION",
                    ],
                }
            }
        }
        tool_execution_context_stub.config = cfg

        res = await tool.execute(
            {"event_type": "TRANSFER_REQUESTED"}, tool_execution_context_stub
        )
        assert res["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_execute_ignored_when_event_type_not_enabled(
        self, tool_execution_context_stub
    ):
        tool = CallEventNotification()
        cfg = {
            "tools": {
                "CallEventNotification": {
                    "queue_backend": "redis",
                    "enabled_event_types": ["HARD_REJECTION"],
                    "redis": {"stream_name": "call_events", "max_stream_length": 10000},
                }
            }
        }
        tool_execution_context_stub.config = cfg

        res = await tool.execute(
            {"event_type": "TRANSFER_REQUESTED"}, tool_execution_context_stub
        )
        assert res["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_execute_success_with_redis_backend(
        self, tool_execution_context_stub, monkeypatch
    ):
        tool = CallEventNotification()

        async def fake_publish(payload, ctx, tool_config):
            return "1720000000-42"

        monkeypatch.setattr(tool, "_publish_to_redis", fake_publish)

        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            "intent_score": 90,
            "priority": "critical",
            "notes": "VIP lead",
        }
        res = await tool.execute(params, tool_execution_context_stub)
        assert res["status"] == "success"
        assert res["queue_message_id"] == "1720000000-42"
        assert "event_id" in res

    @pytest.mark.asyncio
    async def test_execute_success_with_data_extraction_event(
        self, tool_execution_context_stub, monkeypatch
    ):
        tool = CallEventNotification()

        async def fake_publish(payload, ctx, tool_config):
            assert payload["event_type"] == "DATA_EXTRACTION"
            assert payload["event_description"] == "Extracción de respuestas"
            assert payload["extracted_data"] == {"p1": ["Regular"], "p2": ["Bueno"]}
            return "1720000000-99"

        monkeypatch.setattr(tool, "_publish_to_redis", fake_publish)

        params = {
            "event_type": "DATA_EXTRACTION",
            "notes": "Extracción completada",
            "event_description": "Extracción de respuestas",
            "extracted_data": {"p1": ["Regular"], "p2": ["Bueno"]},
            "priority": "medium",
        }

        res = await tool.execute(params, tool_execution_context_stub)
        assert res["status"] == "success"
        assert res["queue_message_id"] == "1720000000-99"

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_validation_failure(
        self, tool_execution_context_stub
    ):
        tool = CallEventNotification()
        res = await tool.execute(
            {"event_type": "PURCHASE_INTENT_HIGH"}, tool_execution_context_stub
        )
        assert res["status"] == "error"
        assert "intent_score" in (res.get("message") or "").lower()

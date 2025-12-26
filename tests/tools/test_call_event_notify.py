import pytest
from types import SimpleNamespace

from src.tools.business.event_notify import CallEventNotification


class TestCallEventNotificationValidation:
    def setup_method(self):
        self.tool = CallEventNotification()

    def test_validate_parameters_missing_intent_score_for_high_intent(self):
        params = {
            "event_type": "PURCHASE_INTENT_HIGH",
            # "intent_score" missing on purpose
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


class TestCallEventNotificationInternals:
    def setup_method(self):
        self.tool = CallEventNotification()

    def test_generate_event_id_deterministic_with_frozen_time(self, freeze_utcnow):
        """
        _generate_event_id debe ser determinista con utcnow fijo y call_id/event_type fijos.
        """
        eid1 = self.tool._generate_event_id("call-123", "PURCHASE_INTENT_HIGH")
        eid2 = self.tool._generate_event_id("call-123", "PURCHASE_INTENT_HIGH")
        assert eid1 == eid2
        assert eid1.startswith("evt_")
        assert len(eid1) == 4 + 12  # evt_ + 12 hex

    def test_build_payload_includes_session_fields_and_strips_none(self, tool_execution_context_stub, freeze_utcnow):
        """
        _build_payload:
        - Incluye caller_id y conversation_step si hay sesión
        - Elimina claves con valor None al nivel superior
        """
        # Inyectar una session con caller_id y turn_index
        session = SimpleNamespace(caller_id="5551234", turn_index=7)
        class SessionStore:
            def get_session(self, call_id):
                assert call_id == tool_execution_context_stub.call_id
                return session
        tool_execution_context_stub.session_store = SessionStore()

        params = {
            "event_type": "TRANSFER_REQUESTED",
            "product_name": None,  # será eliminado
            "notes": "Testing",
            # intent_score ausente (no requerido para TRANSFER_REQUESTED)
        }
        payload = self.tool._build_payload(params, tool_execution_context_stub)

        # Chequeos
        assert payload["call_id"] == tool_execution_context_stub.call_id
        assert payload["event_type"] == "TRANSFER_REQUESTED"
        assert payload["notes"] == "Testing"
        assert "product_name" not in payload  # None eliminado
        assert payload["caller_id"] == "5551234"
        assert "metadata" in payload
        assert payload["metadata"]["provider"] == "unittest"
        assert payload["metadata"]["conversation_step"] == 7
        # timestamp existe (valor fijo por freeze_utcnow)
        assert "timestamp" in payload


class TestCallEventNotificationRedis:
    @pytest.mark.asyncio
    async def test_publish_to_redis_success(self, tool_execution_context_stub, fake_redis):
        tool = CallEventNotification()
        # Config backend redis ya está en tool_execution_context_stub.get_config()

        # Asegurar fake redis retorna un id concreto
        fake_redis._xadd_result = "1720000000-1"

        # make context.get_redis_client return our fake_redis
        async def _get_client():
            return fake_redis
        tool_execution_context_stub.get_redis_client = _get_client

        params = {"event_type": "TRANSFER_REQUESTED"}
        payload = tool._build_payload(params, tool_execution_context_stub)
        msg_id = await tool._publish_to_redis(payload, tool_execution_context_stub)
        assert msg_id == "1720000000-1"

        # Verifica que se invocó xadd con nuestro event_id
        assert fake_redis.last_xadd["stream"] == "call_events"
        assert fake_redis.last_xadd["id"] == payload["event_id"]

    @pytest.mark.asyncio
    async def test_publish_to_redis_duplicate_returns_DUPLICATE(self, tool_execution_context_stub, fake_redis):
        tool = CallEventNotification()

        # Simular duplicado
        fake_redis.raise_duplicate = True

        async def _get_client():
            return fake_redis
        tool_execution_context_stub.get_redis_client = _get_client

        params = {"event_type": "TRANSFER_REQUESTED"}
        payload = tool._build_payload(params, tool_execution_context_stub)
        msg_id = await tool._publish_to_redis(payload, tool_execution_context_stub)
        assert msg_id == "DUPLICATE"


class TestCallEventNotificationExecute:
    @pytest.mark.asyncio
    async def test_execute_ignored_when_backend_none(self, tool_execution_context_stub, monkeypatch):
        """
        Si backend=none debe ignorar y retornar status="ignored".
        """
        tool = CallEventNotification()

        # Modificar configuración para el test
        base = tool_execution_context_stub.get_config()
        cfg = {
            **base,
            "tools": {
                "CallEventNotification": {
                    "queue_backend": "none",
                    "enabled_event_types": ["PURCHASE_INTENT_HIGH", "TRANSFER_REQUESTED"],
                }
            }
        }
        tool_execution_context_stub.get_config = lambda: cfg

        res = await tool.execute({"event_type": "TRANSFER_REQUESTED"}, tool_execution_context_stub)
        assert res["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_execute_ignored_when_event_type_not_enabled(self, tool_execution_context_stub):
        tool = CallEventNotification()
        base = tool_execution_context_stub.get_config()
        cfg = {
            **base,
            "tools": {
                "CallEventNotification": {
                    "queue_backend": "redis",
                    "enabled_event_types": ["HARD_REJECTION"],  # no incluye TRANSFER_REQUESTED
                    "redis": {"stream_name": "call_events", "max_stream_length": 10000},
                }
            }
        }
        tool_execution_context_stub.get_config = lambda: cfg

        res = await tool.execute({"event_type": "TRANSFER_REQUESTED"}, tool_execution_context_stub)
        assert res["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_execute_success_with_redis_backend(self, tool_execution_context_stub, fake_redis, monkeypatch):
        tool = CallEventNotification()

        # Forzar que _publish_to_redis regrese un ID fijo sin tocar Redis real
        async def fake_publish(payload, ctx):
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
    async def test_execute_returns_error_on_validation_failure(self, tool_execution_context_stub):
        tool = CallEventNotification()
        # Faltan campos requeridos para PURCHASE_INTENT_HIGH (intent_score)
        res = await tool.execute({"event_type": "PURCHASE_INTENT_HIGH"}, tool_execution_context_stub)
        assert res["status"] == "error"
        assert "intent_score" in (res.get("message") or "").lower()
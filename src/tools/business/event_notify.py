from __future__ import annotations

from datetime import datetime
from enum import Enum
import hashlib
from uuid import uuid4
from typing import Any, Dict, Optional

import redis.asyncio as redis
from redis.exceptions import ResponseError
from pydantic import BaseModel, Field, validator

from src.tools.base import Tool, ToolCategory, ToolDefinition, ToolParameter
from src.tools.context import ToolExecutionContext


class QueueBackend(str, Enum):
    """Enumeration of queue backends."""
    REDIS = "redis"
    RABBITMQ = "rabbitmq"


class EventType(str, Enum):
    """Enumeration of event types."""
    PURCHASE_INTENT_HIGH = "PURCHASE_INTENT_HIGH"
    TRANFER_REQUESTED = "TRANSFER_REQUESTED"
    HARD_REJECTION = "HARD_REJECTION"
    SOFT_REJECTION = "SOFT_REJECTION"
    ESCALATION_REQURIDED = "ESCALATION_REQUIRED"
    SATISFACTION_HIGH = "SATISFACTION_HIGH"
    POSITIVE_FEEDBACK = "POSITIVE_FEEDBACK"
    NEGATIVE_FEEDBACK = "NEGATIVE_FEEDBACK"


class Priority(str, Enum):
    """Enumeration of event priorities."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CallEventPayload(BaseModel):
    """
    Modelo legado: NO se usa para publicar a Redis, pero se deja por compatibilidad.
    Nota: intent_score aquí está 0..1, pero el tool valida 0..100.
    Si planeas usar este modelo en el futuro, alinéalo.
    """
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    call_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    event_type: EventType
    intent_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    product_name: Optional[str]
    customer_id: Optional[str]
    notes: Optional[str] = Field(None, max_length=500)
    agent_id: Optional[str] = None
    priority: Priority = Priority.MEDIUM
    source: str = "AI Engine"

    @validator("intent_score")
    def validate_intent_score(cls, v, values):
        if values.get("event_type") == EventType.PURCHASE_INTENT_HIGH and v is None:
            raise ValueError("Intent score is required for PURCHASE_INTENT_HIGH events")
        return v


class CallEventNotification(Tool):
    """
    Tool que publica eventos críticos a una cola (Redis Streams).
    Diseño: el tool es autosuficiente para Redis.
    - Obtiene config con context.get_config_value()
    - Extrae URL con dict.get()
    - Mantiene su propio pool/cache por URL
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Cache por URL => cliente Redis (pool interno de redis-py).
        self._redis_clients: Dict[str, redis.Redis] = {}

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="CallEventNotification",
            description=(
                "Notifica eventos críticos de negocio durante la llamada a un sistema de colas. "
                "Úsala SOLO cuando detectes eventos con impacto real: alta intención de compra, "
                "rechazo definitivo, solicitud de transferencia, escalaciones. No la llames por eventos triviales."
            ),
            category=ToolCategory.BUSINESS,
            parameters=[
                ToolParameter(
                    name="event_type",
                    type="string",
                    required=True,
                    enum=[
                        "PURCHASE_INTENT_HIGH", "TRANSFER_REQUESTED",
                        "HARD_REJECTION", "SOFT_REJECTION",
                        "ESCALATION_REQUIRED", "POSITIVE_FEEDBACK"
                    ],
                    description="Tipo de evento detectado."
                ),
                ToolParameter(
                    name="intent_score",
                    type="number",
                    required=False,
                    description="Score de intención (0-100). Requerido para PURCHASE_INTENT_HIGH."
                ),
                ToolParameter(
                    name="product_name",
                    type="string",
                    required=False,
                    description="Nombre del producto/servicio involucrado."
                ),
                ToolParameter(
                    name="product_id",
                    type="string",
                    required=False,
                    description="ID del producto/servicio."
                ),
                ToolParameter(
                    name="notes",
                    type="string",
                    required=False,
                    description="Justificación breve (1-2 frases)."
                ),
                ToolParameter(
                    name="priority",
                    type="string",
                    required=False,
                    enum=["low", "medium", "high", "critical"],
                    description="Prioridad del evento. Default: medium"
                )
            ],
            requires_channel=False,
            max_execution_time=10.0
        )

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        """
        Validación custom con lógica de negocio.
        """
        # Validación base (required + enum)
        super().validate_parameters(parameters)

        event_type = parameters.get("event_type")
        intent_score = parameters.get("intent_score")

        if event_type == "PURCHASE_INTENT_HIGH" and intent_score is None:
            raise ValueError("intent_score es obligatorio cuando event_type=PURCHASE_INTENT_HIGH")

        if intent_score is not None and not (0 <= intent_score <= 100):
            raise ValueError("intent_score debe estar entre 0 y 100")

        priority = parameters.get("priority", "medium")
        if priority not in ["low", "medium", "high", "critical"]:
            raise ValueError(f"priority inválida: {priority}")

        return True

    def _generate_event_id(self, call_id: str, event_type: str) -> str:
        """ID determinista para idempotencia."""
        timestamp_prefix = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        content = f"{call_id}:{event_type}:{timestamp_prefix}"
        return f"evt_{hashlib.md5(content.encode()).hexdigest()[:12]}"

    def _build_payload(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext
    ) -> Dict[str, Any]:
        """Construye payload canónico."""
        session = None
        if getattr(context, "session_store", None):
            try:
                session = context.session_store.get_session(context.call_id)
            except Exception:
                if getattr(context, "logger", None):
                    context.logger.warning("No se pudo cargar sesión para enriquecer payload")

        payload = {
            "event_id": self._generate_event_id(context.call_id, parameters["event_type"]),
            "call_id": context.call_id,
            "caller_id": getattr(session, "caller_id", None),
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": parameters["event_type"],
            "intent_score": parameters.get("intent_score"),
            "product_name": parameters.get("product_name"),
            "product_id": parameters.get("product_id"),
            "notes": parameters.get("notes"),
            "priority": parameters.get("priority", "medium"),
            "agent_id": getattr(context, "agent_id", None),
            "metadata": {
                "provider": getattr(context, "provider_name", None),
                "conversation_step": getattr(session, "turn_index", None) if session else None
            }
        }

        # Limpiar None values
        return {k: v for k, v in payload.items() if v is not None}

    def _get_redis_client(self, redis_url: str, context: ToolExecutionContext) -> redis.Redis:
        """
        Manager local del tool: recibe URL y devuelve cliente cacheado.
        No lee config, no decide defaults (eso ocurre antes).
        """
        client = self._redis_clients.get(redis_url)
        if client is not None:
            return client

        client = redis.from_url(redis_url, decode_responses=True)
        self._redis_clients[redis_url] = client

        if getattr(context, "logger", None):
            context.logger.debug("Redis client inicializado", redis_url=redis_url)

        return client

    async def _publish_to_redis(
        self,
        payload: Dict[str, Any],
        context: ToolExecutionContext
    ) -> str:
        """Publica a Redis Streams con idempotencia."""
        tool_config = context.get_config_value(f"tools.{self.definition.name}", {}) or {}

        redis_url = (
            tool_config.get("redis", {}).get("url")
            or "redis://localhost:6379/0"
        )
        stream_name = (
            tool_config.get("redis", {}).get("stream_name")
            or "call_events"
        )
        max_len = (
            tool_config.get("redis", {}).get("max_stream_length")
            or 10000
        )

        client = self._get_redis_client(redis_url, context)

        try:
            msg_id = await client.xadd(
                stream_name,
                payload,
                maxlen=max_len,
                approximate=True,
                id=payload["event_id"]
            )
            return str(msg_id)

        except ResponseError as e:
            # Redis suele devolver: "The ID specified in XADD is equal or smaller..."
            # No siempre contiene "duplicate", así que tratamos cualquier error de ID como idempotencia.
            if "ID" in str(e).upper():
                if getattr(context, "logger", None):
                    context.logger.info("Evento duplicado ignorado", event_id=payload["event_id"])
                return "DUPLICATE"
            raise

    async def _publish_to_rabbitmq(self, payload: Dict[str, Any], context: ToolExecutionContext) -> str:
        """Placeholder para RabbitMQ (implementar si se necesita)."""
        raise NotImplementedError("RabbitMQ backend not yet implemented")

    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext
    ) -> Dict[str, Any]:
        """
        Ejecución stateless desde perspectiva de negocio.
        (El único estado local es cache de clientes Redis por URL.)
        """
        try:
            # 1) Validar parámetros
            self.validate_parameters(parameters)

            # 2) Cargar config del tool (desde context.get_config_value)
            config = context.get_config_value(f"tools.{self.definition.name}", {}) or {}
            backend = config.get("queue_backend", "none")

            # 3) Filtrar eventos no habilitados (si aplica)
            enabled_types = config.get("enabled_event_types")
            if enabled_types and parameters["event_type"] not in enabled_types:
                msg = f"Evento {parameters['event_type']} no habilitado"
                if getattr(context, "logger", None):
                    context.logger.info(msg)
                return {"status": "ignored", "message": msg}

            # 4) Backend deshabilitado
            if backend == "none":
                if getattr(context, "logger", None):
                    context.logger.warning("Queue backend deshabilitado")
                return {"status": "ignored", "message": "Backend deshabilitado"}

            # 5) Construir payload
            payload = self._build_payload(parameters, context)

            # 6) Publicar según backend
            message_id: Optional[str] = None
            if backend == "redis":
                message_id = await self._publish_to_redis(payload, context)
            elif backend == "rabbitmq":
                message_id = await self._publish_to_rabbitmq(payload, context)
            else:
                raise ValueError(f"Backend desconocido: {backend}")

            # 7) Éxito
            if getattr(context, "logger", None):
                context.logger.info(
                    "Evento publicado",
                    event_type=payload.get("event_type"),
                    event_id=payload.get("event_id"),
                    backend=backend
                )

            return {
                "status": "success",
                "message": f"Evento {payload['event_type']} publicado",
                "event_id": payload["event_id"],
                "queue_message_id": message_id,
                "backend": backend
            }

        except ValueError as e:
            if getattr(context, "logger", None):
                context.logger.warning("Validación fallida", error=str(e), parameters=parameters)
            return {"status": "error", "message": f"Validación: {str(e)}"}

        except Exception as e:
            if getattr(context, "logger", None):
                context.logger.error("Error ejecutando tool", error=str(e), exc_info=True, parameters=parameters)
            return {
                "status": "error",
                "message": f"Falló ejecución: {str(e)}",
                "error_type": type(e).__name__
            }

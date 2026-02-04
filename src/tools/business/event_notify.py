# src/tools/business/event_notify.py

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from uuid import uuid4

from pydantic import BaseModel, Field, validator

from src.tools.base import Tool, ToolCategory, ToolDefinition, ToolParameter
from src.tools.context import ToolExecutionContext


@dataclass
class _HttpResponse:
    status: int
    text: str


class QueueBackend(str, Enum):
    """Enumeration of queue backends."""

    REDIS = "redis"
    RABBITMQ = "rabbitmq"
    HTTP = "http"
    HTTPS = "https"  # <-- agregar


class EventType(str, Enum):
    """Enumeration of event types."""

    PURCHASE_INTENT_HIGH = "PURCHASE_INTENT_HIGH"
    TRANSFER_REQUESTED = "TRANSFER_REQUESTED"
    HARD_REJECTION = "HARD_REJECTION"
    SOFT_REJECTION = "SOFT_REJECTION"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    POSITIVE_FEEDBACK = "POSITIVE_FEEDBACK"
    NEGATIVE_FEEDBACK = "NEGATIVE_FEEDBACK"


class Priority(str, Enum):
    """Enumeration of event priorities."""
    CRITICAL = "CRITICAL",
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
    intent_score: Optional[float] = Field(None, ge=0.0, le=100.0)
    product_name: Optional[str] = None
    customer_id: Optional[str] = None
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
        self._redis_clients: Dict[str, Any] = {}

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
                        "PURCHASE_INTENT_HIGH",
                        "TRANSFER_REQUESTED",
                        "HARD_REJECTION",
                        "SOFT_REJECTION",
                        "ESCALATION_REQUIRED",
                        "POSITIVE_FEEDBACK",
                        "NEGATIVE_FEEDBACK",
                    ],
                    description="Tipo de evento detectado.",
                ),
                ToolParameter(
                    name="intent_score",
                    type="number",
                    required=False,
                    description="Score de intención (0-100). Requerido para PURCHASE_INTENT_HIGH.",
                ),
                ToolParameter(
                    name="product_name",
                    type="string",
                    required=False,
                    description="Nombre del producto/servicio involucrado.",
                ),
                ToolParameter(
                    name="product_id",
                    type="string",
                    required=False,
                    description="ID del producto/servicio.",
                ),
                ToolParameter(
                    name="notes",
                    type="string",
                    required=False,
                    description="Justificación breve (1-2 frases).",
                ),
                ToolParameter(
                    name="priority",
                    type="string",
                    required=False,
                    enum=["low", "medium", "high", "critical"],
                    description="Prioridad del evento. Default: medium",
                ),
            ],
            requires_channel=False,
            max_execution_time=10.0,
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
            raise ValueError(
                "intent_score es obligatorio cuando event_type=PURCHASE_INTENT_HIGH"
            )

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
        self, parameters: Dict[str, Any], context: ToolExecutionContext
    ) -> Dict[str, Any]:
        """Construye payload canónico."""
        session = None
        if getattr(context, "session_store", None):
            try:
                session = context.session_store.get_session(context.call_id)
            except Exception:
                if getattr(context, "logger", None):
                    context.logger.warning(
                        "No se pudo cargar sesión para enriquecer payload"
                    )

        payload = {
            "event_id": self._generate_event_id(
                context.call_id, parameters["event_type"]
            ),
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
                "conversation_step": (
                    getattr(session, "turn_index", None) if session else None
                ),
            },
        }

        # --- Enrich metadata for AMI handoff / conference ---
        try:
            # Prefer explicit attributes if you added them to session, else fallback to channel_vars
            ami_channel_name = None
            channel_name = None
            ari_channel_id = context.call_id
            conf_id = context.call_id

            if session:
                # 1) Direct attribute hydration (based on your log keys)
                ami_channel_name = getattr(session, "channel_name", None) or getattr(
                    session, "ami_channel_name", None
                )

                # 2) Fallback to channel_vars if you stored it there
                chvars = getattr(session, "channel_vars", None)
                if isinstance(chvars, dict):
                    ami_channel_name = ami_channel_name or chvars.get(
                        "AMI_CHANNEL_NAME"
                    )
                    # Prefer ARI_CHANNEL_NAME and fall back to AMI_CHANNEL_NAME
                    channel_name = chvars.get("ARI_CHANNEL_NAME") or chvars.get(
                        "AMI_CHANNEL_NAME"
                    ) or channel_name
                    conf_id = chvars.get("CONF_ID") or conf_id
                    ari_channel_id = chvars.get("ARI_CHANNEL_ID") or ari_channel_id

            payload["metadata"]["ami_channel_name"] = ami_channel_name
            payload["metadata"]["conf_id"] = conf_id
            payload["metadata"]["ari_channel_id"] = ari_channel_id
            # Expose the resolved channel_name (e.g., PJSIP/...) at the top level
            payload["channel_name"] = channel_name

            # Optional: include bridge_id if you want better observability in the dialer
            if session and getattr(session, "bridge_id", None):
                payload["metadata"]["bridge_id"] = getattr(session, "bridge_id")

            # Optional: allow caller to specify which dialplan contexts / agent endpoint to use
            if parameters.get("agent_endpoint"):
                payload["metadata"]["agent_endpoint"] = parameters["agent_endpoint"]
            if parameters.get("customer_conf_context"):
                payload["metadata"]["customer_conf_context"] = parameters[
                    "customer_conf_context"
                ]
            if parameters.get("agent_conf_context"):
                payload["metadata"]["agent_conf_context"] = parameters[
                    "agent_conf_context"
                ]

        except Exception:
            if getattr(context, "logger", None):
                context.logger.warning(
                    "No se pudo enriquecer metadata AMI/CONF", exc_info=True
                )
        # --- END ---

        # Limpiar None values (top-level + metadata)
        payload = {k: v for k, v in payload.items() if v is not None}
        if isinstance(payload.get("metadata"), dict):
            payload["metadata"] = {
                k: v for k, v in payload["metadata"].items() if v is not None
            }
        return payload

    def _get_tool_config(self, context: ToolExecutionContext) -> Dict[str, Any]:
        gcv = getattr(context, "get_config_value", None)
        if not callable(gcv):
            raise RuntimeError("ToolExecutionContext no expone get_config_value() callable")
        return gcv(f"tools.{self.definition.name}", {}) or {}


    def _get_redis_client(self, redis_url: str, context: ToolExecutionContext):
        """
        Manager local del tool: recibe URL y devuelve cliente cacheado.
        No lee config, no decide defaults (eso ocurre antes).
        """
        client = self._redis_clients.get(redis_url)
        if client is not None:
            return client

        # Lazy import: solo si realmente usas backend=redis.
        import redis.asyncio as redis_asyncio  # type: ignore

        client = redis_asyncio.from_url(redis_url, decode_responses=True)
        self._redis_clients[redis_url] = client

        if getattr(context, "logger", None):
            context.logger.debug("Redis client inicializado", redis_url=redis_url)

        return client

    async def _publish_to_redis(
        self,
        payload: Dict[str, Any],
        context: ToolExecutionContext,
        tool_config: Dict[str, Any],
    ) -> str:
        """
        Publica a Redis Streams con idempotencia.
        NOTA: requiere redis-py instalado, pero solo se importa si backend=redis.
        """
        redis_cfg = tool_config.get("redis", {}) or {}

        stream_name = redis_cfg.get("stream_name", "call_events")
        max_len = int(redis_cfg.get("max_stream_length", 10000))
        redis_url = redis_cfg.get("url") or "redis://127.0.0.1:6379/0"

        client = self._get_redis_client(redis_url, context)

        try:
            msg_id = await client.xadd(
                stream_name,
                payload,
                maxlen=max_len,
                approximate=True,
                id=payload["event_id"],
            )
            return str(msg_id)

        except Exception as e:
            # Manejo específico de duplicado (cuando usas id fijo)
            # redis-py levanta ResponseError; comparamos por texto para evitar hard import del tipo.
            msg = str(e)
            if "ID" in msg.upper() and "duplicate" in msg.lower():
                if getattr(context, "logger", None):
                    context.logger.info(
                        "Evento duplicado ignorado", event_id=payload["event_id"]
                    )
                return "DUPLICATE"
            raise

    # -----------------------------
    # HTTP backend (tu app.py /ingest)
    # -----------------------------

    def _sync_http_post_json(
        self,
        url: str,
        body_obj: Dict[str, Any],
        headers: Dict[str, str],
        timeout: float,
    ) -> _HttpResponse:
        body = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json", **(headers or {})}

        req = urlrequest.Request(url=url, data=body, method="POST", headers=req_headers)

        with urlrequest.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return _HttpResponse(status=resp.status, text=text)

    async def _publish_to_http(
        self,
        payload: Dict[str, Any],
        context: ToolExecutionContext,
        tool_config: Dict[str, Any],
    ) -> str:
        http_cfg = tool_config.get("http", {}) or {}

        url = http_cfg.get("url") or "http://127.0.0.1:8000/ingest"
        timeout = float(http_cfg.get("timeout_secs", 3.0))
        headers = http_cfg.get("headers", {}) or {}
        headers = {str(k): str(v) for k, v in headers.items() if v}

        if getattr(context, "logger", None):
            context.logger.debug("Publicando evento por HTTP", url=url, timeout_secs=timeout)

        try:
            resp = await asyncio.to_thread(self._sync_http_post_json, url, payload, headers, timeout)
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(e)
            raise RuntimeError(f"HTTPError publicando evento: {e.code} {detail}") from e
        except URLError as e:
            raise RuntimeError(f"URLError conectando a {url}: {e}") from e

        try:
            data = json.loads(resp.text) if resp.text else {}
        except Exception:
            data = {}

        return str(data.get("id") or data.get("len") or payload["event_id"])



    async def _publish_to_rabbitmq(
        self, payload: Dict[str, Any], context: ToolExecutionContext
    ) -> str:
        """Placeholder para RabbitMQ (implementar si se necesita)."""
        raise NotImplementedError("RabbitMQ backend not yet implemented")

    # -----------------------------
    # Execute
    # -----------------------------

    async def execute(
        self, parameters: Dict[str, Any], context: ToolExecutionContext
    ) -> Dict[str, Any]:
        try:
            # 1) Validación
            self.validate_parameters(parameters)

            # 2) Config de tool
            tool_config = context.get_config_value(f"tools.{self.definition.name}", {}) or {}
            backend = str(tool_config.get("queue_backend", "none")).lower()

            # 3) Filtrado por allow-list
            enabled_types = tool_config.get("enabled_event_types")
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

            # 5) Payload
            payload = self._build_payload(parameters, context)

            # 6) Publish por backend
            if backend == QueueBackend.REDIS.value:
                queue_message_id = await self._publish_to_redis(
                    payload, context, tool_config
                )
            elif backend in (QueueBackend.HTTP.value, QueueBackend.HTTPS.value):
                queue_message_id = await self._publish_to_http(payload, context, tool_config)
            elif backend == QueueBackend.RABBITMQ.value:
                queue_message_id = await self._publish_to_rabbitmq(payload, context)
            else:
                raise ValueError(f"Backend desconocido: {backend}")

            # 7) OK
            if getattr(context, "logger", None):
                context.logger.info(
                    "Evento publicado",
                    event_type=payload["event_type"],
                    event_id=payload["event_id"],
                    backend=backend,
                )

            return {
                "status": "success",
                "message": f"Evento {payload['event_type']} publicado",
                "event_id": payload["event_id"],
                "queue_message_id": queue_message_id,
                "backend": backend,
            }

        except ValueError as e:
            if getattr(context, "logger", None):
                context.logger.warning(
                    "Validación fallida", error=str(e), parameters=parameters
                )
            return {"status": "error", "message": f"Validación: {str(e)}"}

        except Exception as e:
            if getattr(context, "logger", None):
                context.logger.error(
                    "Error ejecutando tool",
                    error=str(e),
                    exc_info=True,
                    parameters=parameters,
                )
            return {
                "status": "error",
                "message": f"Falló ejecución: {str(e)}",
                "error_type": type(e).__name__,
            }

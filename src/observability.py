"""Centralized observability facade for ai-engine.

Provides Logfire tracing and Prometheus metrics with a strict fail-open design:
if Logfire is unavailable (not installed, missing token, disabled via env), the
engine continues to serve calls without any observability side-effects.

Usage::

    from src.observability import configure_observability, get_observability
    obs = configure_observability(ari_base_url=..., asterisk_host=..., asterisk_ari_port=...)
    # or later:
    obs = get_observability()
    obs.ari_command(method="POST", resource="channels/abc123/answer", status=200, duration=0.05)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Dict, Optional

from prometheus_client import Counter, Gauge, Histogram

from .logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Optional Logfire import (fail-open)
# ---------------------------------------------------------------------------
try:
    import logfire as _logfire  # type: ignore[import-untyped]

    _LOGFIRE_AVAILABLE = True
except Exception:
    _logfire = None  # type: ignore[assignment]
    _LOGFIRE_AVAILABLE = False


# ---------------------------------------------------------------------------
# ARI resource normalizer (avoid high-cardinality Prometheus labels)
# ---------------------------------------------------------------------------
_ARI_RESOURCE_PATTERNS = [
    (re.compile(r"channels/[^/]+/answer"), "channels/{id}/answer"),
    (re.compile(r"channels/[^/]+/play"), "channels/{id}/play"),
    (re.compile(r"channels/[^/]+/record"), "channels/{id}/record"),
    (re.compile(r"channels/[^/]+/continue"), "channels/{id}/continue"),
    (re.compile(r"channels/[^/]+/variable"), "channels/{id}/variable"),
    (re.compile(r"channels/[^/]+/mute"), "channels/{id}/mute"),
    (re.compile(r"channels/[^/]+/applications/[^/]+"), "channels/{id}/applications/{app}"),
    (re.compile(r"bridges/[^/]+/addChannel"), "bridges/{id}/addChannel"),
    (re.compile(r"bridges/[^/]+/removeChannel"), "bridges/{id}/removeChannel"),
    (re.compile(r"bridges/[^/]+/play"), "bridges/{id}/play"),
    (re.compile(r"bridges/[^/]+$"), "bridges/{id}"),
    (re.compile(r"playbacks/[^/]+$"), "playbacks/{id}"),
    (re.compile(r"channels/externalMedia"), "channels/externalMedia"),
    (re.compile(r"channels/[^/]+$"), "channels/{id}"),
]


def normalize_ari_resource(resource: str) -> str:
    """Replace dynamic IDs in ARI resource paths with placeholders."""
    for pattern, replacement in _ARI_RESOURCE_PATTERNS:
        if pattern.fullmatch(resource):
            return replacement
    return resource


def _status_class(status: int) -> str:
    """Map HTTP status code to class string (2xx, 4xx, 5xx)."""
    if 200 <= status < 300:
        return "2xx"
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    return "5xx"


# ---------------------------------------------------------------------------
# Prometheus metrics (registered once at module load)
# ---------------------------------------------------------------------------

# ARI metrics
_ARI_UP = Gauge(
    "ai_agent_ari_up",
    "Whether ARI HTTP connection is up (1=connected, 0=disconnected)",
)
_ARI_REQUESTS_TOTAL = Counter(
    "ai_agent_ari_requests_total",
    "Total ARI HTTP requests",
    labelnames=("method", "resource", "status_class"),
)
_ARI_REQUEST_SECONDS = Histogram(
    "ai_agent_ari_request_seconds",
    "ARI HTTP request duration in seconds",
    labelnames=("method", "resource"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
_ARI_ERRORS_TOTAL = Counter(
    "ai_agent_ari_errors_total",
    "Total ARI HTTP errors",
    labelnames=("method", "resource", "error_type"),
)
_ARI_WS_CONNECTED = Gauge(
    "ai_agent_ari_ws_connected",
    "Whether ARI WebSocket is connected (1=yes, 0=no)",
)
_ARI_WS_RECONNECTS_TOTAL = Counter(
    "ai_agent_ari_ws_reconnects_total",
    "Total ARI WebSocket reconnection attempts",
)
_ARI_WS_LAST_EVENT_AGE = Gauge(
    "ai_agent_ari_ws_last_event_age_seconds",
    "Seconds since last ARI WebSocket event",
)

# Media metrics
_MEDIA_ACTIVE = Gauge(
    "ai_agent_media_active_connections",
    "Number of active media connections",
    labelnames=("transport",),
)
_MEDIA_CONNECTIONS_TOTAL = Counter(
    "ai_agent_media_connections_total",
    "Total media connection attempts",
    labelnames=("transport", "result"),
)
_MEDIA_FIRST_AUDIO = Histogram(
    "ai_agent_media_first_audio_seconds",
    "Time from connection to first audio packet",
    labelnames=("transport",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)
_MEDIA_PACKET_LOSS = Counter(
    "ai_agent_media_packet_loss_total",
    "Total media packet loss events",
    labelnames=("transport",),
)
_MEDIA_SESSIONS_STALLED = Gauge(
    "ai_agent_media_sessions_stalled",
    "Number of media sessions with no recent packets",
    labelnames=("transport",),
)


# ---------------------------------------------------------------------------
# Observability singleton
# ---------------------------------------------------------------------------

_instance: Optional[Observability] = None


class Observability:
    """Central observability facade. Every method is fail-safe."""

    def __init__(
        self,
        *,
        ari_base_url: str = "",
        asterisk_host: str = "",
        asterisk_ari_port: int = 8088,
    ) -> None:
        self._ari_base_url = ari_base_url
        self._asterisk_host = asterisk_host
        self._asterisk_ari_port = asterisk_ari_port
        self._logfire_ok = False
        self._ws_last_event_ts: float = 0.0
        self._media_connect_times: Dict[str, float] = {}
        self._last_packet_loss: Dict[str, int] = {}  # transport -> last known total

        self._configure_logfire()

    def _configure_logfire(self) -> None:
        """Attempt to configure Logfire. Silently skip on any failure."""
        enabled = os.getenv("LOGFIRE_ENABLED", "0")
        if enabled not in ("1", "true", "yes"):
            logger.info("Logfire disabled (LOGFIRE_ENABLED=%s)", enabled)
            return

        if not _LOGFIRE_AVAILABLE:
            logger.info("Logfire package not installed; skipping configuration")
            return

        try:
            service_name = os.getenv("LOGFIRE_SERVICE_NAME", "ai-engine")
            environment = os.getenv("LOGFIRE_ENVIRONMENT", "production")
            send_to_logfire = os.getenv("LOGFIRE_SEND_TO_LOGFIRE", "if-token-present")

            _logfire.configure(
                service_name=service_name,
                environment=environment,
                send_to_logfire=send_to_logfire,
            )

            # Instrument aiohttp client (outbound HTTP to Asterisk ARI)
            try:
                _logfire.instrument_aiohttp_client()
            except Exception:
                logger.debug("Logfire aiohttp-client instrumentation failed", exc_info=True)

            self._logfire_ok = True
            logger.info(
                "Logfire configured",
                service_name=service_name,
                environment=environment,
                send_to_logfire=send_to_logfire,
            )
        except Exception:
            logger.warning("Logfire configuration failed; continuing without tracing", exc_info=True)
            self._logfire_ok = False

    # ------------------------------------------------------------------
    # Logfire span helper
    # ------------------------------------------------------------------
    def _span(self, msg: str, **attrs: Any) -> Any:
        """Create a Logfire span if available, otherwise return a no-op context manager."""
        if self._logfire_ok and _logfire is not None:
            try:
                return _logfire.span(msg, **attrs)
            except Exception:
                pass
        return _NoOpSpan()

    def _log_info(self, msg: str, **attrs: Any) -> None:
        """Log an info-level event to Logfire if available."""
        if self._logfire_ok and _logfire is not None:
            try:
                _logfire.info(msg, **attrs)
            except Exception:
                pass

    def _log_warn(self, msg: str, **attrs: Any) -> None:
        if self._logfire_ok and _logfire is not None:
            try:
                _logfire.warn(msg, **attrs)
            except Exception:
                pass

    def _log_error(self, msg: str, **attrs: Any) -> None:
        if self._logfire_ok and _logfire is not None:
            try:
                _logfire.error(msg, **attrs)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # ARI observability
    # ------------------------------------------------------------------
    def ari_connect(self, *, success: bool, attempt: int = 1, error: str = "") -> None:
        """Record an ARI connection attempt."""
        try:
            _ARI_UP.set(1 if success else 0)
            if success:
                self._log_info(
                    "ARI connected",
                    ari_base_url=self._ari_base_url,
                    attempt=attempt,
                )
            else:
                self._log_warn(
                    "ARI connect failed",
                    ari_base_url=self._ari_base_url,
                    attempt=attempt,
                    error=error,
                )
        except Exception:
            pass

    def ari_command(
        self,
        *,
        method: str,
        resource: str,
        status: int,
        duration: float,
        call_id: str = "",
        channel_id: str = "",
        error: str = "",
    ) -> None:
        """Record an ARI HTTP command with normalized resource labels."""
        try:
            normalized = normalize_ari_resource(resource)
            sc = _status_class(status)
            _ARI_REQUESTS_TOTAL.labels(method=method, resource=normalized, status_class=sc).inc()
            _ARI_REQUEST_SECONDS.labels(method=method, resource=normalized).observe(duration)

            if status >= 400:
                error_type = "http_error" if not error else error
                _ARI_ERRORS_TOTAL.labels(method=method, resource=normalized, error_type=error_type).inc()

            # Logfire span with high-cardinality attributes (not Prometheus labels)
            if self._logfire_ok and _logfire is not None:
                try:
                    attrs: Dict[str, Any] = {
                        "method": method,
                        "resource": resource,
                        "normalized_resource": normalized,
                        "status": status,
                        "duration_s": duration,
                    }
                    if call_id:
                        attrs["call_id"] = call_id
                    if channel_id:
                        attrs["channel_id"] = channel_id
                    if error:
                        attrs["error"] = error
                    _logfire.info("ARI {method} {normalized_resource} -> {status}", **attrs)
                except Exception:
                    pass
        except Exception:
            pass

    def ari_error(
        self,
        *,
        method: str,
        resource: str,
        error_type: str,
        error: str = "",
        call_id: str = "",
    ) -> None:
        """Record an ARI error (e.g. network failure before getting a status code)."""
        try:
            normalized = normalize_ari_resource(resource)
            _ARI_ERRORS_TOTAL.labels(method=method, resource=normalized, error_type=error_type).inc()
            self._log_error(
                "ARI error {method} {resource}",
                method=method,
                resource=resource,
                error_type=error_type,
                error=error,
                call_id=call_id,
            )
        except Exception:
            pass

    def ari_ws_connected(self) -> None:
        """Record ARI WebSocket connected."""
        try:
            _ARI_WS_CONNECTED.set(1)
            self._ws_last_event_ts = time.time()
            self._log_info("ARI WebSocket connected")
        except Exception:
            pass

    def ari_ws_disconnected(self) -> None:
        """Record ARI WebSocket disconnected."""
        try:
            _ARI_WS_CONNECTED.set(0)
            _ARI_UP.set(0)
            self._log_warn("ARI WebSocket disconnected")
        except Exception:
            pass

    def ari_ws_reconnect(self, *, attempt: int = 1) -> None:
        """Record an ARI WebSocket reconnection attempt."""
        try:
            _ARI_WS_RECONNECTS_TOTAL.inc()
            self._log_warn("ARI WebSocket reconnecting", attempt=attempt)
        except Exception:
            pass

    def ari_ws_event_received(self) -> None:
        """Update last WS event timestamp (called per event; no span created)."""
        try:
            self._ws_last_event_ts = time.time()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Media observability (RTP / AudioSocket)
    # ------------------------------------------------------------------
    def media_connected(self, *, transport: str, conn_id: str = "", call_id: str = "") -> None:
        """Record a new media connection."""
        try:
            _MEDIA_ACTIVE.labels(transport=transport).inc()
            _MEDIA_CONNECTIONS_TOTAL.labels(transport=transport, result="connected").inc()
            self._media_connect_times[conn_id or call_id] = time.time()
            self._log_info(
                "Media connected",
                transport=transport,
                conn_id=conn_id,
                call_id=call_id,
            )
        except Exception:
            pass

    def media_rejected(self, *, transport: str, reason: str = "", conn_id: str = "") -> None:
        """Record a rejected media connection."""
        try:
            _MEDIA_CONNECTIONS_TOTAL.labels(transport=transport, result="rejected").inc()
            self._log_warn(
                "Media rejected",
                transport=transport,
                reason=reason,
                conn_id=conn_id,
            )
        except Exception:
            pass

    def media_first_audio(self, *, transport: str, conn_id: str = "", call_id: str = "") -> None:
        """Record first audio packet for a media connection."""
        try:
            key = conn_id or call_id
            connect_ts = self._media_connect_times.get(key)
            if connect_ts is not None:
                latency = time.time() - connect_ts
                if latency < 0:
                    latency = 0.0
                _MEDIA_FIRST_AUDIO.labels(transport=transport).observe(latency)
                self._log_info(
                    "Media first audio",
                    transport=transport,
                    conn_id=conn_id,
                    call_id=call_id,
                    first_audio_latency_s=latency,
                )
            else:
                self._log_info(
                    "Media first audio (no connect time)",
                    transport=transport,
                    conn_id=conn_id,
                    call_id=call_id,
                )
        except Exception:
            pass

    def media_disconnected(self, *, transport: str, conn_id: str = "", call_id: str = "") -> None:
        """Record a media disconnection."""
        try:
            key = conn_id or call_id
            self._media_connect_times.pop(key, None)
            # Guard against gauge going negative
            gauge = _MEDIA_ACTIVE.labels(transport=transport)
            current = gauge._value.get()
            if current > 0:
                gauge.dec()
            self._log_info(
                "Media disconnected",
                transport=transport,
                conn_id=conn_id,
                call_id=call_id,
            )
        except Exception:
            pass

    def sample_media(self, *, transport: str, packet_loss: int = 0, stalled: int = 0) -> None:
        """Push periodic media quality sample (called by the sampler task).

        ``packet_loss`` is the cumulative total from the RTP server; only the
        delta since the last sample is added to the Prometheus Counter.
        """
        try:
            prev = self._last_packet_loss.get(transport, 0)
            delta = packet_loss - prev
            if delta > 0:
                _MEDIA_PACKET_LOSS.labels(transport=transport).inc(delta)
            self._last_packet_loss[transport] = packet_loss
            _MEDIA_SESSIONS_STALLED.labels(transport=transport).set(stalled)
        except Exception:
            pass

    async def run_media_sampler(
        self,
        *,
        rtp_server: Any = None,
        interval: float = 15.0,
    ) -> None:
        """Periodically sample RTP server stats and push to Prometheus.

        This coroutine runs until cancelled. It propagates CancelledError.
        """
        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

            try:
                if rtp_server is None:
                    continue

                stats = rtp_server.get_stats()
                now = time.time()

                # Count stalled sessions (no packets for >30s)
                stalled = 0
                sessions = getattr(rtp_server, "sessions", {})
                for sess in sessions.values():
                    if now - getattr(sess, "last_packet_at", now) > 30:
                        stalled += 1

                total_loss = stats.get("packet_loss_total", 0)
                self.sample_media(transport="rtp", packet_loss=total_loss, stalled=stalled)

                # Update WS event age
                if self._ws_last_event_ts > 0:
                    try:
                        _ARI_WS_LAST_EVENT_AGE.set(now - self._ws_last_event_ts)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Media sampler iteration failed", exc_info=True)


# ---------------------------------------------------------------------------
# No-op context manager for when Logfire is unavailable
# ---------------------------------------------------------------------------
class _NoOpSpan:
    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    async def __aenter__(self) -> "_NoOpSpan":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Module-level API
# ---------------------------------------------------------------------------
def configure_observability(
    *,
    ari_base_url: str = "",
    asterisk_host: str = "",
    asterisk_ari_port: int = 8088,
) -> Observability:
    """Initialize and return the global Observability singleton."""
    global _instance
    _instance = Observability(
        ari_base_url=ari_base_url,
        asterisk_host=asterisk_host,
        asterisk_ari_port=asterisk_ari_port,
    )
    return _instance


def get_observability() -> Observability:
    """Return the global Observability instance (lazy-creates a no-op one if needed)."""
    global _instance
    if _instance is None:
        _instance = Observability()
    return _instance

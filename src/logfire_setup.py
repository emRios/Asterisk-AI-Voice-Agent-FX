"""Logfire setup shared by logging and observability (fail-open).

- ``ensure_logfire_configured()``: a single ``logfire.configure`` per process,
  used by ``configure_logging()`` (log forwarding) and by ``Observability``
  (f2 spans/events + aiohttp instrumentation).
- ``build_log_forwarder()``: structlog processor that sends a sanitized COPY
  of each event to Logfire and always returns the original event untouched,
  so stdout (``docker logs``) is byte-identical with or without Logfire.

This module must not import ``logging_config`` (it is imported lazily from it).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import traceback
from typing import Any, Callable, Dict, Optional

try:
    import logfire as _logfire  # type: ignore[import-untyped]

    _LOGFIRE_AVAILABLE = True
except Exception:
    _logfire = None  # type: ignore[assignment]
    _LOGFIRE_AVAILABLE = False

_TRUTHY = ("1", "true", "yes")

# None = not attempted, True = configured, False = failed (no retry)
_configured: Optional[bool] = None
_config_error: Optional[str] = None
_config_lock = threading.Lock()


def logfire_enabled() -> bool:
    return os.getenv("LOGFIRE_ENABLED", "0") in _TRUTHY


def logfire_settings() -> Dict[str, str]:
    return {
        "service_name": os.getenv("LOGFIRE_SERVICE_NAME", "ai-engine"),
        "environment": os.getenv("LOGFIRE_ENVIRONMENT", "production"),
        "send_to_logfire": os.getenv("LOGFIRE_SEND_TO_LOGFIRE", "if-token-present"),
    }


# ---------------------------------------------------------------------------
# Scrubbing: Logfire's default scrubber stays on; these exact attribute paths
# are kept because they are operational ids or text already redacted by us.
# ---------------------------------------------------------------------------
_EXC_TYPE_KEY = "exception_type"
_EXC_MESSAGE_KEY = "exception_message"
_EXC_STACK_KEY = "exception_stacktrace"

_SCRUB_KEEP_PATHS = frozenset(
    ("attributes", key)
    for key in (
        "call_id",
        "session_id",
        "channel_id",
        "caller_channel_id",
        "bridge_id",
        "conn_id",
        _EXC_TYPE_KEY,
        _EXC_MESSAGE_KEY,
        _EXC_STACK_KEY,
    )
)


def _scrub_callback(match: Any) -> Any:
    try:
        if tuple(match.path) in _SCRUB_KEEP_PATHS:
            return match.value
    except Exception:
        pass
    return None


def ensure_logfire_configured() -> bool:
    """Configure Logfire once per process. Returns True only on success.

    Never raises and never logs (it runs before structlog is configured).
    A failed attempt is not retried.
    """
    global _configured, _config_error
    if _configured is not None:
        return _configured
    with _config_lock:
        if _configured is not None:
            return _configured
        if not logfire_enabled() or not _LOGFIRE_AVAILABLE:
            # Not a failure: allow a later call once the env/package changes (tests).
            return False
        try:
            settings = logfire_settings()
            _logfire.configure(
                service_name=settings["service_name"],
                environment=settings["environment"],
                send_to_logfire=settings["send_to_logfire"],
                scrubbing=_logfire.ScrubbingOptions(callback=_scrub_callback),
            )
            _configured = True
        except Exception as exc:  # pragma: no cover - depends on env
            _config_error = f"{type(exc).__name__}: {exc}"
            _configured = False
        return _configured


def logfire_config_error() -> Optional[str]:
    return _config_error


# ---------------------------------------------------------------------------
# Secret redaction for free text (event message, string attributes, exceptions)
# ---------------------------------------------------------------------------
REDACTED = "***REDACTED***"

# Quoted values honour escapes; an unterminated quote redacts to end of line.
_VALUE = r"""(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|[^\s"',;&}\]]+|["'][^\n]*)"""
_AUTH_SCHEMES = r"(?:bearer|basic|token|digest|apikey|api-key|key|hmac|negotiate|dsn|aws4-hmac-sha256)"
_SECRET_PATTERNS = [
    # Authorization header: whole credential, any length (scheme kept)
    (
        re.compile(
            r"(?i)(\b(?:proxy-)?authorization\b[\"']?\s*[:=]\s*[\"']?)(" + _AUTH_SCHEMES + r"\s+)?" + _VALUE
        ),
        r"\1\2" + REDACTED,
    ),
    # Authorization schemes outside a header
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]{8,}=*"), r"\1" + REDACTED),
    (re.compile(r"(?i)(\bbasic\s+)(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{8,}={0,2}"), r"\1" + REDACTED),
    # name=value / name: value / "name": "value"
    (
        re.compile(
            r"(?i)(\b(?:x-api-key|api[_-]?key|access[_-]?token|"
            r"refresh[_-]?token|auth[_-]?token|client[_-]?secret|modal[_-]?key|modal[_-]?secret|"
            r"token|secret|password|passwd|pwd)\b[\"']?\s*[:=]\s*)"
            r"(?!(?:bearer|basic)\s)" + _VALUE
        ),
        r"\1" + REDACTED,
    ),
    # Credentials in URL query strings
    (
        re.compile(r"(?i)([?&](?:key|api_key|apikey|token|access_token|auth|sig|signature|secret)=)[^&\s\"'#]+"),
        r"\1" + REDACTED,
    ),
    # Known token formats
    (re.compile(r"\bpylf_v1_[A-Za-z0-9_-]+"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\b(?:wk|ws|ak|as)-[A-Za-z0-9]{16,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
]


def redact_text(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# Forwarder
# ---------------------------------------------------------------------------
# Transcripts are already sent to Logfire by GoldenBridge: keep only metadata.
_DROP_FIELDS = {
    "Deepgram conversation text": ("text", "segments"),
}
_TEXT_LEN_SOURCE = "text"

# Limits in characters (not bytes), applied after redaction.
_MAX_EXC_MESSAGE = 2 * 1024
_MAX_EXC_STACK = 16 * 1024
_EXC_UNAVAILABLE = "<no disponible: error al formatear>"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "exception": logging.ERROR,
    "critical": logging.CRITICAL,
    "fatal": logging.CRITICAL,
}


def _resolve_exception(exc_info: Any) -> tuple:
    """Return (exception or None, dropped flag)."""
    if exc_info is None or exc_info is False:
        return None, False
    if isinstance(exc_info, BaseException):
        return exc_info, False
    if exc_info is True:
        return sys.exc_info()[1], False
    if isinstance(exc_info, (tuple, list)) and len(exc_info) == 3:
        if exc_info[1] is None:
            return None, False
        if isinstance(exc_info[1], BaseException):
            return exc_info[1], False
    return None, True


def _exception_fields(exc: BaseException) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    try:
        fields[_EXC_TYPE_KEY] = f"{redact_text(str(type(exc).__module__))}.{redact_text(type(exc).__qualname__)}"
    except Exception:
        fields[_EXC_TYPE_KEY] = _EXC_UNAVAILABLE
    try:
        fields[_EXC_MESSAGE_KEY] = redact_text(str(exc))[:_MAX_EXC_MESSAGE]
    except Exception:
        fields[_EXC_MESSAGE_KEY] = _EXC_UNAVAILABLE
    try:
        stack = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        fields[_EXC_STACK_KEY] = redact_text(stack)[-_MAX_EXC_STACK:]
    except Exception:
        fields[_EXC_STACK_KEY] = _EXC_UNAVAILABLE
    return fields


def _parse_level(value: str) -> Optional[int]:
    return _LEVELS.get(value.strip().lower())


class _LogForwarder:
    def __init__(self, processor: Callable[..., Any], min_level: int) -> None:
        self._processor = processor
        self._min_level = min_level
        self.errors = 0

    def _build_copy(self, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(event_dict)

        drop = _DROP_FIELDS.get(data.get("event"))
        if drop:
            text = data.get(_TEXT_LEN_SOURCE)
            for key in drop:
                data.pop(key, None)
            if isinstance(text, str):
                data["text_len"] = len(text)

        for key in (_EXC_TYPE_KEY, _EXC_MESSAGE_KEY, _EXC_STACK_KEY):
            data.pop(key, None)

        for key, value in data.items():
            if isinstance(value, str) and key not in ("level", "timestamp"):
                data[key] = redact_text(value)

        if "exc_info" in data:
            exc, dropped = _resolve_exception(data.pop("exc_info"))
            if exc is not None:
                data.update(_exception_fields(exc))
            elif dropped:
                data["exc_info_dropped"] = True
        return data

    def __call__(self, logger: Any, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
        try:
            level = _LEVELS.get(str(event_dict.get("level", "info")).lower(), logging.INFO)
            if level >= self._min_level:
                self._processor(logger, method_name, self._build_copy(event_dict))
        except Exception as exc:
            self.errors += 1
            if self.errors == 1:
                try:
                    print(
                        f"logfire log forwarding error (further errors silenced): {type(exc).__name__}",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
        return event_dict


def build_log_forwarder() -> Optional[_LogForwarder]:
    """Return the forwarder, or None (chain stays as today) on any problem."""
    try:
        if os.getenv("LOGFIRE_FORWARD_LOGS", "1") not in _TRUTHY:
            return None
        min_level = _parse_level(os.getenv("LOGFIRE_LOG_LEVEL", "INFO"))
        if min_level is None:
            return None
        if not ensure_logfire_configured():
            return None
        from logfire.integrations.structlog import LogfireProcessor  # type: ignore[import-untyped]

        return _LogForwarder(LogfireProcessor(console_log=False), min_level)
    except Exception:
        return None


def _reset_for_tests() -> None:
    global _configured, _config_error
    _configured = None
    _config_error = None

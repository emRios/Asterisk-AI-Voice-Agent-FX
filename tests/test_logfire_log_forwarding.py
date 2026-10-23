"""Forwarding of structlog events to Logfire (src/logfire_setup.py)."""

import json
import logging
import sys

import pytest
import structlog

logfire = pytest.importorskip("logfire")
from logfire.testing import TestExporter  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402

from src import logfire_setup  # noqa: E402
from src.logging_config import configure_logging  # noqa: E402

SECRET = "ejemplo-secreto-123"


@pytest.fixture
def lf(monkeypatch):
    """Logfire configured against an in-memory exporter; restores global state."""
    saved_structlog = structlog.get_config()
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level

    exporter = TestExporter()
    real_configure = logfire.configure
    calls = []

    def fake_configure(**kwargs):
        calls.append(kwargs)
        kwargs["send_to_logfire"] = False
        real_configure(
            console=False,
            additional_span_processors=[SimpleSpanProcessor(exporter)],
            **kwargs,
        )

    monkeypatch.setattr(logfire, "configure", fake_configure)
    for name in ("LOG_LEVEL", "LOG_SHOW_TRACEBACKS", "LOGFIRE_LOG_LEVEL", "LOGFIRE_FORWARD_LOGS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOGFIRE_ENABLED", "1")
    monkeypatch.setenv("LOG_FORMAT", "json")
    logfire_setup._reset_for_tests()

    exporter.calls = calls
    yield exporter

    logfire_setup._reset_for_tests()
    structlog.configure(**saved_structlog)
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _logs(exporter):
    return [s for s in exporter.exported_spans_as_dict() if s["attributes"].get("logfire.span_type") == "log"]


def _dump(spans):
    return json.dumps(spans, default=str)


def _stdout_records(out):
    records = []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        d.pop("timestamp", None)
        records.append(d)
    return records


def _has_forwarder():
    return any(isinstance(p, logfire_setup._LogForwarder) for p in structlog.get_config()["processors"])


# --------------------------------------------------------------------------- redaction unit


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {SECRET}",
        f"authorization: bearer {SECRET}",
        f"token={SECRET}",
        f"TOKEN = '{SECRET}'",
        f'{{"api_key": "{SECRET}"}}',
        f"{{'password': '{SECRET} con espacio'}}",
        f"https://h/x?key={SECRET}&token={SECRET}",
        f"MODAL_SECRET={SECRET} y MODAL_KEY={SECRET}",
        "pylf_v1_us_AbCdEfGh123456",
        "wk-AbCdEfGh12345678 ws-AbCdEfGh12345678",
        "Basic dXNlcjpwYXNzd29yZA==",
    ],
)
def test_redact_text_removes_secret(text):
    out = logfire_setup.redact_text(text)
    for secret in (SECRET, "pylf_v1_us_AbCdEfGh123456", "AbCdEfGh12345678", "dXNlcjpwYXNzd29yZA=="):
        assert secret not in out
    assert logfire_setup.REDACTED in out


@pytest.mark.parametrize(
    "text",
    ["max_tokens=200", "basic configuration loaded", "call_id=1790611608.96", "Hanging up channel"],
)
def test_redact_text_keeps_normal_text(text):
    assert logfire_setup.redact_text(text) == text


# --------------------------------------------------------------------------- forwarding


def test_info_forwarded_debug_not(lf, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_logging()
    log = structlog.get_logger("t")
    log.info("hola info", call_id="c1", n=3)
    log.debug("hola debug")
    logs = _logs(lf)
    assert [s["attributes"]["logfire.msg"] for s in logs] == ["hola info"]
    attrs = logs[0]["attributes"]
    assert attrs["call_id"] == "c1" and attrs["n"] == 3
    assert attrs["logfire.level_num"] == 9  # info


def test_secret_in_message_redacted_stdout_unchanged(lf, capsys):
    configure_logging()
    log = structlog.get_logger("t")
    log.info(f"llamada fallo Authorization: Bearer {SECRET}", detail=f"token={SECRET}")
    log.info("pylf_v1_us_AbCdEfGh123456 visto")
    dumped = _dump(_logs(lf))
    assert SECRET not in dumped and "pylf_v1_us_AbCdEfGh123456" not in dumped
    assert "llamada fallo Authorization: Bearer ***REDACTED***" in dumped
    # stdout behaves as today (key-based sanitizer only)
    assert SECRET in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["triple", "true", "direct"])
def test_exc_info_always_forwarded_as_text(lf, monkeypatch, mode):
    monkeypatch.setenv("LOG_SHOW_TRACEBACKS", "always")
    configure_logging()
    log = structlog.get_logger("t")
    try:
        raise ValueError("boom")
    except ValueError as e:
        triple = sys.exc_info()
        if mode == "true":
            log.error("fallo", exc_info=True)
        exc = e
    if mode == "triple":
        log.error("fallo", exc_info=triple)
    elif mode == "direct":
        log.error("fallo", exc_info=exc)
    logs = _logs(lf)
    assert len(logs) == 1
    attrs = logs[0]["attributes"]
    assert attrs["exception_type"] == "builtins.ValueError"
    assert attrs["exception_message"] == "boom"
    assert "ValueError: boom" in attrs["exception_stacktrace"]
    assert "exc_info" not in attrs


def test_exc_info_never_policy(lf, monkeypatch):
    monkeypatch.setenv("LOG_SHOW_TRACEBACKS", "never")
    configure_logging()
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("t").error("fallo", exc_info=True)
    logs = _logs(lf)
    assert len(logs) == 1
    assert "exception_message" not in logs[0]["attributes"]


def test_invalid_exc_info_dropped(lf, monkeypatch):
    monkeypatch.setenv("LOG_SHOW_TRACEBACKS", "always")
    configure_logging()
    structlog.get_logger("t").error("fallo", exc_info=["no", "es", "valido"])
    attrs = _logs(lf)[0]["attributes"]
    assert attrs["exc_info_dropped"] is True


def test_exception_secrets_redacted_but_diagnostic_kept(lf, monkeypatch):
    monkeypatch.setenv("LOG_SHOW_TRACEBACKS", "always")
    configure_logging()
    try:
        try:
            raise KeyError(f"token={SECRET}")
        except KeyError as inner:
            raise RuntimeError(f"Fallo HTTP: Authorization: Bearer {SECRET}") from inner
    except RuntimeError:
        structlog.get_logger("t").error("fallo", exc_info=True)
    dumped = _dump(_logs(lf))
    assert SECRET not in dumped
    assert "[Scrubbed" not in dumped
    attrs = _logs(lf)[0]["attributes"]
    assert attrs["exception_message"] == "Fallo HTTP: Authorization: Bearer ***REDACTED***"
    assert "KeyError" in attrs["exception_stacktrace"]


def test_secret_on_code_line_of_traceback_redacted(lf, monkeypatch):
    monkeypatch.setenv("LOG_SHOW_TRACEBACKS", "always")
    configure_logging()
    try:
        raise ValueError("x")  # token=ejemplo-secreto-123
    except ValueError:
        structlog.get_logger("t").error("fallo", exc_info=True)
    assert SECRET not in _dump(_logs(lf))


def test_caller_cannot_spoof_exception_fields(lf):
    configure_logging()
    structlog.get_logger("t").info("x", exception_message=f"Authorization: Bearer {SECRET}")
    attrs = _logs(lf)[0]["attributes"]
    assert "exception_message" not in attrs
    assert SECRET not in _dump(_logs(lf))


def test_conversation_text_not_forwarded(lf, capsys):
    configure_logging()
    structlog.get_logger("t").info(
        "Deepgram conversation text", call_id="c1", role="user", text="hola quiero mi tarjeta", segments=[{"a": 1}]
    )
    attrs = _logs(lf)[0]["attributes"]
    assert "text" not in attrs and "segments" not in attrs
    assert attrs["text_len"] == len("hola quiero mi tarjeta")
    assert attrs["call_id"] == "c1" and attrs["role"] == "user"
    assert "hola quiero mi tarjeta" in capsys.readouterr().out


def test_scrubbing_keeps_ids_redacts_sensitive_keys(lf):
    configure_logging()
    structlog.get_logger("t").info(
        "ids",
        session_id="dg-123",
        call_id="call-auth-123",
        cookie="c-val",
        access_token_hint="x-val",
        payload={"session_id": "p-val"},
    )
    attrs = _logs(lf)[0]["attributes"]
    assert attrs["session_id"] == "dg-123"
    assert attrs["call_id"] == "call-auth-123"
    dumped = _dump(_logs(lf))
    assert "c-val" not in dumped and "p-val" not in dumped


def test_stdout_identical_with_and_without_logfire(lf, monkeypatch, capsys):
    def emit():
        log = structlog.get_logger("t")
        log.info("Deepgram conversation text", call_id="c1", text="hola", segments=[1])
        log.info(f"Authorization: Bearer {SECRET}", api_key="abcdef")
        try:
            raise ValueError("boom")
        except ValueError:
            log.error("fallo", exc_info=True)

    monkeypatch.setenv("LOG_SHOW_TRACEBACKS", "always")
    configure_logging()
    assert _has_forwarder()
    emit()
    with_lf = _stdout_records(capsys.readouterr().out)

    monkeypatch.setenv("LOGFIRE_ENABLED", "0")
    logfire_setup._reset_for_tests()
    configure_logging()
    assert not _has_forwarder()
    emit()
    without_lf = _stdout_records(capsys.readouterr().out)
    assert with_lf == without_lf and len(with_lf) == 3


@pytest.mark.parametrize(
    "env",
    [
        {"LOGFIRE_ENABLED": "0"},
        {"LOGFIRE_LOG_LEVEL": "nivel-invalido"},
        {"LOGFIRE_FORWARD_LOGS": "0"},
    ],
)
def test_chain_unchanged_when_not_forwarding(lf, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    configure_logging()
    assert not _has_forwarder()


def test_configure_failure_keeps_chain(lf, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no config")

    monkeypatch.setattr(logfire, "configure", boom)
    configure_logging()
    assert not _has_forwarder()
    assert logfire_setup.ensure_logfire_configured() is False
    assert "no config" in logfire_setup.logfire_config_error()


def test_forwarder_error_does_not_break_stdout(lf, capsys):
    configure_logging()
    fwd = next(p for p in structlog.get_config()["processors"] if isinstance(p, logfire_setup._LogForwarder))

    def broken(*a, **k):
        raise RuntimeError("export roto")

    fwd._processor = broken
    structlog.get_logger("t").info("sigue vivo")
    captured = capsys.readouterr()
    assert "sigue vivo" in captured.out
    assert fwd.errors == 1


def test_single_configure_and_instrumentation(lf, monkeypatch):
    from src import observability

    instrument_calls = []
    monkeypatch.setattr(observability, "_aiohttp_instrumented", False)
    monkeypatch.setattr(logfire, "instrument_aiohttp_client", lambda *a, **k: instrument_calls.append(1))
    monkeypatch.setattr(observability, "_instance", None)
    configure_logging()
    configure_logging()
    obs = observability.configure_observability()
    observability.Observability()
    assert len(lf.calls) == 1
    assert len(instrument_calls) == 1
    assert obs._logfire_ok is True
    obs.media_connected(call_id="c1", transport="rtp") if hasattr(obs, "media_connected") else None
    structlog.get_logger("t").info("log tras observability")
    assert any(s["attributes"].get("logfire.msg") == "log tras observability" for s in _logs(lf))


def test_logger_created_before_configure_is_forwarded(lf):
    early = structlog.get_logger("temprano")
    configure_logging()
    early.info("desde logger temprano")
    assert [s["attributes"]["logfire.msg"] for s in _logs(lf)] == ["desde logger temprano"]

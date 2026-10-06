"""F2.6.1 S3 — ai-engine: el tipo CALL_SUMMARY y su transporte (§12.5).

La foto viaja como una invocación de CallEventNotification desde GoldenBridge
(canal servidor→engine), NUNCA desde el LLM. S3 solo habilita el camino: el
tipo es válido para execute pero está fuera del enum expuesto al modelo, el
event_id va por seq, el summary viaja tal cual a la raíz del payload, y el
proveedor captura el contexto síncrono / retiene la tarea / no ensucia el
historial. Inerte hasta que GoldenBridge emita (S5).
"""
import json
import types

import pytest

from src.tools.business.event_notify import CallEventNotification, EventType


def _summary(seq=1, **over):
    base = {
        "schema_version": 1, "seq": seq, "is_final": True, "generated_at": 1791300000,
        "initial_state": "SALUDO", "final_state": "SALUDO",
        "meaningful_transitions": 0, "user_turns": 0, "filler_only_turns": 0,
        "agent_responses": 1, "exchanges": 0, "words_first_turn": 0, "words_median": 0,
        "words_max": 0, "vad_turns": 0, "vad_no_transcript": 0,
        "amd_verdict": None, "machine_phrase_seen": False,
    }
    base.update(over)
    return base


def _tool():
    return CallEventNotification()


# ---------------------------------------------- oculto del LLM / schema estable

def test_call_summary_is_in_the_enum_but_not_exposed_to_the_llm():
    assert EventType.CALL_SUMMARY.value == "CALL_SUMMARY"
    definition = _tool().definition
    event_param = next(p for p in definition.parameters if p.name == "event_type")
    assert "CALL_SUMMARY" not in event_param.enum          # el LLM no lo ve
    # pero sí todos los eventos que el LLM puede elegir hoy
    assert "PURCHASE_INTENT_HIGH" in event_param.enum and "TRANSITION" in event_param.enum


def test_exported_schema_is_unchanged_for_the_llm():
    """Regresión: el schema que ve el proveedor no cambió (ningún parámetro nuevo)."""
    definition = _tool().definition
    names = {p.name for p in definition.parameters}
    assert names == {"event_type", "intent_score", "product_name", "product_id",
                     "notes", "event_description", "extracted_data", "priority"}
    assert "summary" not in names                          # no se declaró summary
    schema = definition.to_deepgram_schema()
    enum = schema["parameters"]["properties"]["event_type"]["enum"]
    assert "CALL_SUMMARY" not in enum


# --------------------------------------------------------------- validación

def test_valid_call_summary_passes():
    assert _tool().validate_parameters({"event_type": "CALL_SUMMARY", "summary": _summary()}) is True


@pytest.mark.parametrize("params", [
    {"event_type": "CALL_SUMMARY"},                               # sin summary
    {"event_type": "CALL_SUMMARY", "summary": "x"},               # no dict
    {"event_type": "CALL_SUMMARY", "summary": _summary() | {"schema_version": 2}},
    {"event_type": "CALL_SUMMARY", "summary": _summary() | {"seq": 0}},
    {"event_type": "CALL_SUMMARY", "summary": _summary() | {"seq": "1"}},
    {"event_type": "CALL_SUMMARY", "summary": _summary() | {"seq": True}},
])
def test_invalid_call_summary_is_rejected(params):
    with pytest.raises(ValueError):
        _tool().validate_parameters(params)


# --------------------------------------------------------------- event_id

def test_event_id_uses_seq():
    tool = _tool()
    eid = tool._generate_event_id("chan-1", "CALL_SUMMARY", {"summary": _summary(seq=7)})
    assert eid == "chan-1:CALL_SUMMARY:7"
    # dos fotos del mismo segundo NO colisionan
    assert tool._generate_event_id("chan-1", "CALL_SUMMARY", {"summary": _summary(seq=8)}) != eid


def test_event_id_other_events_keep_the_md5():
    eid = _tool()._generate_event_id("chan-1", "TRANSITION", {})
    assert eid.startswith("evt_")


# --------------------------------------------------------------- payload

def test_summary_travels_verbatim_in_the_payload_root():
    tool = _tool()
    ctx = types.SimpleNamespace(call_id="chan-9", agent_id=None, provider_name="deepgram")
    foto = _summary(seq=4, user_turns=3)
    payload = tool._build_payload(
        {"event_type": "CALL_SUMMARY", "summary": foto}, ctx, session=None
    )
    assert payload["summary"] == foto                      # tal cual, sin rearmar
    assert payload["event_id"] == "chan-9:CALL_SUMMARY:4"
    assert payload["event_type"] == "CALL_SUMMARY" and payload["call_id"] == "chan-9"


# ---------------------------------------------- config real (yaml en disco)

def test_yaml_lists_call_summary_in_both_lists():
    import yaml, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config" / "ai-agent.yaml").read_text())["tools"]["CallEventNotification"]
    assert "CALL_SUMMARY" in cfg["http"]["urls_by_event_type"]   # tiene endpoint
    assert "CALL_SUMMARY" in cfg["enabled_event_types"]          # está habilitado


# ------------------------------------------- proveedor: contexto / historial
from src.providers.deepgram import DeepgramProvider


class _WS:
    async def send(self, payload):  # noqa: D401
        pass


def _provider():
    async def on_event(_e):
        pass
    p = DeepgramProvider({}, types.SimpleNamespace(), on_event)
    p.call_id = "chan-9"
    p.websocket = _WS()
    return p


def test_context_snapshot_captures_call_id_and_websocket():
    p = _provider()
    ctx = p._function_call_context()
    assert ctx["call_id"] == "chan-9" and ctx["websocket"] is p.websocket
    # el snapshot es inmune a que stop_session borre call_id después
    p.call_id = None
    assert ctx["call_id"] == "chan-9"


async def test_handle_function_call_uses_the_captured_context_not_self():
    """La foto final cerca del cierre: self.call_id ya es None, pero la tarea usa
    el contexto capturado en el receive loop."""
    p = _provider()
    seen = {}

    async def adapter(event_data, context):
        seen["call_id"] = context["call_id"]
        return {"status": "success", "function_name": "CallEventNotification"}

    p.tool_adapter.handle_tool_call_event = adapter
    captured = p._function_call_context()
    p.call_id = None                      # stop_session borró el call_id
    fcr = {"functions": [{"id": "fc1", "name": "CallEventNotification",
                          "arguments": json.dumps({"event_type": "CALL_SUMMARY",
                                                    "summary": _summary()})}]}
    await p._handle_function_call(fcr, captured)
    assert seen["call_id"] == "chan-9"


async def test_call_summary_is_excluded_from_tool_history():
    recorded = []

    class Store:
        async def get_by_call_id(self, cid):
            s = types.SimpleNamespace(tool_calls=recorded)
            return s
        async def upsert_call(self, s):
            pass

    p = _provider()
    p._session_store = Store()

    async def adapter(event_data, context):
        return {"status": "success", "function_name": "CallEventNotification"}

    p.tool_adapter.handle_tool_call_event = adapter

    async def ok_send(result, context):
        pass
    p.tool_adapter.send_tool_result = ok_send

    def fcr(et):
        return {"functions": [{"id": "fc1", "name": "CallEventNotification",
                               "arguments": json.dumps({"event_type": et, "summary": _summary()})}]}

    await p._handle_function_call(fcr("CALL_SUMMARY"), p._function_call_context())
    assert recorded == []                 # la foto NO entra al historial
    await p._handle_function_call(fcr("TRANSITION"), p._function_call_context())
    assert len(recorded) == 1             # un evento normal sí


async def test_tracked_task_logs_on_crash(caplog):
    import asyncio
    p = _provider()

    async def boom():
        raise RuntimeError("x")

    t = p._track_task(asyncio.create_task(boom()), "function_call")
    assert t in p._bg_tasks
    with pytest.raises(RuntimeError):
        await t
    await asyncio.sleep(0)
    assert t not in p._bg_tasks           # se descarta al terminar

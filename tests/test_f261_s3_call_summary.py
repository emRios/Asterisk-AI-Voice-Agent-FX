"""F2.6.1 S3 — ai-engine: CALL_SUMMARY por canal de PROTOCOLO (§12.2 plan B, §12.5).

La foto NO viaja como tool call: un LLM podría falsificarla y reclasificar a un
humano real (Codex). Viaja como un mensaje de protocolo propio ({"type":"CallSummary"})
que GoldenBridge envía por el WS — el LLM no puede producir un tipo de mensaje del
protocolo. El camino del LLM (CallEventNotification con event_type=CALL_SUMMARY) se
rechaza SIEMPRE. Mismo patrón que ServerHangup (A1).
"""
import asyncio
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
    event_param = next(p for p in _tool().definition.parameters if p.name == "event_type")
    assert "CALL_SUMMARY" not in event_param.enum
    assert "PURCHASE_INTENT_HIGH" in event_param.enum and "TRANSITION" in event_param.enum


def test_exported_schema_is_unchanged_for_the_llm():
    definition = _tool().definition
    names = {p.name for p in definition.parameters}
    assert names == {"event_type", "intent_score", "product_name", "product_id",
                     "notes", "event_description", "extracted_data", "priority"}
    assert "summary" not in names
    assert "CALL_SUMMARY" not in definition.to_deepgram_schema()["parameters"]["properties"]["event_type"]["enum"]


# ------------------------------------------ el LLM NO puede publicar la foto

def test_llm_path_rejects_call_summary():
    """validate_parameters rechaza SIEMPRE CALL_SUMMARY por el camino del LLM,
    aunque traiga un summary válido."""
    with pytest.raises(ValueError):
        _tool().validate_parameters({"event_type": "CALL_SUMMARY", "summary": _summary()})


async def test_llm_execute_of_call_summary_does_not_post():
    tool = _tool()
    posted = []
    tool._publish_to_http = lambda *a, **k: posted.append(a)  # no debe llamarse
    ctx = types.SimpleNamespace(
        call_id="chan-9", get_config_value=lambda *a, **k: {},
        get_session=_noop_session, logger=None,
    )
    res = await tool.execute({"event_type": "CALL_SUMMARY", "summary": _summary()}, ctx)
    assert res["status"] == "error"
    assert posted == []


async def _noop_session():
    raise RuntimeError("no session")


# ------------------------------------------- event_id / payload (camino servidor)

def test_event_id_uses_seq():
    tool = _tool()
    assert tool._generate_event_id("chan-1", "CALL_SUMMARY", {"summary": _summary(seq=7)}) == "chan-1:CALL_SUMMARY:7"
    assert tool._generate_event_id("chan-1", "CALL_SUMMARY", {"summary": _summary(seq=8)}) != "chan-1:CALL_SUMMARY:7"


def test_event_id_other_events_keep_md5():
    assert _tool()._generate_event_id("chan-1", "TRANSITION", {}).startswith("evt_")


def test_summary_travels_verbatim_in_payload_root():
    ctx = types.SimpleNamespace(call_id="chan-9", agent_id=None, provider_name="deepgram")
    foto = _summary(seq=4, user_turns=3)
    payload = _tool()._build_payload({"event_type": "CALL_SUMMARY", "summary": foto}, ctx, None)
    assert payload["summary"] == foto
    assert payload["event_id"] == "chan-9:CALL_SUMMARY:4" and payload["event_type"] == "CALL_SUMMARY"


@pytest.mark.parametrize("bad", [None, "x", _summary() | {"schema_version": 2},
                                 _summary() | {"seq": 0}, _summary() | {"seq": "1"},
                                 _summary() | {"seq": True}])
def test_server_summary_validation_rejects(bad):
    with pytest.raises(ValueError):
        CallEventNotification._validate_server_summary(bad)


# --------------------------------------------------- publish_server_summary

def _server_ctx(config):
    return types.SimpleNamespace(
        call_id="chan-9", agent_id=None, provider_name="deepgram", logger=None,
        get_config_value=lambda key, default=None: config.get(key, default),
        get_session=_noop_session,
    )


_HTTP_CFG = {"tools.CallEventNotification": {
    "queue_backend": "http",
    "enabled_event_types": ["CALL_SUMMARY"],
    "http": {"urls_by_event_type": {"CALL_SUMMARY": "http://x/ingest"}, "timeout_secs": 1},
}}


async def test_publish_server_summary_ok_posts_verbatim():
    tool = _tool()
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["url"] = url; captured["payload"] = payload
        return types.SimpleNamespace(text='{"id":"1"}')

    tool._sync_http_post_json = fake_post
    res = await tool.publish_server_summary(_summary(seq=5), _server_ctx(_HTTP_CFG))
    assert res["status"] == "ok"
    assert captured["url"] == "http://x/ingest"
    assert captured["payload"]["summary"]["seq"] == 5
    assert captured["payload"]["event_id"] == "chan-9:CALL_SUMMARY:5"


async def test_publish_server_summary_ignored_when_type_disabled():
    cfg = {"tools.CallEventNotification": {"queue_backend": "http", "enabled_event_types": ["TRANSITION"],
                                           "http": {}}}
    res = await _tool().publish_server_summary(_summary(), _server_ctx(cfg))
    assert res["status"] == "ignored"


async def test_publish_server_summary_error_on_post_failure():
    tool = _tool()
    def boom(*a, **k):
        raise RuntimeError("5xx")
    tool._sync_http_post_json = boom
    res = await tool.publish_server_summary(_summary(), _server_ctx(_HTTP_CFG))
    assert res["status"] == "error"


async def test_publish_server_summary_error_on_invalid():
    res = await _tool().publish_server_summary({"schema_version": 2}, _server_ctx(_HTTP_CFG))
    assert res["status"] == "error"


# --------------------------------------------------- provider: protocolo + ACK
from src.providers.deepgram import DeepgramProvider


class _WS:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send(self, payload):
        if self.fail:
            raise ConnectionError("ws closed")
        self.sent.append(json.loads(payload) if isinstance(payload, str) else payload)


def _provider(ws=None):
    async def on_event(_e):
        pass
    p = DeepgramProvider({}, types.SimpleNamespace(), on_event)
    p.call_id = "chan-9"
    p.websocket = ws or _WS()
    return p


async def test_protocol_call_summary_publishes_with_captured_call_id():
    p = _provider()
    published = {}

    class FakeTool:
        async def publish_server_summary(self, summary, ctx):
            published["call_id"] = ctx.call_id
            published["seq"] = summary["seq"]
            return {"status": "ok"}

    p.tool_adapter = types.SimpleNamespace(registry=types.SimpleNamespace(
        get=lambda name: FakeTool() if name == "CallEventNotification" else None))
    ctx = p._function_call_context()
    p.call_id = None                       # stop_session borró el call_id
    await p._handle_call_summary({"type": "CallSummary", "summary": _summary(seq=6)}, ctx)
    assert published == {"call_id": "chan-9", "seq": 6}
    ack = p.websocket.sent[-1]
    assert ack == {"type": "CallSummaryAck", "seq": 6, "status": "ok"}


@pytest.mark.parametrize("status", ["ok", "error", "ignored"])
async def test_ack_reflects_publish_status(status):
    p = _provider()

    class FakeTool:
        async def publish_server_summary(self, summary, ctx):
            return {"status": status}

    p.tool_adapter = types.SimpleNamespace(registry=types.SimpleNamespace(get=lambda name: FakeTool()))
    await p._handle_call_summary({"type": "CallSummary", "summary": _summary(seq=3)}, p._function_call_context())
    assert p.websocket.sent[-1] == {"type": "CallSummaryAck", "seq": 3, "status": status}


async def test_ack_on_closed_ws_does_not_raise():
    p = _provider(ws=_WS(fail=True))

    class FakeTool:
        async def publish_server_summary(self, summary, ctx):
            return {"status": "ok"}

    p.tool_adapter = types.SimpleNamespace(registry=types.SimpleNamespace(get=lambda name: FakeTool()))
    # no debe propagar la excepción del WS
    await p._handle_call_summary({"type": "CallSummary", "summary": _summary()}, p._function_call_context())


async def test_registry_missing_tool_acks_error():
    p = _provider()
    p.tool_adapter = types.SimpleNamespace(registry=types.SimpleNamespace(get=lambda name: None))
    await p._handle_call_summary({"type": "CallSummary", "summary": _summary(seq=2)}, p._function_call_context())
    assert p.websocket.sent[-1] == {"type": "CallSummaryAck", "seq": 2, "status": "error"}


# --------------------------------------------------- function-call context (otras tools)

def test_context_snapshot_is_immune_to_call_id_reset():
    p = _provider()
    ctx = p._function_call_context()
    p.call_id = None
    assert ctx["call_id"] == "chan-9" and ctx["websocket"] is p.websocket


async def test_tracked_task_logs_on_crash():
    p = _provider()

    async def boom():
        raise RuntimeError("x")

    t = p._track_task(asyncio.create_task(boom()), "call_summary")
    assert t in p._bg_tasks
    with pytest.raises(RuntimeError):
        await t
    await asyncio.sleep(0)
    assert t not in p._bg_tasks


# ---------------------------------------------- config real (yaml en disco)

def test_yaml_lists_call_summary_in_both_lists():
    import yaml, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config" / "ai-agent.yaml").read_text())["tools"]["CallEventNotification"]
    assert "CALL_SUMMARY" in cfg["http"]["urls_by_event_type"]
    assert "CALL_SUMMARY" in cfg["enabled_event_types"]

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


_TS_LEVEL_RE = re.compile(
    r"^\s*(?P<ts>\d{4}-\d\d-\d\dT[0-9:.]+Z)\s*\[\s*(?P<level>[a-zA-Z]+)\s*\]\s*(?P<rest>.*)$"
)

_LOGGER_RE = re.compile(r"^(?P<msg>.*?)\s*\[(?P<logger>[^\]]+)\]\s*(?P<kv>.*)$")

_KEY_RE = re.compile(r"\b(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)=(?P<val>\"[^\"]*\"|'[^']*'|[^\s]+)")


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        # Example: 2025-12-25T21:23:32.755042Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _KEY_RE.finditer(text or ""):
        k = m.group("key")
        v = m.group("val")
        if v and len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        out[k] = v
    return out


def _first_present(d: Dict[str, str], keys: Iterable[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


@dataclass(frozen=True)
class LogEvent:
    ts: Optional[datetime]
    level: str
    msg: str
    component: Optional[str]
    call_id: Optional[str]
    provider: Optional[str]
    context: Optional[str]
    pipeline: Optional[str]
    category: str
    milestone: bool
    raw: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts.isoformat() if self.ts else None,
            "level": self.level,
            "msg": self.msg,
            "component": self.component,
            "call_id": self.call_id,
            "provider": self.provider,
            "context": self.context,
            "pipeline": self.pipeline,
            "category": self.category,
            "milestone": self.milestone,
            "raw": self.raw,
        }


def classify_event(msg: str, component: Optional[str]) -> Tuple[str, bool]:
    text = (msg or "").lower()
    comp = (component or "").lower()

    # Milestones (info-level) + categories
    if "audio profile resolved and applied" in text:
        return "audio", True
    if "openai session.updated ack received" in text or "session.updated ack received" in text:
        return "provider", True
    if "rtp server started for externalmedia transport" in text or "externalmedia channel created" in text:
        return "transport", True
    if "call cleanup completed" in text or text.startswith("cleaning up call"):
        return "call", True

    # Categories (non-milestone)
    if "externalmedia" in text or "rtp " in text or "ari " in text or "audiosocket" in text:
        return "transport", False
    if "vad" in text or "talk detect" in text or "barge" in text:
        return "vad", False
    if "tool" in text or "mcp" in text:
        return "tools", False
    if "encode" in text or "resample" in text or "normalizer" in text or "gating" in text:
        return "audio", False
    if "provider" in text or comp.startswith("src.providers") or "realtime" in text:
        return "provider", False
    if "config" in text or "configuration" in text:
        return "config", False

    return "call", False


def parse_log_line(line: str) -> Optional[Tuple[LogEvent, Dict[str, str]]]:
    raw = strip_ansi(line.rstrip("\n"))
    if not raw.strip():
        return None

    m = _TS_LEVEL_RE.match(raw)
    if not m:
        # Best-effort: return as "unknown" info
        msg = raw.strip()
        category, milestone = classify_event(msg, None)
        event = LogEvent(
            ts=None,
            level="info",
            msg=msg,
            component=None,
            call_id=None,
            provider=None,
            context=None,
            pipeline=None,
            category=category,
            milestone=milestone,
            raw=raw,
        )
        return event, {}

    ts_s = m.group("ts")
    level = (m.group("level") or "info").lower()
    rest = m.group("rest") or ""

    component = None
    msg = rest.strip()
    kv_str = ""

    m2 = _LOGGER_RE.match(rest)
    if m2:
        msg = (m2.group("msg") or "").strip()
        component = (m2.group("logger") or "").strip()
        kv_str = m2.group("kv") or ""

    kv = _parse_kv(kv_str)
    call_id = _first_present(kv, ("call_id", "caller_channel_id", "channel_id"))
    provider = _first_present(kv, ("provider", "provider_name"))
    context = _first_present(kv, ("context", "context_name"))
    pipeline = _first_present(kv, ("pipeline", "pipeline_name"))
    if not component:
        component = kv.get("component") or None

    category, milestone = classify_event(msg, component)
    return (
        LogEvent(
            ts=_parse_ts(ts_s),
            level=level,
            msg=msg,
            component=component,
            call_id=call_id,
            provider=provider,
            context=context,
            pipeline=pipeline,
            category=category,
            milestone=milestone,
            raw=raw,
        ),
        kv,
    )


def should_hide_payload(event: LogEvent) -> bool:
    # Hide large transcript/control payloads that swamp troubleshooting
    t = event.raw.lower()
    if "provider control event" in t and "provider_event" in t:
        return True
    if "transcript" in t and "provider_event" in t:
        return True
    if "has_prompt" in t and "has_config" in t:
        return True
    return False

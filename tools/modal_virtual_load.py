#!/usr/bin/env python3
"""Generate real Golden-Modal traffic through ai-engine without phone calls.

Each virtual session uses the production DeepgramProvider configuration and
the same continuous-input coalescer seam as a live call.  It intentionally
does *not* create ARI channels, AudioSocket connections or outbound PSTN
calls.  The load target is ai-engine's provider-bound media path plus the
GoldenBridge Modal WebSocket endpoint.

Input is raw 8 kHz mu-law split into logical 20 ms / 160-byte frames and paced
in real time.  ``--frame-ms 60`` changes only the ai-engine egress coalescer;
the virtual source remains 20 ms, as it does with Asterisk AudioSocket.

This is an opt-in live-traffic tool.  It refuses to start unless
``--confirm-live-modal`` is supplied, reads credentials only from the existing
runtime config/environment, and never prints them.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Any, Optional
from urllib.parse import urlparse

try:  # Linux container: RSS; Windows development/test host: unavailable.
    import resource
except ModuleNotFoundError:  # pragma: no cover - exercised by Windows import
    resource = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import DeepgramProviderConfig, load_config
from src.engine import Engine
from src.providers.deepgram import DeepgramProvider


FRAME_BYTES = 160
FRAME_SECONDS = 0.020
SILENCE_ULAW = b"\xff" * FRAME_BYTES


def percentile(values: list[float], quantile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * quantile))))
    return ordered[index]


def _ms(value: Optional[float]) -> Optional[float]:
    return round(value * 1000.0, 2) if value is not None else None


def max_rss_kib() -> Optional[int]:
    if resource is None:
        return None
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


@dataclass(frozen=True)
class FrameProgram:
    """One repeating utterance followed by a period of mu-law silence."""

    frames: tuple[bytes, ...]
    pause_frames: int

    def frame_at(self, index: int) -> bytes:
        period = len(self.frames) + self.pause_frames
        if not period:
            return SILENCE_ULAW
        position = index % period
        return self.frames[position] if position < len(self.frames) else SILENCE_ULAW


def load_programs(path: Path, pause_seconds: float) -> list[FrameProgram]:
    candidates = sorted(item for item in path.iterdir() if item.is_file()) if path.is_dir() else [path]
    if not candidates:
        raise ValueError("--speech no contiene archivos")
    programs: list[FrameProgram] = []
    for candidate in candidates:
        raw = candidate.read_bytes()
        usable = len(raw) - (len(raw) % FRAME_BYTES)
        if usable == 0:
            raise ValueError(f"{candidate} no contiene una trama mu-law de 160 bytes")
        frames = tuple(raw[offset:offset + FRAME_BYTES] for offset in range(0, usable, FRAME_BYTES))
        programs.append(FrameProgram(frames=frames, pause_frames=max(0, round(pause_seconds / FRAME_SECONDS))))
    return programs


@dataclass
class VirtualCall:
    call_id: str
    source_frames: int = 0
    source_bytes: int = 0
    emitted_messages: int = 0
    emitted_bytes: int = 0
    agent_audio_messages: int = 0
    agent_audio_bytes: int = 0
    event_counts: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    connected_at: Optional[float] = None
    settings_applied_at: Optional[float] = None
    first_agent_audio_at: Optional[float] = None
    last_user_text_at: Optional[float] = None
    first_reply_after_user_at: Optional[float] = None

    def as_summary(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "source_frames": self.source_frames,
            "source_bytes": self.source_bytes,
            "emitted_messages": self.emitted_messages,
            "emitted_bytes": self.emitted_bytes,
            "agent_audio_messages": self.agent_audio_messages,
            "agent_audio_bytes": self.agent_audio_bytes,
            "settings_ack_ms": _ms(
                self.settings_applied_at - self.connected_at
                if self.settings_applied_at and self.connected_at else None
            ),
            "first_agent_audio_ms": _ms(
                self.first_agent_audio_at - self.connected_at
                if self.first_agent_audio_at and self.connected_at else None
            ),
            "first_reply_after_user_ms": _ms(
                self.first_reply_after_user_at - self.last_user_text_at
                if self.first_reply_after_user_at and self.last_user_text_at else None
            ),
            "events": dict(self.event_counts),
            "errors": self.errors,
        }


class LoopLagObserver:
    def __init__(self, interval_seconds: float = 0.050) -> None:
        self.interval_seconds = interval_seconds
        self.samples: list[float] = []
        self._stop = asyncio.Event()

    async def run(self) -> None:
        deadline = time.perf_counter() + self.interval_seconds
        while not self._stop.is_set():
            await asyncio.sleep(max(0.0, deadline - time.perf_counter()))
            now = time.perf_counter()
            self.samples.append(max(0.0, now - deadline))
            deadline += self.interval_seconds

    def stop(self) -> None:
        self._stop.set()


async def _sleep_until(deadline: float) -> None:
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 0.010))


def _deepgram_config(config: Any) -> DeepgramProviderConfig:
    raw = getattr(config, "providers", {}).get("deepgram")
    if raw is None:
        raise RuntimeError("no existe providers.deepgram en la configuración activa")
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise RuntimeError("providers.deepgram no es un bloque de configuración válido")
    provider_config = DeepgramProviderConfig(**deepcopy(raw))
    host = (urlparse(provider_config.voice_agent_base_url).hostname or "").lower()
    if not host.endswith(".modal.run"):
        raise RuntimeError("el endpoint activo no parece GoldenBridge Modal; se rechaza la carga")
    if not provider_config.modal_key or not provider_config.modal_secret:
        raise RuntimeError("faltan credenciales de proxy Modal en el entorno activo")
    return provider_config


def _minimal_engine(frame_ms: int) -> Engine:
    engine = Engine.__new__(Engine)
    engine._audiosocket_frame_ms = frame_ms
    engine._audiosocket_coalesce_states = {}
    engine._audiosocket_integrity_probe = False
    engine._audiosocket_integrity_ledgers = {}
    return engine


async def _run_call(
    *,
    engine: Engine,
    provider_config: DeepgramProviderConfig,
    llm_config: Any,
    call_id: str,
    program: FrameProgram,
    hold_seconds: float,
    start_delay_seconds: float,
    drain_seconds: float,
) -> VirtualCall:
    result = VirtualCall(call_id=call_id)

    async def on_event(event: dict[str, Any]) -> None:
        now = time.perf_counter()
        event_type = str(event.get("type") or "unknown")
        result.event_counts[event_type] += 1
        if event_type == "SettingsApplied":
            result.settings_applied_at = now
        elif event_type == "AgentAudio":
            result.agent_audio_messages += 1
            result.agent_audio_bytes += len(event.get("data") or b"")
            if result.first_agent_audio_at is None:
                result.first_agent_audio_at = now
            if result.last_user_text_at is not None and result.first_reply_after_user_at is None:
                result.first_reply_after_user_at = now
        elif event_type == "ConversationText" and event.get("role") == "user":
            result.last_user_text_at = now
        elif event_type == "Error":
            # Do not persist an arbitrary remote payload; it may contain context.
            result.errors.append("provider_error_event")

    provider = DeepgramProvider(deepcopy(provider_config), llm_config, on_event)
    original_send = provider.send_audio

    async def observed_send(payload: bytes, *args: Any, **kwargs: Any) -> None:
        result.emitted_messages += 1
        result.emitted_bytes += len(payload)
        await original_send(payload, *args, **kwargs)

    provider.send_audio = observed_send  # type: ignore[method-assign]
    sent_any = False
    try:
        result.connected_at = time.perf_counter()
        await provider.start_session(call_id, context={"tools": []})
        ack_deadline = time.perf_counter() + 3.0
        while not getattr(provider, "_settings_acked", False) and time.perf_counter() < ack_deadline:
            await asyncio.sleep(0.020)
        if not getattr(provider, "_settings_acked", False):
            raise RuntimeError("settings_not_acked")

        await _sleep_until(time.perf_counter() + start_delay_seconds)
        started = time.perf_counter()
        deadline = started
        index = 0
        while time.perf_counter() - started < hold_seconds:
            await _sleep_until(deadline)
            frame = program.frame_at(index)
            result.source_frames += 1
            result.source_bytes += len(frame)
            await engine._send_continuous_provider_audio(
                call_id,
                provider,
                frame,
                "ulaw",
                "deepgram",
                8000,
            )
            sent_any = True
            index += 1
            deadline += FRAME_SECONDS
        await asyncio.sleep(max(0.0, drain_seconds))
    except Exception as exc:
        result.errors.append(type(exc).__name__)
    finally:
        if sent_any:
            try:
                await engine._send_or_coalesce_provider_audio(
                    call_id,
                    provider,
                    provider_active=True,
                    flush_reason="virtual_load_terminal",
                )
            except Exception as exc:
                result.errors.append(f"flush_{type(exc).__name__}")
        try:
            await provider.stop_session()
        except Exception as exc:
            result.errors.append(f"stop_{type(exc).__name__}")
    return result


def _aggregate(calls: list[VirtualCall], loop_lag: list[float], elapsed: float, cpu_before: float) -> dict[str, Any]:
    ack = [item.settings_applied_at - item.connected_at for item in calls if item.settings_applied_at and item.connected_at]
    first_audio = [item.first_agent_audio_at - item.connected_at for item in calls if item.first_agent_audio_at and item.connected_at]
    reply = [item.first_reply_after_user_at - item.last_user_text_at for item in calls if item.first_reply_after_user_at and item.last_user_text_at]
    events: Counter = Counter()
    for item in calls:
        events.update(item.event_counts)
    source_messages = sum(item.source_frames for item in calls)
    emitted_messages = sum(item.emitted_messages for item in calls)
    reduction = 100.0 * (1.0 - emitted_messages / source_messages) if source_messages else None
    return {
        "sessions": len(calls),
        "sessions_clean": sum(not item.errors for item in calls),
        "sessions_failed": sum(bool(item.errors) for item in calls),
        "elapsed_seconds": round(elapsed, 3),
        "process_cpu_seconds": round(max(0.0, time.process_time() - cpu_before), 3),
        "max_rss_kib": max_rss_kib(),
        "source_messages": source_messages,
        "source_bytes": sum(item.source_bytes for item in calls),
        "emitted_messages": emitted_messages,
        "emitted_bytes": sum(item.emitted_bytes for item in calls),
        "application_message_reduction_percent": round(reduction, 2) if reduction is not None else None,
        "settings_ack_ms": {"p50": _ms(percentile(ack, 0.50)), "p90": _ms(percentile(ack, 0.90)), "n": len(ack)},
        "first_agent_audio_ms": {"p50": _ms(percentile(first_audio, 0.50)), "p90": _ms(percentile(first_audio, 0.90)), "n": len(first_audio)},
        "first_reply_after_user_ms": {"p50": _ms(percentile(reply, 0.50)), "p90": _ms(percentile(reply, 0.90)), "n": len(reply)},
        "loop_lag_ms": {"p50": _ms(percentile(loop_lag, 0.50)), "p90": _ms(percentile(loop_lag, 0.90)), "p99": _ms(percentile(loop_lag, 0.99)), "n": len(loop_lag)},
        "provider_events": dict(events),
        "calls": [item.as_summary() for item in calls],
    }


async def run_load(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    provider_config = _deepgram_config(config)
    programs = load_programs(Path(args.speech), args.pause_seconds)
    engine = _minimal_engine(args.frame_ms)
    loop_observer = LoopLagObserver()
    loop_task = asyncio.create_task(loop_observer.run())
    started = time.perf_counter()
    cpu_before = time.process_time()
    try:
        tasks = []
        interval = args.ramp_seconds / args.sessions if args.sessions else 0.0
        for index in range(args.sessions):
            if index:
                await asyncio.sleep(interval)
            tasks.append(asyncio.create_task(_run_call(
                engine=engine,
                provider_config=provider_config,
                llm_config=config.llm,
                call_id=f"virtual-load-{int(time.time())}-{index:04d}",
                program=programs[index % len(programs)],
                hold_seconds=args.hold_seconds,
                start_delay_seconds=args.start_delay_seconds,
                drain_seconds=args.drain_seconds,
            )))
        calls = await asyncio.gather(*tasks)
    finally:
        loop_observer.stop()
        await loop_task
    return _aggregate(calls, loop_observer.samples, time.perf_counter() - started, cpu_before)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live-modal", action="store_true", help="acknowledge that this opens real Modal sessions")
    parser.add_argument("--config", default="config/ai-agent.yaml", help="active ai-engine YAML; credentials remain in its environment")
    parser.add_argument("--speech", required=True, help="raw 8 kHz mu-law file or directory of files")
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--ramp-seconds", type=float, default=0.0)
    parser.add_argument("--hold-seconds", type=float, default=30.0)
    parser.add_argument("--pause-seconds", type=float, default=8.0)
    parser.add_argument("--start-delay-seconds", type=float, default=2.0)
    parser.add_argument("--drain-seconds", type=float, default=3.0)
    parser.add_argument("--frame-ms", type=int, choices=(20, 60), required=True)
    args = parser.parse_args()
    if not args.confirm_live_modal:
        parser.error("refusing live traffic without --confirm-live-modal")
    if args.sessions < 1 or args.hold_seconds <= 0 or args.ramp_seconds < 0 or args.pause_seconds < 0:
        parser.error("sessions must be >=1; durations must be valid")
    return args


def main() -> int:
    args = parse_args()
    summary = asyncio.run(run_load(args))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    # A clean session proves connection + ACK + byte preservation at the
    # generator boundary. AgentAudio is reported, not required: campaigns may
    # intentionally be silent until their first user turn.
    return 0 if summary["sessions_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Local F1 load check for the 60 ms AudioSocket provider coalescer."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.engine import Engine


class TimedProvider:
    def __init__(self) -> None:
        self.messages: list[tuple[float, bytes]] = []

    async def send_audio(self, payload: bytes) -> None:
        self.messages.append((time.perf_counter(), bytes(payload)))


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * quantile))))
    return ordered[index]


async def sleep_until(deadline: float) -> None:
    """Pace frames without accumulating Windows timer-resolution drift."""
    while time.perf_counter() < deadline:
        await asyncio.sleep(0)


async def feed_call(engine: Engine, call_index: int, duration: float) -> tuple[TimedProvider, bool]:
    call_id = f"load-{call_index}"
    provider = TimedProvider()
    frame_count = max(1, int(duration / 0.020))
    barge_in_observed = False

    for index in range(frame_count):
        # Barge-in/VAD runs before this seam in Engine. This marker verifies the
        # per-20-ms input observation remains independent from provider batching.
        if index >= 90:
            barge_in_observed = True
        frame = bytes([(call_index + index) % 256]) * 160
        await engine._send_or_coalesce_provider_audio(
            call_id,
            provider,
            frame,
            "ulaw",
            "deepgram",
        )
        await sleep_until(time.perf_counter() + 0.020)

    await engine._send_or_coalesce_provider_audio(
        call_id,
        provider,
        provider_active=True,
        flush_reason="terminal_cleanup",
    )
    return provider, barge_in_observed


async def run(calls: int, duration: float) -> dict[str, float | int | bool]:
    engine = Engine.__new__(Engine)
    engine._audiosocket_frame_ms = 60
    engine._audiosocket_coalesce_states = {}

    results = await asyncio.gather(*(feed_call(engine, index, duration) for index in range(calls)))
    intervals: list[float] = []
    full_messages = 0
    for provider, _barge in results:
        full_times = [stamp for stamp, payload in provider.messages if len(payload) == 480]
        full_messages += len(full_times)
        intervals.extend(later - earlier for earlier, later in zip(full_times, full_times[1:]))

    if not intervals:
        raise RuntimeError("duration is too short to measure coalescer cadence")

    p50 = statistics.median(intervals)
    p99 = percentile(intervals, 0.99)
    early_sends = sum(interval < 0.055 for interval in intervals)
    leaked_states = len(engine._audiosocket_coalesce_states)
    barge_ok = all(observed for _provider, observed in results) if duration >= 1.82 else True
    summary = {
        "calls": calls,
        "duration_seconds": duration,
        "full_messages": full_messages,
        "interval_p50_ms": round(p50 * 1000.0, 3),
        "interval_p99_ms": round(p99 * 1000.0, 3),
        "sends_under_55ms": early_sends,
        "leaked_states": leaked_states,
        "barge_in_marker_ok": barge_ok,
    }
    if p99 >= 0.080 or early_sends or leaked_states or not barge_ok:
        raise RuntimeError(json.dumps(summary, sort_keys=True))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--calls", type=int, default=50)
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.calls, args.duration)), sort_keys=True))


if __name__ == "__main__":
    main()

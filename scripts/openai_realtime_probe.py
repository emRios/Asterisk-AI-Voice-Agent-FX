"""
Utility probe for validating OpenAI Realtime audio format/commit settings.

Usage (inside ai-engine container):
    docker-compose exec ai-engine python scripts/openai_realtime_probe.py

The script:
  * establishes a bare Realtime WebSocket session using OPENAI_API_KEY
  * negotiates pcm16 @ 24 kHz in session.update
  * sends a synthetic 440 Hz sine wave (roughly 1.2 s) in 20 ms chunks
  * commits the buffer and saves any provider audio deltas to probe_output.raw

Inspect the log output and (optionally) convert probe_output.raw to WAV:
    docker-compose exec ai-engine sox -t raw -b 16 -e signed-integer -r 24000 -c 1 probe_output.raw probe_output.wav
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import pathlib
import sys
from typing import AsyncIterator

import websockets

REALTIME_URL = os.environ.get("OPENAI_REALTIME_URL", "wss://api.openai.com/v1/realtime")
REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
API_KEY = os.environ.get("OPENAI_API_KEY")
VOICE = os.environ.get("OPENAI_REALTIME_VOICE", "alloy")
SAMPLE_RATE = 24_000
BYTES_PER_SAMPLE = 2  # PCM16
FRAME_MS = 20

OUTPUT_FILE = pathlib.Path("probe_output.raw")


def require_api_key() -> str:
    if not API_KEY:
        print("OPENAI_API_KEY env var is required", file=sys.stderr)
        sys.exit(1)
    return API_KEY


def generate_tone(duration_sec: float = 1.2, freq_hz: float = 440.0, amplitude: float = 0.4) -> bytes:
    """Generate a PCM16 sine wave at SAMPLE_RATE."""
    total_samples = int(duration_sec * SAMPLE_RATE)
    data = bytearray()
    for n in range(total_samples):
        value = amplitude * math.sin(2 * math.pi * freq_hz * (n / SAMPLE_RATE))
        sample = int(max(-1.0, min(1.0, value)) * 32767)
        data.extend(sample.to_bytes(2, byteorder="little", signed=True))
    return bytes(data)


def iter_frames(pcm: bytes) -> AsyncIterator[bytes]:
    frame_size = int(SAMPLE_RATE * (FRAME_MS / 1000.0)) * BYTES_PER_SAMPLE
    for offset in range(0, len(pcm), frame_size):
        yield pcm[offset : offset + frame_size]


async def main() -> None:
    require_api_key()

    pcm = generate_tone()
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    url = REALTIME_URL
    if "model=" not in url:
        connector = "&" if "?" in url else "?"
        url = f"{url}{connector}model={REALTIME_MODEL}"

    async with websockets.connect(url, extra_headers=headers) as ws:
        session_update = {
            "type": "session.update",
            "session": {
                "modalities": ["audio", "text"],
                "voice": VOICE,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
            },
        }
        await ws.send(json.dumps(session_update))
        print("sent session.update")

        # send a response request so we force the model to speak
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "event_id": "probe-greeting",
                    "response": {
                        "modalities": ["audio", "text"],
                        "instructions": "Please greet the caller clearly.",
                    },
                }
            )
        )
        print("sent response.create (probe greeting)")

        for frame in iter_frames(pcm):
            payload = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame).decode("ascii"),
            }
            await ws.send(json.dumps(payload))
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        print("sent append+commit (tone)")

        OUTPUT_FILE.unlink(missing_ok=True)
        async for message in ws:
            data = json.loads(message)
            msg_type = data.get("type")
            print("recv", msg_type)
            if msg_type == "error":
                print("error payload:", data)
                break
            if msg_type == "response.delta":
                delta = data.get("delta", {})
                if delta.get("type") == "output_audio.delta":
                    audio = base64.b64decode(delta["audio"])
                    with OUTPUT_FILE.open("ab") as fh:
                        fh.write(audio)
            if msg_type == "response.completed":
                break

        print("probe finished. output audio written to", OUTPUT_FILE.resolve())


if __name__ == "__main__":
    asyncio.run(main())

"""
Enhanced VAD Manager - integrates WebRTC VAD and energy-based detection under a feature flag.
"""

from __future__ import annotations

import asyncio
import audioop
from dataclasses import dataclass
from typing import Dict, Optional

import structlog
from prometheus_client import Counter, Gauge, Histogram

try:
    import webrtcvad  # pyright: ignore[reportMissingImports]
    WEBRTC_VAD_AVAILABLE = True
except ImportError:  # pragma: no cover
    webrtcvad = None  # type: ignore
    WEBRTC_VAD_AVAILABLE = False

logger = structlog.get_logger(__name__)


# Prometheus metrics ---------------------------------------------------------
_VAD_FRAMES_TOTAL = Counter(
    "ai_agent_vad_frames_total",
    "Total audio frames processed by Enhanced VAD",
    labelnames=("call_id", "result"),
)

_VAD_CONFIDENCE_HISTOGRAM = Histogram(
    "ai_agent_vad_confidence",
    "Enhanced VAD confidence distribution",
    labelnames=("call_id",),
    buckets=(0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
)

_VAD_ADAPTIVE_THRESHOLD = Gauge(
    "ai_agent_vad_adaptive_threshold",
    "Enhanced VAD adaptive energy threshold",
    labelnames=("call_id",),
)


@dataclass
class VADResult:
    is_speech: bool
    confidence: float
    energy_level: int
    webrtc_result: bool
    frame_duration_ms: int = 20


class AdaptiveThreshold:
    def __init__(self, base_threshold: int, adaptation_rate: float = 0.1, max_samples: int = 100):
        self.base_threshold = base_threshold
        self.current_threshold = base_threshold
        self.adaptation_rate = adaptation_rate
        self.max_samples = max_samples
        self._noise_samples: list[int] = []
        self.noise_floor: float = 0.0

    def update(self, energy: int, is_speech: bool) -> None:
        if is_speech:
            return
        if len(self._noise_samples) >= self.max_samples:
            return
        self._noise_samples.append(energy)
        if len(self._noise_samples) < 10:
            return
        self.noise_floor = sum(self._noise_samples) / len(self._noise_samples)
        target_threshold = max(self.base_threshold, int(self.noise_floor * 2.5))
        self.current_threshold = int(
            self.current_threshold * (1 - self.adaptation_rate) + target_threshold * self.adaptation_rate
        )

    def get_threshold(self) -> int:
        return max(self.current_threshold, self.base_threshold)

    def reset(self) -> None:
        self.current_threshold = self.base_threshold
        self._noise_samples.clear()
        self.noise_floor = 0.0


class EnhancedVADManager:
    """Feature-flagged enhanced VAD manager used for barge-in heuristics."""

    def __init__(
        self,
        *,
        energy_threshold: int = 1500,
        confidence_threshold: float = 0.6,
        adaptive_threshold_enabled: bool = False,
        noise_adaptation_rate: float = 0.1,
        webrtc_aggressiveness: int = 1,
        min_speech_frames: int = 2,
        max_silence_frames: int = 15,
    ) -> None:
        self.energy_threshold = energy_threshold
        self.confidence_threshold = confidence_threshold
        self.adaptive_threshold_enabled = adaptive_threshold_enabled
        self.webrtc_aggressiveness = webrtc_aggressiveness
        self.min_speech_frames = max(1, min_speech_frames)
        self.max_silence_frames = max(1, max_silence_frames)

        self.webrtc_vad = None
        if WEBRTC_VAD_AVAILABLE:
            try:
                self.webrtc_vad = webrtcvad.Vad(self.webrtc_aggressiveness)
                logger.info("Enhanced VAD - WebRTC initialized", aggressiveness=self.webrtc_aggressiveness)
            except Exception:
                logger.warning("Enhanced VAD - WebRTC initialization failed", exc_info=True)
                self.webrtc_vad = None
        else:
            logger.warning("Enhanced VAD - WebRTC module not available")

        self.adaptive_threshold = AdaptiveThreshold(
            base_threshold=self.energy_threshold,
            adaptation_rate=noise_adaptation_rate,
        )
        self._speech_frames = 0
        self._silence_frames = 0
        self._is_speaking = False

        self._call_stats: Dict[str, Dict[str, float]] = {}
        self._lock = asyncio.Lock()

    async def process_frame(self, call_id: str, audio_frame_pcm16: bytes) -> VADResult:
        if len(audio_frame_pcm16) < 320:
            audio_frame_pcm16 = audio_frame_pcm16.ljust(320, b"\x00")
        energy = audioop.rms(audio_frame_pcm16, 2)
        webrtc_result = False
        if self.webrtc_vad:
            try:
                webrtc_result = self.webrtc_vad.is_speech(audio_frame_pcm16, 8000)
            except Exception:
                logger.debug("Enhanced VAD - WebRTC processing error", exc_info=True)

        threshold = self.adaptive_threshold.get_threshold() if self.adaptive_threshold_enabled else self.energy_threshold
        energy_result = energy >= threshold

        if self.adaptive_threshold_enabled:
            self.adaptive_threshold.update(energy, webrtc_result or energy_result)

        final_speech = self._smooth_frames(webrtc_result or energy_result)
        confidence = self._calc_confidence(webrtc_result, energy_result, energy, threshold)

        result = VADResult(
            is_speech=final_speech,
            confidence=confidence,
            energy_level=energy,
            webrtc_result=webrtc_result,
        )

        self._update_metrics(call_id, result, threshold)
        return result

    def _smooth_frames(self, raw_speech: bool) -> bool:
        if raw_speech:
            self._speech_frames += 1
            self._silence_frames = 0
            if not self._is_speaking and self._speech_frames >= self.min_speech_frames:
                self._is_speaking = True
                logger.debug("Enhanced VAD - Speech started", frames=self._speech_frames)
        else:
            self._silence_frames += 1
            self._speech_frames = 0
            if self._is_speaking and self._silence_frames >= self.max_silence_frames:
                self._is_speaking = False
                logger.debug("Enhanced VAD - Speech ended", silence_frames=self._silence_frames)
        return self._is_speaking

    def _calc_confidence(self, webrtc_result: bool, energy_result: bool, energy: int, threshold: int) -> float:
        confidence = 0.0
        if webrtc_result:
            confidence += 0.4
        if energy_result:
            energy_ratio = min(energy / max(threshold, 1), 3.0)
            confidence += 0.4 * (energy_ratio / 3.0)
        if webrtc_result == energy_result:
            confidence += 0.2
        return min(confidence, 1.0)

    def _update_metrics(self, call_id: str, result: VADResult, threshold: int) -> None:
        try:
            label = "speech" if result.is_speech else "silence"
            _VAD_FRAMES_TOTAL.labels(call_id, label).inc()
            _VAD_CONFIDENCE_HISTOGRAM.labels(call_id).observe(result.confidence)
            _VAD_ADAPTIVE_THRESHOLD.labels(call_id).set(threshold)
        except Exception:
            logger.debug("Enhanced VAD - metrics update failed", exc_info=True)

    async def reset_call(self, call_id: str) -> None:
        async with self._lock:
            self._speech_frames = 0
            self._silence_frames = 0
            self._is_speaking = False
            self.adaptive_threshold.reset()
            self._call_stats.pop(call_id, None)

    @staticmethod
    def mu_law_to_pcm16(frame_ulaw: bytes) -> bytes:
        if len(frame_ulaw) == 0:
            return b""
        try:
            return audioop.ulaw2lin(frame_ulaw, 2)
        except Exception:
            logger.debug("Enhanced VAD - ulaw to PCM16 conversion failed", exc_info=True)
            return b""

import math
import array
import pytest

from src.core.streaming_playback_manager import StreamingPlaybackManager


def _rms_pcm16(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    buf = array.array('h')
    buf.frombytes(pcm)
    if len(buf) == 0:
        return 0.0
    acc = 0.0
    for s in buf:
        acc += float(s) * float(s)
    return math.sqrt(acc / float(len(buf)))


def _make_sine_pcm16(amplitude: int, samples: int, freq: float = 440.0, rate: int = 8000) -> bytes:
    # Simple 16-bit mono sine wave
    buf = array.array('h')
    two_pi_f = 2.0 * math.pi * freq
    for n in range(samples):
        t = n / float(rate)
        val = int(round(amplitude * math.sin(two_pi_f * t)))
        # Clip to int16
        if val > 32767:
            val = 32767
        elif val < -32768:
            val = -32768
        buf.append(val)
    return buf.tobytes()


def _make_manager():
    class Dummy:
        pass
    # Only using _apply_normalizer; other deps are not exercised
    return StreamingPlaybackManager(
        session_store=Dummy(),
        ari_client=Dummy(),
        conversation_coordinator=None,
        fallback_playback_manager=None,
        streaming_config={
            'normalizer': {
                'enabled': True,
                'target_rms': 1400,
                'max_gain_db': 12.0,
            }
        },
        audio_transport="audiosocket",
    )


def test_apply_normalizer_increases_rms_within_bound():
    mgr = _make_manager()
    # Low amplitude sine ~ RMS ~= amp/sqrt(2)
    pcm = _make_sine_pcm16(amplitude=300, samples=800)  # 100ms at 8k
    before = _rms_pcm16(pcm)
    out = mgr._apply_normalizer(pcm, target_rms=1400, max_gain_db=12.0)
    after = _rms_pcm16(out)

    # Expect increase, but capped by 12 dB (~4x)
    assert after > before
    assert after <= before * (10 ** (12.0 / 20.0)) + 1.0


def test_apply_normalizer_target_zero_no_change():
    mgr = _make_manager()
    pcm = _make_sine_pcm16(amplitude=800, samples=800)
    out = mgr._apply_normalizer(pcm, target_rms=0, max_gain_db=12.0)
    assert out == pcm


def test_apply_normalizer_invalid_pcm_returns_original():
    mgr = _make_manager()
    pcm = b"\x01"
    out = mgr._apply_normalizer(pcm, target_rms=1400, max_gain_db=12.0)
    assert out == pcm


def test_apply_normalizer_respects_small_max_gain():
    mgr = _make_manager()
    pcm = _make_sine_pcm16(amplitude=1000, samples=800)
    before = _rms_pcm16(pcm)
    out = mgr._apply_normalizer(pcm, target_rms=1200, max_gain_db=0.5)  # ~+0.5 dB max
    after = _rms_pcm16(out)

    # Expected RMS roughly before * 10^(0.5/20) with some integer rounding tolerance
    expected = before * (10 ** (0.5 / 20.0))
    assert after == pytest.approx(expected, rel=0.05)


def _reference_normalizer(pcm: bytes, target_rms: int, max_gain_db: float) -> bytes:
    # Previous pure-Python implementation, kept as the reference: the numpy
    # version must return byte-identical audio.
    if not pcm or target_rms <= 0:
        return pcm
    buf = array.array('h')
    try:
        buf.frombytes(pcm)
    except ValueError:
        return pcm
    if len(buf) == 0:
        return pcm
    acc = 0.0
    for s in buf:
        acc += float(s) * float(s)
    rms = math.sqrt(acc / float(len(buf)))
    gain = min(float(target_rms) / max(1.0, rms), math.pow(10.0, float(max_gain_db) / 20.0))
    if gain <= 1.01:
        return pcm
    for i, s in enumerate(buf):
        y = float(s) * gain
        buf[i] = int(max(-32768.0, min(32767.0, y)))
    return buf.tobytes()


@pytest.mark.parametrize("amplitude,target_rms,max_gain_db", [
    (1400, 1400, 18.0),   # seed 3 differs by one LSB if computed in float32
    (5000, 3000, 40.0),   # seed 2 differs by one LSB if computed in float32
    (300, 1400, 9.0),     # production settings, quiet audio
    (20000, 1400, 9.0),   # loud audio, no gain
    (32767, 3000, 40.0),  # clipping path
])
def test_apply_normalizer_matches_reference_byte_for_byte(amplitude, target_rms, max_gain_db):
    import random
    mgr = _make_manager()
    for seed in range(50):
        rng = random.Random(seed)
        pcm = array.array('h', [rng.randint(-amplitude, amplitude) for _ in range(160)]).tobytes()
        assert mgr._apply_normalizer(pcm, target_rms, max_gain_db) == _reference_normalizer(pcm, target_rms, max_gain_db), seed

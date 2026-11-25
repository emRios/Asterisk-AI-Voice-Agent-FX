"""Start-up and fallback guarantees of the playback normalizer.

Two defects found in review are pinned here:

1. numpy used to be imported lazily inside the audio path, so the first
   normalized frame of a freshly started engine stalled the event loop while
   numpy loaded. numpy must already be loaded once ``src.engine`` is imported,
   and the first frame must not import anything.
2. On an image without numpy the normalizer silently returned the audio without
   gain. It must fall back to the pure-Python implementation and produce exactly
   the bytes the engine produced before numpy was introduced.

The start-up tests run in a fresh interpreter on purpose: inside the pytest
process numpy is already imported by other tests, so an in-process check would
pass even with the lazy import.

``GOLDEN_SHA256`` was produced by running ``_apply_normalizer`` from commit
15ac267 (the last version without numpy) over ``GOLDEN_CASES``. PCM is built
with ``array('h')`` in native byte order, like the engine does (little-endian
hosts).
"""

import array
import audioop
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import src.core.streaming_playback_manager as spm
from src.core.streaming_playback_manager import StreamingPlaybackManager

REPO_ROOT = Path(__file__).resolve().parents[1]


def _lcg_samples(n, amplitude, seed):
    """Deterministic pseudo-random PCM16 samples (integer-only, platform independent)."""
    out = []
    state = seed & 0x7FFFFFFF
    for _ in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        out.append((state >> 8) % (2 * amplitude + 1) - amplitude)
    return out


def _pcm(samples):
    return array.array('h', samples).tobytes()


# name -> (samples, target_rms, max_gain_db)
GOLDEN_CASES = {
    "codex_pm300": ([300, -300], 1400, 9.0),
    "frame_20ms_quiet": (_lcg_samples(160, 400, 1), 1400, 9.0),
    "saturation": (_lcg_samples(160, 30000, 2), 32000, 18.0),
    "int16_extremes": ([-32768, 32767, -32768, 32767, 0, 1, -1, 12345], 40000, 18.0),
    "silence": ([0] * 160, 1400, 9.0),
    "loud_unchanged": (_lcg_samples(160, 20000, 3), 1400, 9.0),
    "gain_cap": (_lcg_samples(160, 3, 4), 1400, 18.0),
    "truncation": ([-1, -2, -3, 5, 7], 1400, 9.0),
    "long_500ms": (_lcg_samples(4000, 1500, 5), 1400, 9.0),
}

# sha256 of the output of 15ac267's _apply_normalizer for each case above.
GOLDEN_SHA256 = {
    "codex_pm300": "aa19e94401c28ea6adb0155820e0e5da6051a57014f5db3bcc5116e82f75845b",
    "frame_20ms_quiet": "8f78c885f4712987827afc62d74b7a3db491a9090c938c07ef825e771be10d02",
    "saturation": "38cbb1a74a4506958a09fd487c349e24e85deb279277d9123c10ec8e140555fb",
    "int16_extremes": "d25e82dd9338fd243a780090144121a25ef95873d3dbee399ba292560d7e1d6b",
    "silence": "7b6436b0c98f62380866d9432c2af0ee08ce16a171bda6951aecd95ee1307d61",
    "loud_unchanged": "c029acf9901ef64a1118c5b0605b87c138d3c3e46c6a5dc766aa9090c32d223f",
    "gain_cap": "fa85e0666c767a0b8cd87734a1c37e3cecebe996deb597f5274ac8531f471f05",
    "truncation": "49b71b4912bc65f189a3b32b3e1a70b526396c6e274d02ae0cc1952deaf244db",
    "long_500ms": "f1f0b87871a3bebb15b78043035591eb0124c54a6c952edd3a463cbafc110f1c",
}

# Cases where the base engine really applied gain (output differs from input).
CASES_WITH_GAIN = {
    "codex_pm300", "frame_20ms_quiet", "saturation", "int16_extremes",
    "gain_cap", "truncation", "long_500ms",
}

# Shared by the fresh-interpreter scripts below.
_SUBPROCESS_PRELUDE = """
import hashlib, json, os, sys

def make_manager():
    from src.core.streaming_playback_manager import StreamingPlaybackManager
    class Dummy:
        pass
    return StreamingPlaybackManager(
        session_store=Dummy(), ari_client=Dummy(), conversation_coordinator=None,
        fallback_playback_manager=None, streaming_config={}, audio_transport="audiosocket",
    )

def emit(result):
    print("RESULT " + json.dumps(result))
"""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _make_manager():
    class Dummy:
        pass
    return StreamingPlaybackManager(
        session_store=Dummy(),
        ari_client=Dummy(),
        conversation_coordinator=None,
        fallback_playback_manager=None,
        streaming_config={},
        audio_transport="audiosocket",
    )


def _run_fresh_interpreter(body, extra_env=None):
    """Run ``body`` in a brand-new Python process and return its RESULT payload."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("OPENBLAS_NUM_THREADS", None)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_PRELUDE + body],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"fresh interpreter failed:\n{proc.stderr[-3000:]}"
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, f"no RESULT line in output:\n{proc.stdout[-2000:]}"
    return json.loads(lines[-1][len("RESULT "):])


@pytest.fixture
def no_numpy(monkeypatch):
    """Simulate an image without numpy for code running in this process."""
    monkeypatch.setitem(sys.modules, "numpy", None)   # `import numpy` raises ImportError
    monkeypatch.setattr(spm, "np", None, raising=False)


def _hex_cases():
    return {name: [_pcm(samples).hex(), target, gain] for name, (samples, target, gain) in GOLDEN_CASES.items()}


# --------------------------------------------------------------------------
# Audio parity with the engine that shipped before numpy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_python_fallback_matches_base_audio(name, no_numpy):
    """Without numpy the method must return exactly what the base engine returned."""
    samples, target, gain = GOLDEN_CASES[name]
    out = _make_manager()._apply_normalizer(_pcm(samples), target, gain)
    assert _sha(out) == GOLDEN_SHA256[name]
    assert (out != _pcm(samples)) == (name in CASES_WITH_GAIN)


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_numpy_path_matches_base_audio(name):
    pytest.importorskip("numpy")
    samples, target, gain = GOLDEN_CASES[name]
    out = _make_manager()._apply_normalizer(_pcm(samples), target, gain)
    assert _sha(out) == GOLDEN_SHA256[name]


def test_fallback_keeps_gain_in_mulaw(no_numpy):
    """Reviewer's scenario: +/-300 PCM at the effective production settings (1400 / 9 dB)."""
    pcm = _pcm([300, -300])
    out = _make_manager()._apply_normalizer(pcm, 1400, 9.0)
    assert audioop.lin2ulaw(out, 2).hex() == "d151"       # base engine output
    assert audioop.lin2ulaw(pcm, 2).hex() == "e464"       # what losing the gain looks like


@pytest.mark.parametrize("backend", ["python", "numpy"])
@pytest.mark.parametrize("pcm,target", [(b"", 1400), (b"\x01\x02\x03", 1400), (_pcm([300, -300]), 0)])
def test_untouched_inputs(backend, pcm, target, request):
    """Empty, odd-length and target<=0 inputs come back unchanged on both paths."""
    if backend == "numpy":
        pytest.importorskip("numpy")
    else:
        request.getfixturevalue("no_numpy")
    assert _make_manager()._apply_normalizer(pcm, target, 9.0) == pcm


def test_numpy_and_python_paths_agree_on_many_buffers():
    pytest.importorskip("numpy")
    checked = 0
    for seed in range(1, 401):
        length = 1 + (seed * 37) % 1600
        amplitude = [3, 40, 400, 3000, 12000, 32767][seed % 6]
        target = [1400, 700, 5000, 32000][seed % 4]
        gain = [9.0, 18.0, 3.0, 0.5][seed % 4]
        pcm = _pcm(_lcg_samples(length, amplitude, seed))
        assert spm._normalize_pcm16_numpy(pcm, target, gain) == spm._normalize_pcm16_python(pcm, target, gain), (
            f"seed={seed} length={length} amplitude={amplitude} target={target} gain={gain}"
        )
        checked += 1
    assert checked == 400


# --------------------------------------------------------------------------
# Start-up behaviour (fresh interpreter)
# --------------------------------------------------------------------------

def test_numpy_is_loaded_at_engine_import_and_first_frame_imports_nothing():
    """Deterministic stand-in for "the first 20 ms tick is not delayed by an import"."""
    pytest.importorskip("numpy")
    pcm_hex = _pcm(GOLDEN_CASES["frame_20ms_quiet"][0]).hex()
    result = _run_fresh_interpreter(f"""
import src.engine  # the real start-up import chain
loaded_at_import = "numpy" in sys.modules
mgr = make_manager()
before = set(sys.modules)
out = mgr._apply_normalizer(bytes.fromhex("{pcm_hex}"), 1400, 9.0)
emit({{
    "loaded_at_import": loaded_at_import,
    "new_modules": sorted(set(sys.modules) - before),
    "out": hashlib.sha256(out).hexdigest(),
}})
""")
    assert result["loaded_at_import"] is True
    assert result["new_modules"] == []
    assert result["out"] == GOLDEN_SHA256["frame_20ms_quiet"]


def test_engine_starts_without_numpy_and_keeps_base_audio():
    """New code on an image without numpy: engine imports, audio unchanged, operator warned."""
    result = _run_fresh_interpreter(f"""
sys.modules["numpy"] = None  # makes `import numpy` raise ImportError
import src.engine
import src.core.streaming_playback_manager as spm
from structlog.testing import capture_logs
with capture_logs() as logs:
    mgr = make_manager()
cases = json.loads('''{json.dumps(_hex_cases())}''')
emit({{
    "np_is_none": getattr(spm, "np", "missing") is None,
    "import_error": getattr(spm, "_NUMPY_IMPORT_ERROR", None),
    "warnings": [e["event"] for e in logs if e.get("log_level") == "warning" and "numpy" in e["event"]],
    "backend": [e.get("normalizer_backend") for e in logs if e["event"] == "Streaming mode"],
    "out": {{name: hashlib.sha256(mgr._apply_normalizer(bytes.fromhex(h), t, g)).hexdigest()
            for name, (h, t, g) in cases.items()}},
}})
""")
    assert result["out"] == GOLDEN_SHA256          # the defect: gain silently lost
    assert result["np_is_none"] is True
    assert result["import_error"]
    assert len(result["warnings"]) == 1
    assert result["backend"] == ["python"]


def test_startup_log_reports_numpy_backend_without_warning():
    pytest.importorskip("numpy")
    result = _run_fresh_interpreter("""
import src.core.streaming_playback_manager as spm
from structlog.testing import capture_logs
with capture_logs() as logs:
    make_manager()
emit({
    "warnings": [e["event"] for e in logs if e.get("log_level") == "warning" and "numpy" in e["event"]],
    "backend": [e.get("normalizer_backend") for e in logs if e["event"] == "Streaming mode"],
})
""")
    assert result["warnings"] == []
    assert result["backend"] == ["numpy"]


_THREAD_PROBE = """
def threads():
    return len(os.listdir("/proc/self/task"))
before = threads()
import src.core.streaming_playback_manager as spm
mgr = make_manager()
mgr._apply_normalizer(bytes.fromhex("2c01d4fe") * 80, 1400, 9.0)  # one 20 ms frame through the real path
emit({
    "before": before,
    "after": threads(),
    "env": os.environ.get("OPENBLAS_NUM_THREADS"),
    "numpy": "numpy" in sys.modules,
})
"""


@pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="needs Linux /proc to count threads")
def test_loading_numpy_adds_no_worker_threads():
    """numpy's BLAS pool (one thread per core) is never used by the normalizer."""
    pytest.importorskip("numpy")
    result = _run_fresh_interpreter(_THREAD_PROBE)
    assert result["numpy"] is True
    assert result["after"] == result["before"]     # the finding: 15 idle threads on a 16-core host
    assert result["env"] == "1"


@pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="needs Linux /proc to count threads")
def test_operator_can_override_blas_thread_cap():
    result = _run_fresh_interpreter(_THREAD_PROBE, extra_env={"OPENBLAS_NUM_THREADS": "2"})
    assert result["env"] == "2"

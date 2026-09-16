"""Unit contracts for the virtual ai-engine -> Golden-Modal load tool.

No network or credentials are used here.  Live connections remain guarded by
``--confirm-live-modal`` in the tool itself.
"""

import importlib.util
from pathlib import Path
import sys


def _tool_module():
    path = Path(__file__).parents[1] / "tools" / "modal_virtual_load.py"
    spec = importlib.util.spec_from_file_location("modal_virtual_load", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_frame_program_preserves_audio_then_inserts_exact_silence():
    tool = _tool_module()
    first = b"a" * tool.FRAME_BYTES
    second = b"b" * tool.FRAME_BYTES
    program = tool.FrameProgram(frames=(first, second), pause_frames=2)

    assert [program.frame_at(index) for index in range(6)] == [
        first,
        second,
        tool.SILENCE_ULAW,
        tool.SILENCE_ULAW,
        first,
        second,
    ]


def test_percentile_handles_empty_and_selects_deterministically():
    tool = _tool_module()
    assert tool.percentile([], 0.90) is None
    assert tool.percentile([5.0, 1.0, 3.0], 0.50) == 3.0
    assert tool.percentile([5.0, 1.0, 3.0], 0.90) == 5.0

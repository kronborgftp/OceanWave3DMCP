"""
Determinism contract tests for the visualizer.

Two docstrings promise visual determinism but nothing verified it:

    visualizer.py            -> "Same fort files always produce the same pixels"
    server.generate_visualization -> "Same run_id always produces the same image"

These tests pin that contract. They render the SAME run twice and assert the
result is identical — not just byte-for-byte (which is the easy part) but
pixel-for-pixel after decoding, which is the property the docstrings actually
claim and the one that survives a future change to image metadata.

No solver and no real simulations/ run is needed: we synthesize a tiny fort.1XX
dataset on disk (the exact `x y E P` ASCII the parser reads), so the tests are
fast and always run wherever the plotting stack is present. They target the
matplotlib rendering path directly — the browser/canvas viewer is rendered
client-side and is out of scope here.

A guard test (distinct inputs -> distinct pixels) keeps the equality checks from
passing vacuously, e.g. if rendering ever degraded to a constant blank image.

Run:  pytest tests/test_visualization_determinism.py -v
  or: python tests/test_visualization_determinism.py
"""
import importlib.util
import io
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# The visualizer auto-installs matplotlib/Pillow on demand; we must NOT trigger a
# pip install from the test suite, so skip cleanly when the stack is absent.
_HAS_PLOTTING = (
    importlib.util.find_spec("matplotlib") is not None
    and importlib.util.find_spec("PIL") is not None
)
pytestmark = pytest.mark.skipif(
    not _HAS_PLOTTING,
    reason="matplotlib/Pillow not installed (visualizer plotting stack unavailable)",
)


def _write_synthetic_run(run_dir: Path, num_snapshots: int = 6, nx: int = 33,
                         amp: float = 0.05) -> None:
    """Lay down a minimal-but-realistic run_dir: a few fort.1XX snapshot files
    (the parser's `x y E P` columns, blank-line terminated) plus params.json.

    A travelling sine wave gives every column a non-trivial crest-to-trough, so
    the stats (_y_lim, _time_per_snapshot) and rendering paths behave just like a
    real propagating run. `amp` is exposed so a second run can be made visibly
    different from the first (see the guard test)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    length = 10.0
    k = 2 * math.pi / length            # exactly one wavelength across the domain
    for s in range(num_snapshots):
        phase = 2 * math.pi * s / num_snapshots
        rows = []
        for i in range(nx):
            x = length * i / (nx - 1)
            E = amp * math.sin(k * x - phase)
            P = amp * math.cos(k * x - phase)
            rows.append(f"{x:.6f} 0.000000 {E:.6f} {P:.6f}")
        # fort.100 = t=0 initial condition, fort.101.. = subsequent steps.
        (run_dir / f"fort.{100 + s}").write_text("\n".join(rows) + "\n")
    (run_dir / "params.json").write_text(
        json.dumps({"timestep_s": 0.05, "num_steps": 200})
    )


@pytest.fixture
def synthetic_run(tmp_path):
    run_dir = tmp_path / "20260101_000000_determinism_fixture"
    _write_synthetic_run(run_dir)
    return run_dir


def _png_pixels(data: bytes) -> bytes:
    """Decode PNG bytes to raw RGBA pixel bytes — the rendered image independent
    of any file-format metadata. tobytes() is a faithful 1:1 of the pixel grid."""
    from PIL import Image
    return Image.open(io.BytesIO(data)).convert("RGBA").tobytes()


def _gif_frame_pixels(data: bytes):
    """Decode every GIF frame to its RGBA pixel bytes; returns a list-of-frames."""
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    frames = []
    for i in range(img.n_frames):
        img.seek(i)
        frames.append(img.convert("RGBA").tobytes())
    return frames


def test_final_png_is_pixel_identical_across_renders(synthetic_run):
    """Two renders of the same run -> identical decoded pixels (the core claim),
    and in practice identical bytes too (matplotlib's Agg PNG embeds no
    timestamp — a regression that adds one is worth catching)."""
    from oceanwave_mcp import visualizer

    a = visualizer.generate_final_png(str(synthetic_run))
    b = visualizer.generate_final_png(str(synthetic_run))

    assert _png_pixels(a) == _png_pixels(b), "PNG pixels differ between renders"
    assert a == b, "PNG bytes differ between renders (image metadata not deterministic?)"


def test_gif_is_pixel_identical_across_renders(synthetic_run):
    """Same for the animated GIF: every frame's pixels must match, and the
    GIF is saved with optimize=False so the bytes match as well."""
    from oceanwave_mcp import visualizer

    a = visualizer.generate_gif_bytes(str(synthetic_run))
    b = visualizer.generate_gif_bytes(str(synthetic_run))

    fa, fb = _gif_frame_pixels(a), _gif_frame_pixels(b)
    assert len(fa) == len(fb) == 6, "unexpected frame count"
    assert fa == fb, "GIF frame pixels differ between renders"
    assert a == b, "GIF bytes differ between renders"


def test_server_tool_same_run_id_same_image(synthetic_run, monkeypatch):
    """End-to-end on the public MCP tool: generate_visualization(run_id) twice
    yields the same image bytes, exercising the run_id -> run_dir resolution the
    docstring's promise is phrased around."""
    from oceanwave_mcp import runner, server

    # Point the tool's run lookup at our throwaway fixture dir.
    monkeypatch.setattr(runner, "SIMULATIONS_DIR", synthetic_run.parent)
    run_id = synthetic_run.name

    png1 = server.generate_visualization(run_id, format="png")
    png2 = server.generate_visualization(run_id, format="png")
    assert png1.data == png2.data, "same run_id produced different PNGs"
    assert _png_pixels(png1.data) == _png_pixels(png2.data)

    gif1 = server.generate_visualization(run_id, format="gif")
    gif2 = server.generate_visualization(run_id, format="gif")
    assert gif1.data == gif2.data, "same run_id produced different GIFs"


def test_distinct_inputs_produce_distinct_pixels(tmp_path):
    """Guard against vacuous determinism: different fort data must render to
    different pixels, so the equality tests above are testing something real."""
    from oceanwave_mcp import visualizer

    small = tmp_path / "run_small"
    big = tmp_path / "run_big"
    _write_synthetic_run(small, amp=0.02)
    _write_synthetic_run(big, amp=0.20)

    assert _png_pixels(visualizer.generate_final_png(str(small))) != \
        _png_pixels(visualizer.generate_final_png(str(big))), \
        "distinct wave fields rendered to identical pixels — rendering is not data-driven"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

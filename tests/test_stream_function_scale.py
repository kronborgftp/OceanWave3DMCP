"""
Regression tests for real-scale stream-function wave generation.

These exercise the full wrapper path end to end against the compiled solver
(build_inp -> runner.run_simulation -> output_parser), covering the cases that
used to fail at steady-wave initialization with

    did not converge sufficiently after 256 iterations

The root cause was the .inp pinning the stream-function wavelength to a dummy
1.0 m while selecting WAVELENGTH mode, which made the dimensionless steepness
fed to Fenton's solver equal to H itself (H/1.0). Any H >~ 0.1 then exceeded
the breaking limit and the Newton iteration diverged. The fix switches to
PERIOD mode and passes the real dispersion wavelength; see
inp_builder.build_stream_function_wave.

Each run is ~2-4 s. The whole module is skipped automatically when the solver
isn't installed (no native binary and no Docker image).

Run:  pytest tests/test_stream_function_scale.py -v
  or: python tests/test_stream_function_scale.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp import runner                       # noqa: E402
from oceanwave_mcp.inp_builder import build_inp         # noqa: E402

pytestmark = pytest.mark.skipif(
    not runner.solver_ready(),
    reason="OceanWave3D solver not installed (no native binary and no Docker image)",
)


@pytest.fixture(autouse=True)
def _isolated_sim_dir(tmp_path, monkeypatch):
    """Write each run into a throwaway dir so the suite never pollutes
    simulations/ (and the interactive viewer's run list)."""
    monkeypatch.setattr(runner, "SIMULATIONS_DIR", tmp_path / "simulations")


def _run_stream_function(num_periods=15, **kwargs):
    inp, params = build_inp("stream_function_wave", num_periods=num_periods, **kwargs)
    return runner.run_simulation(inp, label="test_sf_scale", params=params)


# (id, kwargs, max_interior_deviation_pct)
#   deviation is measured in the clean propagation interior (wave_height_interior),
#   the same quantity the recap reports for zoned scenarios.
#   The 4 m case is gated only loosely: convergence is the point there, and the
#   interior H still lands within a few %, but we don't make the suite flaky on it.
SCALE_CASES = [
    ("baseline_flume", dict(wave_height=0.08, water_depth=1.0,  wave_period=1.0),   5.0),
    ("swell_1m",       dict(wave_height=1.0,  water_depth=10.0, wave_period=8.0),   5.0),
    ("coastal_1m",     dict(wave_height=1.0,  water_depth=4.0,  wave_period=6.0),   5.0),
    ("submerging_4m",  dict(wave_height=4.0,  water_depth=20.0, wave_period=12.0), 10.0),
]


@pytest.mark.parametrize(
    "label,kwargs,max_dev", SCALE_CASES, ids=[c[0] for c in SCALE_CASES]
)
def test_stream_function_scale_converges(label, kwargs, max_dev):
    result = _run_stream_function(**kwargs)

    # success requires the solver to print "JOB IS COMPLETE", i.e. the steady
    # wave initialised AND every time step ran.
    assert result.success, (
        f"{label}: run did not complete: {result.error_message}\n"
        f"...{(result.stdout + result.stderr)[-600:]}"
    )

    out = result.output
    assert out is not None and out.num_snapshots > 0, f"{label}: no snapshots written"
    assert out.files_found, f"{label}: no fort.1XX output files produced"

    target_H = kwargs["wave_height"]
    dev_pct = 100.0 * abs(out.wave_height_interior - target_H) / target_H
    assert dev_pct < max_dev, (
        f"{label}: interior steady H = {out.wave_height_interior:.4f} m deviates "
        f"{dev_pct:.1f}% from the requested {target_H} m (bound {max_dev}%)"
    )


def test_height_continuation_override_flows_through():
    """The stream_func_height_steps override reaches the .inp and still converges."""
    inp, params = build_inp(
        "stream_function_wave", wave_height=1.0, water_depth=10.0, wave_period=8.0,
        num_periods=10, stream_func_height_steps=12,
    )
    assert params["stream_func_height_steps"] == 12
    assert " 12 24 <-" in inp  # n_h_steps n_four_modes on the stream-function line

    result = runner.run_simulation(inp, label="test_sf_override", params=params)
    assert result.success, f"override run failed: {result.error_message}"


def test_baseline_regression_standing_wave():
    """Closed-domain scenario (zones=[]) still converges and runs all steps;
    confirms the interior-vs-global H selection didn't disturb it."""
    inp, params = build_inp(
        "nonlinear_standing_wave",
        wave_height=0.04, water_depth=1.0, wave_period=1.0, num_periods=10,
    )
    assert params["zones"] == []  # closed domain -> recap keeps the global H measure
    result = runner.run_simulation(inp, label="test_standing_regression", params=params)
    assert result.success, f"standing-wave regression failed: {result.error_message}"
    assert result.output and result.output.num_snapshots > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

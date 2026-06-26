"""
Contract tests for the nonlinear standing wave.

This scenario reproduces the FIXED Agnon & Glozman (1996) benchmark: the solver's
initial-condition routine (nonlinearstandingwave1D.f90, IC=1) hardcodes the wave
profile, so its height, wavelength, depth and period are intrinsic and NOT
configurable. The old builder accepted wave_height/water_depth/wave_period and
wrote them onto a .inp line the standing-wave path never reads — i.e. it silently
dropped them, producing the same ~0.089 m, λ=2 m field for every requested height.

These tests pin the corrected contract: supplied physical inputs are ignored AND
reported, and the emitted .inp + params carry the real fixed values regardless of
input. Pure builder tests — no solver needed, so they always run.

Run:  pytest tests/test_standing_wave_fixed.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp.inp_builder import build_inp  # noqa: E402

# The intrinsic benchmark values (see inp_builder._NLSTANDING_*).
FIXED_H = 0.0893
FIXED_DEPTH = 2.0
FIXED_WAVELENGTH = 2.0


def test_requested_height_is_ignored_and_value_is_fixed():
    _, p_tall = build_inp("nonlinear_standing_wave", wave_height=0.1)
    _, p_short = build_inp("nonlinear_standing_wave", wave_height=0.026)
    # Whatever the caller asks for, the reported wave is the fixed benchmark.
    assert p_tall["wave_height_m"] == pytest.approx(FIXED_H)
    assert p_short["wave_height_m"] == pytest.approx(FIXED_H)
    assert p_tall["wavelength_m"] == pytest.approx(FIXED_WAVELENGTH)
    assert p_tall["water_depth_m"] == pytest.approx(FIXED_DEPTH)


def test_height_does_not_change_the_emitted_inp():
    # The direct guard against the original ignored-parameter bug: the requested
    # height must not alter the physics written to the .inp.
    inp_tall, _ = build_inp("nonlinear_standing_wave", wave_height=0.1)
    inp_short, _ = build_inp("nonlinear_standing_wave", wave_height=0.026)
    assert inp_tall == inp_short


def test_ignored_inputs_are_reported_not_silent():
    _, params = build_inp(
        "nonlinear_standing_wave", wave_height=0.1, water_depth=3.0, wave_period=2.0
    )
    assert params["ignored_inputs"] == {
        "wave_height": 0.1, "water_depth": 3.0, "wave_period": 2.0,
    }


def test_no_ignored_inputs_when_none_supplied():
    _, params = build_inp("nonlinear_standing_wave", grid_points_x=65)
    assert params["ignored_inputs"] == {}


def test_resolution_and_duration_remain_configurable():
    _, params = build_inp(
        "nonlinear_standing_wave",
        grid_points_x=65, vertical_layers=12, num_periods=5, nonlinear=False,
    )
    assert params["grid_points_x"] == 65
    assert params["vertical_layers"] == 12
    assert params["num_periods"] == 5
    assert params["nonlinear"] is False


def test_domain_matches_the_fixed_wavelength_and_ic_flag():
    inp, params = build_inp("nonlinear_standing_wave")
    assert params["domain_length_m"] == pytest.approx(FIXED_WAVELENGTH)
    assert params["wavelength_m"] == pytest.approx(FIXED_WAVELENGTH)
    assert params["zones"] == []  # closed domain
    # IC=1 (predefined standing wave), IncWaveType=0 (no generation).
    assert inp.splitlines()[1].startswith("1  0")


def test_linear_flag_reaches_the_inp():
    inp_nl, _ = build_inp("nonlinear_standing_wave", nonlinear=True)
    inp_lin, _ = build_inp("nonlinear_standing_wave", nonlinear=False)
    # The linear/nonlinear computation flag is the "<flag> 0 <-" line.
    assert "\n1 0 <-\n" in inp_nl
    assert "\n0 0 <-\n" in inp_lin


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

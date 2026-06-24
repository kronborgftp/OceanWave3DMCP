"""
Unit tests for the pre-run physical-feasibility checks (oceanwave_mcp.feasibility).

Pure physics — no solver needed, so these always run.

Run:  pytest tests/test_feasibility.py -v
  or: python tests/test_feasibility.py
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp import feasibility                                  # noqa: E402
from oceanwave_mcp.feasibility import (                                # noqa: E402
    WaveInfeasibleError,
    check_feasibility,
    format_refusal,
    format_report,
    wavenumber,
)
from oceanwave_mcp.inp_builder import _wavelength                      # noqa: E402


# --------------------------------------------------------------------------
# Input sanity — must NOT raise (no div-by-zero on the dispersion solve)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("H, h, T", [
    (0.0, 1.0, 1.0),
    (0.1, 0.0, 1.0),
    (0.1, 1.0, 0.0),
    (-1.0, 1.0, 1.0),
    (0.1, -2.0, 1.0),
    (0.1, 1.0, -3.0),
    (float("nan"), 1.0, 1.0),
    (0.1, float("inf"), 1.0),
    (0.1, 1.0, float("nan")),
])
def test_invalid_inputs_are_infeasible_without_raising(H, h, T):
    res = check_feasibility(H, h, T)  # must not raise (sanity runs before the solve)
    assert res.feasible is False
    assert any(v.criterion == "Invalid input" for v in res.violations)


# --------------------------------------------------------------------------
# Miche breaking limit — the master gate
# --------------------------------------------------------------------------

def test_miche_deep_water_limit_is_about_0142():
    # Short period in deep water -> kd large -> tanh(kd) ~ 1 -> limit ~ 0.142.
    res = check_feasibility(wave_height=0.001, water_depth=100.0, wave_period=1.0)
    assert res.regime == "deep"
    assert res.miche_limit == pytest.approx(0.142, abs=1e-3)


def test_miche_refuses_oversteep_deep_wave():
    # Build a wave whose steepness clearly exceeds the deep-water 0.142 limit.
    # T=2 s, deep water: L ~ 6.24 m; pick H so H/L ~ 0.17 (> 0.142).
    h, T = 50.0, 2.0
    L = _wavelength(T, h)
    H = 0.17 * L
    res = check_feasibility(H, h, T)
    assert res.feasible is False
    assert any(v.criterion == "Miche breaking limit" for v in res.violations)


def test_miche_allows_modest_deep_wave():
    h, T = 50.0, 2.0
    L = _wavelength(T, h)
    H = 0.05 * L  # well below 0.142
    res = check_feasibility(H, h, T)
    assert res.feasible is True
    assert not res.warnings


def test_miche_shallow_water_height_limit_about_089_depth():
    # Long period in shallow water -> kd small -> limit ~ 0.142*kd -> H_max ~ 0.89 h.
    h, T = 1.0, 20.0
    res = check_feasibility(wave_height=0.01, water_depth=h, wave_period=T)
    assert res.regime == "shallow"
    h_max = res.miche_limit * res.wavelength_m
    assert h_max == pytest.approx(0.89 * h, rel=0.05)


# --------------------------------------------------------------------------
# Warning band — feasible but within 90-100% of the limit
# --------------------------------------------------------------------------

def test_near_limit_wave_is_feasible_but_warns():
    h, T = 50.0, 2.0
    L = _wavelength(T, h)
    res0 = check_feasibility(0.001, h, T)             # to read the exact limit
    H = 0.95 * res0.miche_limit * L                   # 95% of the breaking limit
    res = check_feasibility(H, h, T)
    assert res.feasible is True
    assert res.warnings, "a wave at 95% of the limit should warn"
    assert res.violations == []


# --------------------------------------------------------------------------
# Trough below seabed and sub-capillary guard
# --------------------------------------------------------------------------

def test_trough_below_seabed():
    # H >= 2d. (Miche fires too — assert the geometric violation is present.)
    res = check_feasibility(wave_height=3.0, water_depth=1.0, wave_period=4.0)
    assert res.feasible is False
    assert any(v.criterion == "Trough below seabed" for v in res.violations)


def test_sub_capillary_wave_rejected():
    # A very short period -> tiny wavelength -> below the gravity-wave regime.
    res = check_feasibility(wave_height=0.0001, water_depth=1.0, wave_period=0.05)
    assert res.feasible is False
    assert any(v.criterion == "Below the gravity-wave regime" for v in res.violations)


# --------------------------------------------------------------------------
# Regression: the real-solver-converging matrix must all stay feasible
# (mirrors SCALE_CASES in test_stream_function_scale.py)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("H, h, T", [
    (0.08, 1.0, 1.0),    # baseline_flume
    (1.0, 10.0, 8.0),    # swell_1m
    (1.0, 4.0, 6.0),     # coastal_1m
    (4.0, 20.0, 12.0),   # submerging_4m
])
def test_scale_cases_remain_feasible(H, h, T):
    res = check_feasibility(H, h, T)
    assert res.feasible is True, (
        f"gate too tight: ({H},{h},{T}) converges in the real solver but was refused"
    )


@pytest.mark.parametrize("H, h, T", [
    (0.08, 1.0, 1.0),    # stream_function_wave default
    (0.002, 2.0, 1.0),   # linear_regular_wave default
    (0.04, 1.0, 1.0),    # nonlinear_standing_wave default
])
def test_builder_defaults_are_feasible(H, h, T):
    assert check_feasibility(H, h, T).feasible is True


# --------------------------------------------------------------------------
# The dispersion refactor must be exactly consistent with _wavelength
# --------------------------------------------------------------------------

@pytest.mark.parametrize("T, h", [(1.0, 1.0), (8.0, 10.0), (6.0, 4.0), (12.0, 20.0)])
def test_wavenumber_matches_wavelength(T, h):
    assert 2.0 * math.pi / wavenumber(T, h) == pytest.approx(_wavelength(T, h), rel=1e-12)


# --------------------------------------------------------------------------
# Message content
# --------------------------------------------------------------------------

def test_format_refusal_contains_law_and_no_retry_instruction():
    res = check_feasibility(wave_height=10.0, water_depth=1.0, wave_period=4.0)
    msg = format_refusal(res, 10.0, 1.0, 4.0)
    assert "PHYSICALLY IMPOSSIBLE" in msg
    assert "REFUSED" in msg
    # cites the governing law
    assert "Miche" in msg or "0.142" in msg
    # forbids silent retry
    assert "Do NOT" in msg
    assert "adjust parameters and re-run" in msg


def test_format_report_feasible_and_infeasible():
    ok = format_report(check_feasibility(0.08, 1.0, 1.0), 0.08, 1.0, 1.0)
    assert "PHYSICALLY POSSIBLE" in ok

    bad = format_report(check_feasibility(10.0, 1.0, 4.0), 10.0, 1.0, 4.0)
    assert "PHYSICALLY IMPOSSIBLE" in bad


def test_wave_infeasible_error_is_value_error():
    assert issubclass(WaveInfeasibleError, ValueError)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

"""
Integration tests: the feasibility gate is wired into build_inp for every scenario.

These exercise the build_inp -> builder -> _enforce_feasibility path WITHOUT
running the solver, so they always run (no native binary or Docker image needed).

Run:  pytest tests/test_feasibility_integration.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp.feasibility import WaveInfeasibleError                  # noqa: E402
from oceanwave_mcp.inp_builder import build_inp                           # noqa: E402


SCENARIOS = ["stream_function_wave", "linear_regular_wave", "nonlinear_standing_wave"]

# Scenarios whose wave is generated from the supplied H/h/T — these refuse an
# impossible request. The nonlinear standing wave is a fixed benchmark that ignores
# physical inputs entirely, so it does not refuse on them (see the dedicated test
# below).
GENERATING_SCENARIOS = ["stream_function_wave", "linear_regular_wave"]


@pytest.mark.parametrize("scenario", GENERATING_SCENARIOS)
def test_impossible_wave_refused_in_generating_scenarios(scenario):
    # 10 m wave in 1 m of water — impossible by every measure.
    with pytest.raises(WaveInfeasibleError) as exc:
        build_inp(scenario, wave_height=10.0, water_depth=1.0, wave_period=4.0)
    msg = str(exc.value)
    assert "PHYSICALLY IMPOSSIBLE" in msg
    assert "REFUSED" in msg
    # the structured result rides along for callers that want it
    assert exc.value.result.feasible is False


def test_standing_wave_ignores_physical_inputs_instead_of_refusing():
    # The standing wave is a fixed benchmark: an "impossible" requested wave is not
    # refused — it is ignored (and reported), because the requested H/h/T never
    # reach the hardcoded initial condition.
    inp, params = build_inp(
        "nonlinear_standing_wave", wave_height=10.0, water_depth=1.0, wave_period=4.0
    )
    assert "<-" in inp
    assert params["ignored_inputs"] == {
        "wave_height": 10.0, "water_depth": 1.0, "wave_period": 4.0,
    }
    # The reported wave is the fixed benchmark, not the requested one.
    assert params["wave_height_m"] != 10.0
    assert params["wavelength_m"] == 2.0


def test_infeasible_error_caught_by_value_error_fallback():
    # server.run_simulation has a dedicated handler, but its existing
    # `except (ValueError, TypeError)` must still catch it as a fail-safe.
    with pytest.raises(ValueError):
        build_inp("stream_function_wave", wave_height=10.0, water_depth=1.0, wave_period=4.0)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_feasible_defaults_build_without_raising(scenario):
    inp, params = build_inp(scenario)  # all-default = feasible
    assert "<-" in inp                 # produced a real .inp body
    assert params["scenario"] == scenario
    assert params.get("feasibility_warning") is None


def test_near_limit_wave_builds_with_warning_in_params():
    # Find a near-breaking (but valid) deep-water wave and confirm the builder
    # stores the non-blocking caution in params rather than refusing.
    from oceanwave_mcp.inp_builder import _wavelength
    from oceanwave_mcp.feasibility import check_feasibility

    h, T = 50.0, 2.0
    L = _wavelength(T, h)
    probe = check_feasibility(0.001, h, T)
    H = 0.95 * probe.miche_limit * L

    inp, params = build_inp("stream_function_wave", wave_height=H, water_depth=h, wave_period=T)
    assert params.get("feasibility_warning"), "near-limit wave should carry a warning"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

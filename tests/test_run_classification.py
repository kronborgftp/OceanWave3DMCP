"""
Unit tests for runner._classify — the gate that decides whether a finished
solver process counts as SUCCESS.

This is trust-critical: the OceanWave3D Fortran binary exits with code 0 even
when it STOPs early (free surface blows up, steady solve diverges, generic
error before STOP) and can leave partial fort.* output behind. A run must
never be reported SUCCESS unless it genuinely completed cleanly. These tests
pin that contract; they are pure (no solver invocation), so they run instantly.

The stdout fragments below mirror the real solver banners:
  - OceanWave3DTakeATimeStep.f90 : "...going unstable, aborting here."
  - stream_func_coeffs.f         : "did not converge sufficiently after N iterations"
  - OceanWave3D.f90              : "JOB IS COMPLETE"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp.runner import _classify  # noqa: E402

_COMPLETE = "\n JOB IS COMPLETE\n"
_UNSTABLE = (
    " ********************************************************.\n"
    " The solution looks to be going unstable, aborting here.\n"
    " eta_max =   3429.408   > 10 h_max =   700.000\n"
)
_NOT_CONVERGED = "\n did not converge sufficiently after 256 iterations\n"


def test_clean_completion_is_success():
    ok, msg = _classify(0, "  Starting to time step.\n...lots of output..." + _COMPLETE)
    assert ok is True
    assert msg == ""


def test_instability_abort_is_failure():
    ok, msg = _classify(0, "...time stepping...\n" + _UNSTABLE)
    assert ok is False
    assert "unstable" in msg.lower()


def test_instability_overrides_job_complete():
    """The trust guarantee: a blown-up run that ALSO prints JOB IS COMPLETE
    (buffering interleave, alternate abort path, or a future solver change)
    must still be reported FAILED — never SUCCESS."""
    ok, msg = _classify(0, _UNSTABLE + _COMPLETE)
    assert ok is False
    assert "unstable" in msg.lower()


def test_steady_solve_divergence_is_failure():
    ok, msg = _classify(0, "...initialising...\n" + _NOT_CONVERGED)
    assert ok is False
    assert "did not converge" in msg.lower()


def test_steady_divergence_overrides_job_complete():
    ok, msg = _classify(0, _NOT_CONVERGED + _COMPLETE)
    assert ok is False
    assert "did not converge" in msg.lower()


def test_generic_fortran_error_is_failure():
    ok, msg = _classify(0, "Error: something went wrong\n" + _COMPLETE)
    assert ok is False


def test_nonzero_exit_code_is_failure():
    ok, msg = _classify(139, "partial output, crashed")
    assert ok is False
    assert "139" in msg


def test_missing_completion_banner_is_failure():
    """Exit 0 but no completion banner and no recognised marker => not success."""
    ok, msg = _classify(0, "  Starting to time step.\n...output but truncated...")
    assert ok is False


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

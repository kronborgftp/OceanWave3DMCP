"""
Builds OceanWave3D .inp parameter files from human-friendly inputs.

The .inp format is a fixed-order ASCII file where each line contains
space-separated values followed by an inline comment.  Only the values
before the '<-' comment marker are read by the Fortran code.
"""
import math
from dataclasses import dataclass
from typing import Optional

from .feasibility import (
    WaveInfeasibleError,
    check_feasibility,
    format_refusal,
    format_warning,
    wavenumber,
)


# ---------------------------------------------------------------------------
# Scenarios exposed to the LLM
# ---------------------------------------------------------------------------

SCENARIOS = {
    "stream_function_wave": {
        "description": (
            "Nonlinear regular wave in 2D generated using stream function theory. "
            "Good for studying finite-amplitude (nonlinear) wave propagation. "
            "Works from small flume waves up to real ocean scale (metre-class "
            "heights in deep or coastal depths). The limiting factor is wave "
            "steepness H/L, not absolute size: waves close to the breaking limit "
            "(H/L ≈ 0.1) may fail to converge at initialization. "
            "Typical parameters: wave height 0.05–5 m, depth 0.5–25 m, period 1–15 s."
        ),
        "parameters": {
            "wave_height":   "Wave height H [m] (crest-to-trough). Default: 0.08",
            "water_depth":   "Still-water depth h [m]. Default: 1.0",
            "wave_period":   "Wave period T [s]. Default: 1.0",
            "domain_length": "Horizontal domain length Lx [m]. Auto-computed if omitted.",
            "grid_points_x": "Number of horizontal grid points Nx (odd, e.g. 65/129/257). Default: 129",
            "vertical_layers": "Number of vertical grid layers Nz. Default: 9",
            "num_periods":   "Simulation duration in wave periods. Default: 15",
            "nonlinear":     "True = fully nonlinear, False = linear equations. Default: True",
            "stream_func_height_steps": (
                "Advanced. Height-continuation steps for the steady-wave solve. "
                "Default 6 (clamped to 60); raise only if a near-breaking wave "
                "fails to converge."
            ),
        },
    },
    "linear_regular_wave": {
        "description": (
            "Small-amplitude linear regular wave in 2D with generation and absorption zones. "
            "Good for testing and validation at low steepness (H/L < 0.01). "
            "Typical parameters: wave height 0.001–0.01 m, depth 1–5 m, period 1–5 s."
        ),
        "parameters": {
            "wave_height":   "Wave height H [m]. Default: 0.002",
            "water_depth":   "Still-water depth h [m]. Default: 2.0",
            "wave_period":   "Wave period T [s]. Default: 1.0",
            "domain_length": "Horizontal domain length Lx [m]. Auto-computed if omitted.",
            "grid_points_x": "Number of horizontal grid points Nx. Default: 137",
            "vertical_layers": "Number of vertical grid layers Nz. Default: 9",
            "num_periods":   "Simulation duration in wave periods. Default: 10",
        },
    },
    "nonlinear_standing_wave": {
        "description": (
            "Nonlinear standing wave (1D / 2D) — a classic benchmark for verifying "
            "numerical accuracy. No generation zones; the initial condition is a "
            "nonlinear standing wave profile. "
            "Typical parameters: amplitude 0.01–0.1 m, depth 0.5–2 m."
        ),
        "parameters": {
            "wave_height":   "Wave height H [m] (twice the amplitude). Default: 0.04",
            "water_depth":   "Still-water depth h [m]. Default: 1.0",
            "wave_period":   "Wave period T [s] (sets domain length = L/2). Default: 1.0",
            "grid_points_x": "Number of horizontal grid points Nx. Default: 33",
            "vertical_layers": "Number of vertical grid layers Nz. Default: 9",
            "num_periods":   "Simulation duration in wave periods. Default: 10",
            "nonlinear":     "True = fully nonlinear. Default: True",
        },
    },
}


# ---------------------------------------------------------------------------
# Dispersion relation helper
# ---------------------------------------------------------------------------

def _wavelength(T: float, h: float, g: float = 9.82) -> float:
    """Return wavelength L from period T and depth h via the dispersion solve.

    Delegates to feasibility.wavenumber so the iterative dispersion solve has a
    single source of truth shared with the pre-run feasibility checks.
    """
    return 2.0 * math.pi / wavenumber(T, h, g)


def _enforce_feasibility(H: float, h: float, T: float) -> Optional[str]:
    """Refuse a physically impossible wave before any solver work.

    Raises WaveInfeasibleError (a ValueError subclass) with a fully-formatted
    refusal if the wave cannot exist. Otherwise returns a non-blocking warning
    string for a near-breaking-but-valid wave (or None).
    """
    res = check_feasibility(H, h, T)
    if not res.feasible:
        raise WaveInfeasibleError(format_refusal(res, H, h, T), res)
    return format_warning(res, H, h, T) or None


# ---------------------------------------------------------------------------
# .inp builders
# ---------------------------------------------------------------------------

@dataclass
class _ZoneLayout:
    x_gen_end: float
    x_ramp_end: float
    x_damp_start: float
    Lx: float


def _zone_layout(Lx: float) -> _ZoneLayout:
    """Standard 1/8 + 1/8 + 1/2 + 1/4 zone split."""
    return _ZoneLayout(
        x_gen_end=Lx / 8.0,
        x_ramp_end=Lx / 4.0,
        x_damp_start=6.0 * Lx / 8.0,
        Lx=Lx,
    )


_MIN_NZ = 9  # stencil gamma=3 → needs 2*3+1=7; use 9 as safe minimum

# Stream-function (Fenton) steady-wave solver controls.
#
# n_h_steps is HEIGHT CONTINUATION: the steady wave is reached by ramping the
# target height up over this many increments, re-solving from the previous
# solution at each step. More steps = gentler continuation for steep waves.
# 6 converges the full real-scale test matrix (≤ ~4 m), so it is the default;
# exposed via run_simulation for near-breaking cases that may need more.
#
# Note: the per-step Newton iteration cap (256) and the convergence tolerance
# are hardcoded in the Fortran solver (stream_func_coeffs.f) and are NOT read
# from the .inp, so they are intentionally not exposed here — raising them
# would require editing the solver, which is out of scope.
_DEFAULT_STREAM_FUNC_HEIGHT_STEPS = 6
_MAX_STREAM_FUNC_HEIGHT_STEPS = 60
_STREAM_FUNC_FOURIER_MODES = 24


def _zone_params(z: _ZoneLayout) -> list[dict]:
    """User-facing zone annotations stored in params.json for the viewer.

    The generation and ramp relaxation zones are merged into one region —
    both exist to create the wave; the distinction is numerical, not physical.
    """
    return [
        {"x0": 0.0, "x1": round(z.x_ramp_end, 4),
         "kind": "generation", "label": "waves created here"},
        {"x0": round(z.x_damp_start, 4), "x1": round(z.Lx, 4),
         "kind": "absorption", "label": "waves absorbed here"},
    ]


def build_stream_function_wave(
    wave_height: float = 0.08,
    water_depth: float = 1.0,
    wave_period: float = 1.0,
    domain_length: Optional[float] = None,
    grid_points_x: int = 129,
    vertical_layers: int = 9,
    num_periods: float = 15.0,
    nonlinear: bool = True,
    stream_func_height_steps: int = _DEFAULT_STREAM_FUNC_HEIGHT_STEPS,
) -> tuple[str, dict]:
    """Return (inp_content, resolved_params) for a stream-function wave simulation."""
    H, h, T = wave_height, water_depth, wave_period
    feasibility_warning = _enforce_feasibility(H, h, T)
    L = _wavelength(T, h)
    Lx = domain_length if domain_length else max(8.0 * L, 4.0)
    Nx = grid_points_x
    Nz = max(vertical_layers, _MIN_NZ)

    # Height-continuation steps for the Fenton steady-wave solve. Reject
    # non-positive values and clamp to a sane ceiling so a typo can't request
    # thousands of steps.
    n_h_steps = int(stream_func_height_steps)
    if n_h_steps < 1:
        raise ValueError(
            "stream_func_height_steps must be a positive integer "
            f"(stream-function height-continuation steps), got {stream_func_height_steps!r}"
        )
    n_h_steps = min(n_h_steps, _MAX_STREAM_FUNC_HEIGHT_STEPS)

    dt = T / 40.0
    Nsteps = max(1, int(math.ceil(num_periods * T / dt)))
    stride = max(2, Nsteps // 100)  # stride ≥ 2 avoids 1000s of files
    nonlinear_flag = 1 if nonlinear else 0
    z = _zone_layout(Lx)

    params = {
        "scenario": "stream_function_wave",
        "wave_height_m": H,
        "water_depth_m": h,
        "wave_period_s": T,
        "wavelength_m": round(L, 4),
        "domain_length_m": round(Lx, 4),
        "grid_points_x": Nx,
        "vertical_layers": Nz,
        "num_periods": num_periods,
        "timestep_s": round(dt, 6),
        "num_steps": Nsteps,
        "nonlinear": nonlinear,
        "stream_func_height_steps": n_h_steps,
        "zones": _zone_params(z),
        "feasibility_warning": feasibility_warning,
    }

    # Each line needs a trailing '<-' so gfortran's list-directed reader
    # treats it as an end-of-record marker and does not consume tokens
    # from the next line. Without this the file parsing silently misaligns.
    inp = "\n".join([
        f"Stream function wave  H={H:.3f}m  h={h:.3f}m  T={T:.3f}s",
        f"0  1  1000. <-",
        f"{Lx:.3f} 1. {h:.3f} {Nx} 1 {Nz} 0 0 1 1 1 1 <-",
        f"3 3 3 1 1 1 <-",
        f"{Nsteps} {dt:.10f} 1 0 1 <-",
        f"9.82 <-",
        f"1 1 0 23 1e-8 1e-6 1 V 1 1 20 <-",
        # Stream-function steady-wave parameters (Fenton). Field order per the
        # solver's reader (ReadInputFileParameters.f90):
        #   H  h  L  T  i_wavel_or_per  e_or_s_vel  i_euler_or_stokes  n_h_steps  n_four_modes
        # Use PERIOD mode (i_wavel_or_per=1) so the solver derives the wavelength
        # self-consistently from H, h, T and the nondimensional steepness is
        # physical: H/(g·T²). The earlier WAVELENGTH mode (flag 0) paired with a
        # dummy L=1.0 made the steepness H/L = H/1.0 = H, so any H ≳ 0.1 exceeded
        # the breaking limit and the Newton solve diverged regardless of depth or
        # period. L is still passed (the dispersion estimate) so the deep/finite-
        # depth branch is classified from a sensible wavenumber.
        f"{H:.4f} {h:.4f} {L:.4f} {T:.4f} 1 0. 1 {n_h_steps} {_STREAM_FUNC_FOURIER_MODES} <-",
        f"-{stride} 20 1 1 <-",
        f"1 {Nx} 1 1 1 1 1 {Nsteps} 1 <-",
        f"{nonlinear_flag} 0 <-",
        f"0 6 10 0.08 0.08 0.4 <-",
        f"1 0. 2 X 0 <-",
        f"0. {z.x_gen_end:.3f} 0. 1. 9 3.5 X 1 X 0. <-",
        f"{z.x_gen_end:.3f} {z.x_ramp_end:.3f} 0. 1. 10 3.5 X 1 X 0. <-",
        f"1  1 <-",
        f"{z.x_damp_start:.3f} {z.Lx:.3f} 0. 0. 1. 1. 0 <-",
        f"0 2.0 2 0 0 1 0 <-",
        f"0 <-",
    ]) + "\n"
    return inp, params


def build_linear_regular_wave(
    wave_height: float = 0.002,
    water_depth: float = 2.0,
    wave_period: float = 1.0,
    domain_length: Optional[float] = None,
    grid_points_x: int = 137,
    vertical_layers: int = 9,
    num_periods: float = 10.0,
) -> tuple[str, dict]:
    """Return (inp_content, resolved_params) for a linear regular wave simulation."""
    H, h, T = wave_height, water_depth, wave_period
    feasibility_warning = _enforce_feasibility(H, h, T)
    L = _wavelength(T, h)
    Lx = domain_length if domain_length else max(8.0 * L, 4.0)
    Nx = grid_points_x
    Nz = max(vertical_layers, _MIN_NZ)

    dt = T / 40.0
    Nsteps = max(1, int(math.ceil(num_periods * T / dt)))
    stride = max(2, Nsteps // 100)
    z = _zone_layout(Lx)

    params = {
        "scenario": "linear_regular_wave",
        "wave_height_m": H,
        "water_depth_m": h,
        "wave_period_s": T,
        "wavelength_m": round(L, 4),
        "domain_length_m": round(Lx, 4),
        "grid_points_x": Nx,
        "vertical_layers": Nz,
        "num_periods": num_periods,
        "timestep_s": round(dt, 6),
        "num_steps": Nsteps,
        "nonlinear": False,
        "zones": _zone_params(z),
        "feasibility_warning": feasibility_warning,
    }

    # Linear wave uses IncWaveType=2 (linear irregular/regular), ispec=-1 (monochromatic)
    inp = "\n".join([
        f"Linear regular wave  H={H:.4f}m  h={h:.3f}m  T={T:.3f}s",
        f"0  2 <-",
        f"{Lx:.3f} 1. {h:.3f} {Nx} 1 {Nz} 0 0 1 1 1 1 <-",
        f"3 3 3 1 1 1 <-",
        f"{Nsteps} {dt:.10f} 1 0 1 <-",
        f"9.82 <-",
        f"1 1 0 23 1e-8 1e-6 1 V 1 1 20 <-",
        f"{H:.4f} {h:.4f} 1.0 {T:.4f} 0 0. 1 4 32 <-",
        f"-{stride} 20 1 1 <-",
        f"1 {Nx} 1 1 1 1 1 {Nsteps} 1 <-",
        f"0 0 <-",
        f"0 6 10 0.08 0.08 0.4 <-",
        f"1 {T:.2f} 3 X 0 <-",
        f"0. {z.x_gen_end:.3f} 0. 1. 9 3.5 X 1 X 0. <-",
        f"{z.x_gen_end:.3f} {z.x_ramp_end:.3f} 0. 1. 10 3.5 X 1 X 0. <-",
        f"{z.x_damp_start:.3f} {z.Lx:.3f} 0. 1. 9 3.5 X 0 X 0. <-",
        f"0 2.0 2 0 0 1 0 <-",
        f"0 <-",
        f"-1  {T:.4f} {H:.4f} {h:.4f} 50. -1 -34 0. 0. run.el 0.0 <-",
    ]) + "\n"
    return inp, params


def build_nonlinear_standing_wave(
    wave_height: float = 0.04,
    water_depth: float = 1.0,
    wave_period: float = 1.0,
    grid_points_x: int = 33,
    vertical_layers: int = 9,
    num_periods: float = 10.0,
    nonlinear: bool = True,
) -> tuple[str, dict]:
    """Return (inp_content, resolved_params) for a nonlinear standing wave simulation."""
    H, h, T = wave_height, water_depth, wave_period
    feasibility_warning = _enforce_feasibility(H, h, T)
    L = _wavelength(T, h)
    Lx = L / 2.0  # domain = half wavelength for standing wave
    Nx = grid_points_x
    Nz = max(vertical_layers, _MIN_NZ)

    dt = T / 40.0
    Nsteps = max(1, int(math.ceil(num_periods * T / dt)))
    stride = max(2, Nsteps // 100)
    nonlinear_flag = 1 if nonlinear else 0

    params = {
        "scenario": "nonlinear_standing_wave",
        "wave_height_m": H,
        "water_depth_m": h,
        "wave_period_s": T,
        "wavelength_m": round(L, 4),
        "domain_length_m": round(Lx, 4),
        "grid_points_x": Nx,
        "vertical_layers": Nz,
        "num_periods": num_periods,
        "timestep_s": round(dt, 6),
        "num_steps": Nsteps,
        "nonlinear": nonlinear,
        "zones": [],  # closed domain — no generation/absorption zones
        "feasibility_warning": feasibility_warning,
    }

    inp = "\n".join([
        f"Nonlinear standing wave  H={H:.3f}m  h={h:.3f}m  T={T:.3f}s",
        f"1  0 <-",
        f"{Lx:.4f} 1. {h:.3f} {Nx} 1 {Nz} 0 0 1 1 1 1 <-",
        f"3 3 3 1 1 1 <-",
        f"{Nsteps} {dt:.10f} 1 0 1 <-",
        f"9.82 <-",
        f"1 1 0 23 1e-8 1e-6 1 V 1 1 20 <-",
        f"{H:.4f} {h:.4f} 1.0 {T:.4f} 0 0. 1 6 24 <-",
        f"-{stride} 20 1 1 <-",
        f"1 {Nx} 1 1 1 1 1 {Nsteps} 1 <-",
        f"{nonlinear_flag} 0 <-",
        f"0 6 10 0.08 0.08 0.4 <-",
        f"0 0. 0 X 0 <-",
        f"0  0 <-",
        f"0 2.0 2 0 0 1 0 <-",
        f"0 <-",
    ]) + "\n"
    return inp, params


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def build_inp(scenario: str, **kwargs) -> tuple[str, dict]:
    """Build a .inp file for the given scenario. Returns (inp_content, resolved_params)."""
    if scenario == "stream_function_wave":
        return build_stream_function_wave(**kwargs)
    elif scenario == "linear_regular_wave":
        return build_linear_regular_wave(**kwargs)
    elif scenario == "nonlinear_standing_wave":
        return build_nonlinear_standing_wave(**kwargs)
    else:
        raise ValueError(f"Unknown scenario '{scenario}'. Choose from: {list(SCENARIOS)}")

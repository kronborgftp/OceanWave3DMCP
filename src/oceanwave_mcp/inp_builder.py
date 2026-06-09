"""
Builds OceanWave3D .inp parameter files from human-friendly inputs.

The .inp format is a fixed-order ASCII file where each line contains
space-separated values followed by an inline comment.  Only the values
before the '<-' comment marker are read by the Fortran code.
"""
import math
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Scenarios exposed to the LLM
# ---------------------------------------------------------------------------

SCENARIOS = {
    "stream_function_wave": {
        "description": (
            "Nonlinear regular wave in 2D generated using stream function theory. "
            "Good for studying finite-amplitude (nonlinear) wave propagation. "
            "Typical parameters: wave height 0.05–0.3 m, depth 0.5–5 m, period 1–10 s."
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
    """Return wavelength L from period T and depth h via iterative dispersion solve."""
    omega = 2.0 * math.pi / T
    k = omega**2 / g  # deep-water seed
    for _ in range(30):
        k = omega**2 / (g * math.tanh(k * h))
    return 2.0 * math.pi / k


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


def build_stream_function_wave(
    wave_height: float = 0.08,
    water_depth: float = 1.0,
    wave_period: float = 1.0,
    domain_length: Optional[float] = None,
    grid_points_x: int = 129,
    vertical_layers: int = 9,
    num_periods: float = 15.0,
    nonlinear: bool = True,
) -> tuple[str, dict]:
    """Return (inp_content, resolved_params) for a stream-function wave simulation."""
    H, h, T = wave_height, water_depth, wave_period
    L = _wavelength(T, h)
    Lx = domain_length if domain_length else max(8.0 * L, 4.0)
    Nx = grid_points_x
    Nz = max(vertical_layers, _MIN_NZ)

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
        f"{H:.4f} {h:.4f} 1.0 {T:.4f} 0 0. 1 6 24 <-",
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

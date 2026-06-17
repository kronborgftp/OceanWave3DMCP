"""
Runs the OceanWave3D binary in an isolated temp directory and returns results.

Each call to run_simulation():
  1. Creates a unique sub-directory under SIMULATIONS_DIR.
  2. Writes the .inp file there.
  3. Executes the binary (cwd = that directory).
  4. Captures stdout/stderr and parses fort.1XX output files.
  5. Returns a RunResult with stats and raw console output.
"""
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .output_parser import SimulationOutput, load_output

# Location of the compiled binary (relative to this file's package root)
_HERE = Path(__file__).parent
_REPO_ROOT = _HERE.parent.parent          # …/OceanWaveMCP/src  → two up
_BINARY_NAME = "OceanWave3D.exe" if sys.platform == "win32" else "OceanWave3D"
BINARY_PATH = _REPO_ROOT / "bin" / _BINARY_NAME
SIMULATIONS_DIR = _REPO_ROOT / "simulations"

INP_FILENAME = "input.inp"

# Run IDs created by THIS server process, in creation order. The localhost
# viewer uses it to present "runs from this session" for side-by-side
# comparison; older on-disk runs remain reachable behind a "show all" toggle.
SESSION_RUN_IDS: list = []
# Hard wall-clock limit per simulation. Kept BELOW the Claude Desktop MCP client's
# ~240 s request timeout so a slow or diverging run returns a clean "timed out"
# RunResult instead of the client aborting the tool call with "request expired /
# connection interrupted". Override via OCEANWAVE3D_SIM_TIMEOUT for heavier batch runs
# (only safe with a client that has a longer request timeout than this).
TIMEOUT_SECONDS = int(os.environ.get("OCEANWAVE3D_SIM_TIMEOUT", "180"))

# Which execution backend to use: "native" (the compiled bin/OceanWave3D),
# "docker" (the sandbox container), or "auto" (default — prefer the native
# binary when present, else the Docker image). Override with OCEANWAVE3D_BACKEND.
BACKEND_ENV = "OCEANWAVE3D_BACKEND"


def select_backend() -> str | None:
    """
    Decide how to run the solver, or return None if it isn't installed by any
    backend. "auto" prefers the native binary (no container overhead) and falls
    back to the Docker sandbox image.
    """
    choice = os.environ.get(BACKEND_ENV, "auto").strip().lower()
    if choice == "native":
        return "native" if BINARY_PATH.exists() else None
    if choice == "docker":
        from . import docker_runner
        return "docker" if docker_runner.image_built() else None
    # auto
    if BINARY_PATH.exists():
        return "native"
    from . import docker_runner
    return "docker" if docker_runner.image_built() else None


def solver_ready() -> bool:
    """True if a simulation can run by either the native or the Docker backend."""
    return select_backend() is not None


# Solver banners that mean the run bailed out, even though the Fortran STOPs
# with exit code 0 and may have already written partial fort.* output. A
# SUCCESS must be trustworthy, so these are treated as HARD failures that
# OVERRIDE a later "JOB IS COMPLETE": a diverged or blown-up run can never be
# reported as success. Each marker is matched as a literal substring of stdout
# and is verified unique to its failure path in the bundled solver source.
_FATAL_MARKERS: tuple[tuple[str, str], ...] = (
    # Free surface blew up; the time loop aborts and STOPs mid-run
    # (OceanWave3DTakeATimeStep.f90: "...going unstable, aborting here.").
    (
        "going unstable",
        "Simulation became numerically unstable and was aborted by the solver "
        "(the free surface blew up mid-run). The wave is likely too steep or too "
        "nonlinear to propagate stably here — reduce the wave height, increase the "
        "water depth, or lengthen the period (lower steepness).",
    ),
    # Fenton stream-function steady wave failed to converge at initialization
    # (stream_func_coeffs.f: "did not converge sufficiently after N iterations").
    (
        "did not converge sufficiently",
        "The steady wave could not be computed at initialization (the Fenton "
        "stream-function solve did not converge). The requested wave is likely "
        "beyond the breaking limit — reduce the wave height or lengthen the period.",
    ),
)


def _classify(returncode: int, stdout: str) -> tuple[bool, str]:
    """Map a finished solver process to (success, error_message). Backend-agnostic.

    The Fortran solver exits with code 0 even when it STOPs early (instability
    abort, non-converged steady solve, or a generic "Error:" before STOP) and
    can leave partial output behind, so success is never inferred from the exit
    code alone — it requires a clean "JOB IS COMPLETE" and the absence of any
    failure banner.
    """
    # Hard failures first: these override "JOB IS COMPLETE" so an aborted or
    # diverged run is never reported as SUCCESS.
    for marker, message in _FATAL_MARKERS:
        if marker in stdout:
            return False, message
    # Other generic Fortran errors printed before a STOP.
    fortran_error = any(
        marker in stdout for marker in ("Error:", "error:", "ERROR:", "JOB IS NOT")
    )
    success = returncode == 0 and not fortran_error and "JOB IS COMPLETE" in stdout
    if success:
        return True, ""
    if returncode == 0:
        return False, "Simulation reported an error (see console output below)"
    return False, f"Process exited with code {returncode}"


def _run_env() -> dict | None:
    """Environment for launching the binary.

    The build links OceanWave3D.exe statically, but as a safety net on Windows we
    also put the MSYS2 mingw64\\bin dir on PATH so any dynamically-loaded
    libgfortran/libgcc runtime DLLs resolve. No-op (returns None) off Windows.
    """
    if sys.platform != "win32":
        return None
    mingw_bin = Path(os.environ.get("OCEANWAVE3D_MSYS2_ROOT", r"C:\msys64")) / "mingw64" / "bin"
    if not mingw_bin.exists():
        return None
    env = os.environ.copy()
    env["PATH"] = str(mingw_bin) + os.pathsep + env.get("PATH", "")
    return env


@dataclass
class RunResult:
    run_id: str
    run_dir: str
    success: bool
    elapsed_seconds: float
    stdout: str
    stderr: str
    output: SimulationOutput | None = None
    error_message: str = ""
    params: dict = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"Run ID       : {self.run_id}",
            f"Status       : {'SUCCESS' if self.success else 'FAILED'}",
            f"Wall time    : {self.elapsed_seconds:.1f} s",
        ]
        if self.error_message:
            lines.append(f"Error        : {self.error_message}")
        if self.output:
            lines.append("")
            lines.append(self.output.summary())
        return "\n".join(lines)


def run_simulation(inp_content: str, label: str = "", params: dict | None = None) -> RunResult:
    """
    Write inp_content to a fresh run directory and execute OceanWave3D.

    Returns a RunResult with parsed output statistics.
    Raises RuntimeError if the solver isn't installed by any backend
    (no native binary and no Docker sandbox image).
    """
    backend = select_backend()
    if backend is None:
        raise RuntimeError(
            f"OceanWave3D isn't installed: no native binary at {BINARY_PATH} and no "
            "Docker sandbox image. Build it first (install_oceanwave3d) — see README.md."
        )

    # Create unique run directory. Run IDs are second-resolution timestamps, so two
    # runs started within the same second would collide — uniquify with a numeric
    # suffix instead of crashing with FileExistsError.
    SIMULATIONS_DIR.mkdir(exist_ok=True)
    base_id = _make_run_id(label)
    run_id = base_id
    n = 1
    while True:
        run_dir = SIMULATIONS_DIR / run_id
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            n += 1
            run_id = f"{base_id}_{n}"

    SESSION_RUN_IDS.append(run_id)

    inp_path = run_dir / INP_FILENAME
    inp_path.write_text(inp_content)
    if params:
        (run_dir / "params.json").write_text(json.dumps(params, indent=2))

    t0 = time.monotonic()
    if backend == "docker":
        # Run inside the sandbox container; run_dir is bind-mounted at /work so the
        # solver writes fort.* / LOG.txt straight into it for the parser below.
        from . import docker_runner
        ex = docker_runner.run(run_dir, INP_FILENAME, TIMEOUT_SECONDS)
        elapsed = time.monotonic() - t0
        if ex.launch_error:
            success, stdout, stderr, error_msg = False, "", "", ex.launch_error
        elif ex.timed_out:
            success, stdout, stderr = False, "", ""
            error_msg = f"Simulation timed out after {TIMEOUT_SECONDS}s."
        else:
            stdout, stderr = ex.stdout, ex.stderr
            success, error_msg = _classify(ex.returncode, stdout)
    else:
        try:
            result = subprocess.run(
                [str(BINARY_PATH), INP_FILENAME],
                cwd=str(run_dir),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                env=_run_env(),
                # Don't inherit the MCP server's stdin pipe, and on Windows run the
                # console binary with no console window so it neither flashes a window
                # nor lets console control events reach the (GUI-launched) server.
                stdin=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            elapsed = time.monotonic() - t0
            stdout = result.stdout
            stderr = result.stderr
            success, error_msg = _classify(result.returncode, stdout)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            success = False
            stdout = ""
            stderr = ""
            error_msg = f"Simulation timed out after {TIMEOUT_SECONDS}s."
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            success = False
            stdout = ""
            stderr = ""
            error_msg = str(exc)

    # Parse output even on partial failure (some files may exist)
    sim_output: SimulationOutput | None = None
    if success or (run_dir / "fort.101").exists():
        try:
            sim_output = load_output(str(run_dir))
        except Exception as exc:  # noqa: BLE001
            error_msg += f" | Output parse error: {exc}"

    return RunResult(
        run_id=run_id,
        run_dir=str(run_dir),
        success=success,
        elapsed_seconds=elapsed,
        stdout=stdout,
        stderr=stderr,
        output=sim_output,
        error_message=error_msg,
        params=params or {},
    )


def _make_run_id(label: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c if c.isalnum() else "_" for c in label)[:30]
    return f"{ts}_{safe_label}" if safe_label else ts

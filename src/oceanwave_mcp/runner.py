"""
Runs the OceanWave3D binary in an isolated temp directory and returns results.

Each call to run_simulation():
  1. Creates a unique sub-directory under SIMULATIONS_DIR.
  2. Writes the .inp file there.
  3. Executes the binary (cwd = that directory).
  4. Captures stdout/stderr and parses fort.1XX output files.
  5. Returns a RunResult with stats and raw console output.
"""
import os
import subprocess
import sys
import tempfile
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
TIMEOUT_SECONDS = 300  # 5-minute hard limit per simulation


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


def run_simulation(inp_content: str, label: str = "") -> RunResult:
    """
    Write inp_content to a fresh run directory and execute OceanWave3D.

    Returns a RunResult with parsed output statistics.
    Raises RuntimeError if the binary cannot be found.
    """
    binary = Path(BINARY_PATH)
    if not binary.exists():
        raise RuntimeError(
            f"OceanWave3D binary not found at {binary}. "
            "Please build the Fortran code first — see README.md for platform-specific instructions."
        )

    # Create unique run directory
    SIMULATIONS_DIR.mkdir(exist_ok=True)
    run_id = _make_run_id(label)
    run_dir = SIMULATIONS_DIR / run_id
    run_dir.mkdir()

    inp_path = run_dir / INP_FILENAME
    inp_path.write_text(inp_content)

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            [str(binary), INP_FILENAME],
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        elapsed = time.monotonic() - t0
        stdout = result.stdout
        stderr = result.stderr
        # The Fortran code may STOP with exit code 0 but print "Error:" to stdout
        fortran_error = any(
            marker in stdout for marker in ("Error:", "error:", "ERROR:", "JOB IS NOT")
        )
        success = result.returncode == 0 and not fortran_error and "JOB IS COMPLETE" in stdout
        if not success and result.returncode == 0:
            error_msg = "Simulation reported an error (see console output below)"
        else:
            error_msg = "" if success else f"Process exited with code {result.returncode}"
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
    )


def _make_run_id(label: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(c if c.isalnum() else "_" for c in label)[:30]
    return f"{ts}_{safe_label}" if safe_label else ts

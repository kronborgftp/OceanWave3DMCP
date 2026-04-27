"""
OceanWave3D MCP Server

Exposes three tools to the LLM:
  1. list_scenarios       — what simulations can I run?
  2. run_simulation       — run a simulation and get statistics back
  3. get_detailed_results — re-read output from a previous run
"""
import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .inp_builder import SCENARIOS, build_inp
from .runner import run_simulation as _run, RunResult
from .output_parser import snapshots_to_text_table, load_output

mcp = FastMCP("OceanWave3D")


# ---------------------------------------------------------------------------
# Tool 1: list_scenarios
# ---------------------------------------------------------------------------

@mcp.tool()
def list_scenarios() -> str:
    """
    List all available OceanWave3D simulation scenarios with descriptions
    and the parameters each one accepts.

    Call this first to understand what simulations are possible.
    """
    lines = ["Available simulation scenarios:\n"]
    for name, info in SCENARIOS.items():
        lines.append(f"## {name}")
        lines.append(info["description"])
        lines.append("\nParameters:")
        for param, desc in info["parameters"].items():
            lines.append(f"  {param}: {desc}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2: run_simulation
# ---------------------------------------------------------------------------

@mcp.tool()
def run_simulation(
    scenario: str,
    wave_height: Optional[float] = None,
    water_depth: Optional[float] = None,
    wave_period: Optional[float] = None,
    domain_length: Optional[float] = None,
    grid_points_x: Optional[int] = None,
    vertical_layers: Optional[int] = None,
    num_periods: Optional[float] = None,
    nonlinear: Optional[bool] = None,
    label: str = "",
) -> str:
    """
    Run an OceanWave3D simulation and return a summary of the results.

    Parameters
    ----------
    scenario : str
        One of the scenario names returned by list_scenarios().
        E.g. "stream_function_wave", "linear_regular_wave", "nonlinear_standing_wave".
    wave_height : float, optional
        Wave height H in metres (crest to trough).
    water_depth : float, optional
        Still-water depth h in metres.
    wave_period : float, optional
        Wave period T in seconds.
    domain_length : float, optional
        Horizontal domain length Lx in metres. Auto-computed from wave
        parameters if omitted.
    grid_points_x : int, optional
        Number of horizontal grid points Nx. More points = higher accuracy
        but longer runtime. Use odd numbers (e.g. 65, 129, 257).
    vertical_layers : int, optional
        Number of vertical grid layers Nz (default 9).
    num_periods : float, optional
        How many wave periods to simulate (controls total simulation time).
    nonlinear : bool, optional
        True for fully nonlinear equations (default), False for linear.
    label : str, optional
        Human-readable label stored with the run (for later retrieval).

    Returns
    -------
    A text summary with run ID, wall time, and wave statistics.
    The run ID can be passed to get_detailed_results() for the full dataset.
    """
    # Build kwargs dict, omitting None values so defaults in inp_builder apply
    kwargs = {k: v for k, v in {
        "wave_height": wave_height,
        "water_depth": water_depth,
        "wave_period": wave_period,
        "domain_length": domain_length,
        "grid_points_x": grid_points_x,
        "vertical_layers": vertical_layers,
        "num_periods": num_periods,
        "nonlinear": nonlinear,
    }.items() if v is not None}

    try:
        inp = build_inp(scenario, **kwargs)
    except (ValueError, TypeError) as exc:
        return f"ERROR building input file: {exc}"

    try:
        result: RunResult = _run(inp, label=label or scenario)
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    summary = result.summary()

    if not result.success:
        tail = (result.stdout + result.stderr)[-800:]
        return f"{summary}\n\nLast console output:\n{tail}"

    return f"{summary}\n\nTo inspect the full time-series data call:\n  get_detailed_results(run_id=\"{result.run_id}\")"


# ---------------------------------------------------------------------------
# Tool 3: get_detailed_results
# ---------------------------------------------------------------------------

@mcp.tool()
def get_detailed_results(run_id: str, max_snapshots: int = 5) -> str:
    """
    Retrieve detailed free-surface elevation data from a completed simulation run.

    Parameters
    ----------
    run_id : str
        The run ID returned by run_simulation().
    max_snapshots : int, optional
        How many evenly-spaced time snapshots to include in the table (default 5).
        Each snapshot shows surface elevation E [m] at ~8 positions along the domain.

    Returns
    -------
    A text table with columns: snapshot_index, x [m], E [m], P [m²/s].
    """
    from .runner import SIMULATIONS_DIR
    run_dir = SIMULATIONS_DIR / run_id
    if not run_dir.exists():
        return (
            f"Run directory '{run_id}' not found under simulations/. "
            "Please check the run_id returned by run_simulation()."
        )

    try:
        out = load_output(str(run_dir))
    except Exception as exc:  # noqa: BLE001
        return f"ERROR loading output from {run_dir}: {exc}"

    if not out.snapshots:
        return f"No fort.1XX output files found in {run_dir}. The simulation may not have produced output."

    lines = [
        out.summary(),
        "",
        "--- Free-surface elevation table (selected snapshots) ---",
        snapshots_to_text_table(out, max_snapshots=max_snapshots),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

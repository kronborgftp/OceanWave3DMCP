"""
OceanWave3D MCP Server

Exposes tools to the LLM:
  1. list_scenarios        — what simulations can I run?
  2. run_simulation        — run a simulation and get statistics back
  3. get_detailed_results  — re-read output from a previous run
  4. generate_visualization — render GIF/PNG from solver output (not Claude-generated)
  5. check_installation    — is OceanWave3D built and ready?
  6. install_oceanwave3d   — build OceanWave3D from the licensed source files
  7. installation_status   — progress of an in-flight build
"""
import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

from . import installer
from .inp_builder import SCENARIOS, build_inp
from .runner import run_simulation as _run, RunResult, BINARY_PATH
from .output_parser import snapshots_to_text_table, load_output
# visualizer is imported lazily inside generate_visualization() to avoid
# loading matplotlib/Pillow at server startup (would exceed Claude Desktop's
# connection timeout)

_SERVER_INSTRUCTIONS = """\
You are connected to OceanWave3D — a professional nonlinear wave simulation
tool used in engineering and research. These rules are ABSOLUTE and apply for
the entire session.

━━━ SIMULATION OUTPUT — STRICT DISPLAY RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
run_simulation() returns a complete engineering recap with four fixed sections:
  [1] Run Info  [2] Input Parameters  [3] Results  [4] Output Files

MANDATORY rules — no exceptions:
1. Display the ENTIRE recap VERBATIM. Do not skip, merge, or reorder sections.
2. Do NOT add ANY text before the recap (no "Simulation complete!", no summary).
3. Do NOT add ANY text after the recap (no "Note on the deviation", no physics
   commentary, no "The wave shows...", no "This is consistent with...").
4. Do NOT reformat the tables — keep each section as a separate table.
5. The deviation % is a solver-computed result. Do not explain or qualify it.
6. Every value is exact solver output — do not round, paraphrase, or annotate.

The recap is a self-contained engineering document. Your job is to display it
unchanged. Any text you add is wrong by definition.

━━━ VISUALIZATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• To show a wave plot, call generate_visualization(run_id="...").
• The image returned IS the visualization — embed it directly, add nothing.
• Do NOT write matplotlib, Chart.js, or any analytical wave code.
  Claude-generated plots are wrong — they guess the shape instead of reading
  the solver output.

━━━ GENERAL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Same inputs → identical output every time. This is an engineering tool.
• Do not offer to re-run unless the user asks.
"""

mcp = FastMCP("OceanWave3D", instructions=_SERVER_INSTRUCTIONS)


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
):
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
    A structured engineering recap table. IMPORTANT: display it exactly as
    returned. Do not rewrite, reformat, summarise, or add commentary of your own.
    """
    # If the solver isn't built yet, suggest installing it instead of failing cryptically.
    if not BINARY_PATH.exists():
        return (
            "OceanWave3D isn't installed yet, so simulations can't run.\n\n"
            "Next steps:\n"
            "  1. Call check_installation() to see what's needed.\n"
            "  2. Call install_oceanwave3d() to build it from your licensed source files.\n"
            "  3. Poll installation_status() until it reports 'succeeded', then re-run this."
        )

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
        inp, params = build_inp(scenario, **kwargs)
    except (ValueError, TypeError) as exc:
        return f"ERROR building input file: {exc}"

    try:
        result: RunResult = _run(inp, label=label or scenario, params=params)
    except RuntimeError as exc:
        return f"ERROR: {exc}"

    if not result.success:
        tail = (result.stdout + result.stderr)[-800:]
        return f"{result.summary()}\n\nLast console output:\n{tail}"

    # Save GIF to disk (background bonus — never blocks the response)
    try:
        from . import visualizer
        visualizer.generate_and_save_gif(result.run_dir)
    except Exception:  # noqa: BLE001
        pass

    return _format_recap(result)


# ---------------------------------------------------------------------------
# Recap + wave-data helpers
# ---------------------------------------------------------------------------

def _format_recap(result: RunResult) -> str:
    """Build a structured, deterministic recap for a completed simulation run."""
    p = result.params
    out = result.output
    run_dir = Path(result.run_dir)

    scenario_labels = {
        "stream_function_wave":    "Stream Function Wave (nonlinear regular wave)",
        "linear_regular_wave":     "Linear Regular Wave",
        "nonlinear_standing_wave": "Nonlinear Standing Wave",
    }
    scenario_name = scenario_labels.get(p.get("scenario", ""), p.get("scenario", "unknown"))
    steepness = (p["wave_height_m"] / p["wavelength_m"]
                 if p.get("wave_height_m") and p.get("wavelength_m") else None)

    lines = [
        f"## OceanWave3D — {scenario_name}",
        "",
        "### [1] Run Info",
        "| Field | Value |",
        "|---|---|",
        f"| Scenario | {scenario_name} |",
        f"| Run ID | `{result.run_id}` |",
        f"| Status | SUCCESS |",
        f"| Wall time | {result.elapsed_seconds:.1f} s |",
        "",
        "### [2] Input Parameters",
        "| Parameter | Value |",
        "|---|---|",
        f"| Wave height | {p.get('wave_height_m', '?')} m |",
        f"| Water depth | {p.get('water_depth_m', '?')} m |",
        f"| Wave period | {p.get('wave_period_s', '?')} s |",
        f"| Wavelength (λ) | {p.get('wavelength_m', '?')} m |",
        f"| Domain length | {p.get('domain_length_m', '?')} m |",
        f"| Grid points (x) | {p.get('grid_points_x', '?')} |",
        f"| Vertical layers | {p.get('vertical_layers', '?')} |",
        f"| Sim. duration | {p.get('num_periods', '?')} periods "
        f"({p.get('num_steps', '?')} steps × {p.get('timestep_s', '?')} s) |",
        f"| Equations | {'Fully nonlinear' if p.get('nonlinear') else 'Linear'} |",
    ]
    if steepness is not None:
        lines.append(f"| Wave steepness H/λ | {steepness:.4f} |")
    lines.append("")

    if out:
        target_H = p.get("wave_height_m")
        dev_row = ""
        if target_H and target_H > 0:
            pct = 100.0 * abs(out.wave_height_measured - target_H) / target_H
            dev_row = f"| Deviation from specified H | {pct:.1f}% |"

        lines += [
            "### [3] Results",
            "| Metric | Value |",
            "|---|---|",
            f"| Max surface elevation | {out.max_elevation:+.4f} m |",
            f"| Min surface elevation | {out.min_elevation:+.4f} m |",
            f"| Measured H (steady, last 50%) | {out.wave_height_measured:.4f} m |",
            f"| RMS elevation | {out.rms_elevation:.5f} m |",
        ]
        if dev_row:
            lines.append(dev_row)
        lines.append("")

        first = run_dir / out.files_found[0] if out.files_found else None
        last  = run_dir / out.files_found[-1] if out.files_found else None
        lines += [
            "### [4] Output Files",
            f"**Directory:** `{run_dir}`",
            "",
            "| File | Path |",
            "|---|---|",
            f"| params.json | `{run_dir / 'params.json'}` |",
            f"| input.inp | `{run_dir / 'input.inp'}` |",
        ]
        if out.files_found:
            lines.append(f"| {out.files_found[0]} | `{run_dir / out.files_found[0]}` |")
            if len(out.files_found) > 2:
                lines.append(f"| … ({len(out.files_found)} snapshot files total) | … |")
            if len(out.files_found) > 1:
                lines.append(f"| {out.files_found[-1]} (final) | `{run_dir / out.files_found[-1]}` |")
        lines.append("")

        lines += [
            "<details>",
            "<summary>All snapshot files</summary>",
            "",
            "| File | Path |",
            "|---|---|",
        ]
        for fname in out.files_found:
            lines.append(f"| {fname} | `{run_dir / fname}` |")
        lines += ["", "</details>", ""]

    return "\n".join(lines)


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

    lines = []

    params_file = run_dir / "params.json"
    if params_file.exists():
        try:
            p = json.loads(params_file.read_text())
            lines += [
                "--- Simulation parameters ---",
                f"  Scenario        : {p.get('scenario', '?')}",
                f"  Wave height     : {p.get('wave_height_m', '?')} m",
                f"  Water depth     : {p.get('water_depth_m', '?')} m",
                f"  Wave period     : {p.get('wave_period_s', '?')} s",
                f"  Wavelength      : {p.get('wavelength_m', '?')} m",
                f"  Domain length   : {p.get('domain_length_m', '?')} m",
                f"  Grid points (x) : {p.get('grid_points_x', '?')}",
                f"  Vertical layers : {p.get('vertical_layers', '?')}",
                f"  Num periods     : {p.get('num_periods', '?')}",
                f"  Equations       : {'Fully nonlinear' if p.get('nonlinear') else 'Linear'}",
                "",
            ]
        except Exception:  # noqa: BLE001
            pass

    lines += [
        "--- Output statistics ---",
        out.summary(),
        "",
        "--- Free-surface elevation table (selected snapshots) ---",
        snapshots_to_text_table(out, max_snapshots=max_snapshots),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 4: generate_visualization
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_visualization(run_id: str) -> Image:
    """
    Generate a visualization from a completed OceanWave3D simulation run.

    Reads the fort.1XX solver output files directly and renders them with
    matplotlib. The image is derived entirely from solver data — no guessing,
    no interpolation. Same run always produces the same image.

    Returns a PNG panel showing evenly-spaced time snapshots embedded directly
    in the chat. Also saves an animated GIF to the run directory for external
    viewing. Do not generate a separate chart — this IS the visualization.

    Parameters
    ----------
    run_id : str
        The run ID returned by run_simulation().
    """
    from .runner import SIMULATIONS_DIR
    run_dir = SIMULATIONS_DIR / run_id
    if not run_dir.exists():
        raise ValueError(
            f"Run directory '{run_id}' not found. "
            "Check the run_id returned by run_simulation()."
        )

    from . import visualizer  # lazy — keeps matplotlib out of startup path

    try:
        # Always return PNG — Claude Desktop only renders image/png inline.
        # GIF is saved to disk for external viewing.
        png_data = visualizer.generate_panel_png(str(run_dir))
        try:
            gif_path = visualizer.generate_and_save_gif(str(run_dir))
        except Exception:  # noqa: BLE001
            gif_path = None  # GIF is bonus; don't let it fail the whole call
        return Image(data=png_data, format="png")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Visualization failed: {exc}\n"
            "DO NOT generate a chart yourself — report this error to the user instead."
        ) from exc


# ---------------------------------------------------------------------------
# Tool 5: check_installation
# ---------------------------------------------------------------------------

@mcp.tool()
def check_installation() -> str:
    """
    Check whether OceanWave3D is built and ready to run, and report exactly
    what (if anything) is still missing to build it.

    Call this when a simulation fails because the solver isn't installed, or
    before trying install_oceanwave3d().
    """
    p = installer.check_prerequisites()

    if p["binary_installed"]:
        return f"OceanWave3D is installed and ready at:\n  {p['binary_path']}"

    def mark(ok: bool) -> str:
        return "[OK]" if ok else "[MISSING]"

    lines = ["OceanWave3D is NOT installed yet. Build readiness:\n"]

    lines.append("Compiler toolchain:")
    for tool, ok in p["tools"].items():
        lines.append(f"  {mark(ok)} {tool}")

    lines.append(f"\nThird-party source files (folder: {p['files_dir']}):")
    for label, ok in p["tarballs"].items():
        lines.append(f"  {mark(ok)} {installer.REQUIRED_TARBALLS[label]}")

    lines.append(f"\nFortran source submodule: {mark(p['submodule_ready'])}")
    if not p["submodule_ready"]:
        lines.append("  Run: git submodule update --init")

    lines.append("")
    if p["missing_tools"]:
        lines.append("Install the missing compiler tools first:")
        lines.append(f"  {p['tool_install_hint']}")
    if p["missing_files"]:
        lines.append(
            f"I've created this folder for you — drop the missing files into it:\n"
            f"  {p['files_dir']}"
        )
        lines.append("\nWhere to get each missing file:")
        for label in p["missing_file_labels"]:
            src = p["file_sources"][label]
            fname = installer.REQUIRED_TARBALLS[label]
            lines.append(f"  - {fname}  ({src['name']})")
            lines.append(f"      {src['url']}")
            lines.append(f"      {src['note']}")
        lines.append(
            f"\nDTU course work: the bundle is usually provided by the OceanWave3D "
            f"maintainers — contact {p['maintainer_contact']}."
        )
        lines.append(
            f"(To use a different folder, set the {installer.FILES_DIR_ENV} "
            "environment variable.)"
        )

    if p["can_build"]:
        lines.append("Everything needed is present — call install_oceanwave3d() to build.")
    else:
        lines.append("Resolve the items above, then call install_oceanwave3d().")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 6: install_oceanwave3d
# ---------------------------------------------------------------------------

@mcp.tool()
def install_oceanwave3d(paid_files_dir: Optional[str] = None) -> str:
    """
    Build OceanWave3D from the licensed source files and install the binary.

    This compiles the third-party libraries (LAPACK/BLAS, SPARSKIT2, Harwell)
    and links the solver. It runs in the background because it takes several
    minutes; poll installation_status() to follow progress.

    Parameters
    ----------
    paid_files_dir : str, optional
        Folder containing the licensed tarballs (Harwell.tar.gz, SPARSKIT2.tar.gz,
        lapack-3.3.1.tgz). Defaults to ~/Documents/OceanWave3D_Files or the
        OCEANWAVE3D_FILES environment variable.
    """
    import os
    if paid_files_dir:
        os.environ[installer.FILES_DIR_ENV] = paid_files_dir

    result = installer.start_background_install()

    if result["started"]:
        return (
            "Build started in the background (this takes several minutes).\n"
            "Poll installation_status() to follow progress; it will report "
            "'succeeded' when bin/OceanWave3D is ready."
        )

    reason = result.get("reason")
    if reason == "already_installed":
        return "OceanWave3D is already installed — no build needed."
    if reason == "already_running":
        return "A build is already running. Call installation_status() to follow it."

    # missing_prerequisites — reuse the detailed readiness report.
    return "Cannot start the build yet.\n\n" + check_installation()


# ---------------------------------------------------------------------------
# Tool 7: installation_status
# ---------------------------------------------------------------------------

@mcp.tool()
def installation_status() -> str:
    """
    Report the progress of an OceanWave3D build started by install_oceanwave3d().

    States: 'running', 'succeeded', 'failed', or 'none' (no build attempted).
    Includes the tail of the build log for diagnosing failures.
    """
    s = installer.installation_status()
    state = s["state"]

    if state == "none":
        return "No installation has been started. Call install_oceanwave3d() to begin."

    lines = [f"State: {state}"]
    if s["elapsed_seconds"] is not None:
        lines.append(f"Elapsed: {s['elapsed_seconds']:.0f} s")
    lines.append(f"Binary installed: {s['binary_installed']}")
    if s["error"]:
        lines.append(f"Error: {s['error']}")
    if s["log_tail"]:
        lines.append("\n--- build log (tail) ---")
        lines.append(s["log_tail"])

    if state == "succeeded":
        lines.append("\nOceanWave3D is ready — you can now run simulations.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

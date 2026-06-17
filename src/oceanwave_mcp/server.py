"""
OceanWave3D MCP Server

Exposes tools to the LLM:
  1. list_scenarios        — what simulations can I run?
  2. run_simulation        — run a simulation and get statistics back
  3. get_detailed_results  — re-read output from a previous run
  4. generate_visualization — render GIF/PNG from solver output (not Claude-generated)
  5. get_visualization_link — localhost link to the interactive viewer
                              (animated, annotated, side-by-side comparison;
                              chat clients render inline PNG but not GIFs)
  6. generate_kinematics_visualization — run OceanWave3D's own ReadKinematics.m
                              (via Octave) to plot subsurface velocity/pressure
                              kinematics the free-surface views don't show
  7. check_installation    — is OceanWave3D built and ready? (native or Docker)
  8. install_oceanwave3d   — build OceanWave3D from the licensed source files,
                             natively or as a Docker sandbox image
  9. installation_status   — progress of an in-flight build
 10. check_version         — which build of this server is running?
"""
import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image

# Support running this file directly (python server.py / VSCode Run) in
# addition to the installed `oceanwave-mcp` entry point: relative imports
# need the package context, so restore it when launched as a plain script.
if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "oceanwave_mcp"

from . import installer
from .inp_builder import SCENARIOS, build_inp
from .runner import run_simulation as _run, RunResult, BINARY_PATH, solver_ready
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
• Still image in chat: call generate_visualization(run_id="...", format="png").
  The returned PNG IS the visualization — embed it directly, add nothing.
• Anything richer (animation, annotated ocean cross-section, x–t heatmap,
  3D surface, comparing runs side by side): call
  get_visualization_link(run_id="...") and give the user the returned
  http://127.0.0.1:... URL as a clickable markdown link. The page is an
  interactive viewer with toggles — no need to render variants yourself.
• To compare runs visually, pass compare_with="other_run_id" to
  get_visualization_link — the viewer shows the runs side by side with
  linked playback, toggles, and scales.
• Subsurface kinematics (flow BELOW the surface — velocity field, velocity
  potential, shear through the water column): call
  generate_kinematics_visualization(run_id="..."). It runs OceanWave3D's own
  ReadKinematics.m script (via Octave) and returns a localhost viewer LINK
  (opening the Kinematics tab). Present that link as a clickable markdown link,
  exactly like get_visualization_link — do NOT try to embed the figures inline
  (chat clients don't render them). Use this when the user asks about
  velocities, the flow under the wave, particle motion, or kinematics — the
  free-surface views can't show that. (Requires Octave; if it's missing, or the
  run produced no kinematics data, the tool returns a one-line explanation
  instead of a link — relay it.)
• The image or link MUST appear in your final, user-visible response.
  NEVER show it only inside a thinking/reasoning block — the user cannot
  see content there. If you called the tool while reasoning, repeat the
  image/link in the visible response.
• Do NOT write matplotlib, Chart.js, or any analytical wave code.
  Claude-generated plots are wrong — they guess the shape instead of reading
  the solver output.

━━━ GENERAL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Same inputs → identical output every time. This is an engineering tool.
• Do not offer to re-run unless the user asks.
"""

mcp = FastMCP("OceanWave3D", instructions=_SERVER_INSTRUCTIONS)

# Bump these whenever you change the server, then verify with check_version()
# that Claude Desktop picked up the new code.
_VERSION = "0.8"
_VERSION_MESSAGE = ("Interactive viewer (cross-section, heatmap, 3D, compare) "
                    "+ subsurface kinematics via OceanWave3D's ReadKinematics.m, "
                    "shown in the viewer's Kinematics tab via a browser link; "
                    "Docker sandbox backend — build & run the solver in a "
                    "container from the three tarballs, no Fortran toolchain needed")


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
    stream_func_height_steps: Optional[int] = None,
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
    stream_func_height_steps : int, optional
        For scenario="stream_function_wave". Number of height-continuation
        steps the Fenton steady-wave solver ramps through to reach the target
        height (default 6, clamped to 60). Raise it only if a very steep /
        near-breaking wave fails to converge at initialization; the default
        converges the full real-scale range. Leave unset for other scenarios.
    label : str, optional
        Human-readable label stored with the run (for later retrieval).

    Returns
    -------
    A structured engineering recap table. IMPORTANT: display it exactly as
    returned. Do not rewrite, reformat, summarise, or add commentary of your own.
    """
    # If the solver isn't ready by EITHER backend (native binary or Docker
    # sandbox image), suggest installing it instead of failing cryptically.
    if not solver_ready():
        return (
            "OceanWave3D isn't installed yet, so simulations can't run.\n\n"
            "Next steps:\n"
            "  1. Call check_installation() to see what's needed.\n"
            "  2. Call install_oceanwave3d() to build it from your licensed source files\n"
            "     (natively, or as a Docker sandbox if you have Docker but no Fortran toolchain).\n"
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
        "stream_func_height_steps": stream_func_height_steps,
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
        # Scenarios with relaxation zones (generation + absorption) are open
        # propagating-wave domains: measure H in the clean interior, away from
        # the wave-maker/absorber transients that inflate a whole-domain
        # max-min. Closed-domain scenarios (e.g. the standing wave, zones=[])
        # have their largest crest-trough AT the walls, so keep the global one.
        has_zones = bool(p.get("zones"))
        measured_H = out.wave_height_interior if has_zones else out.wave_height_measured
        measured_label = ("Measured H (steady, mid-domain)" if has_zones
                          else "Measured H (steady, last 50%)")

        dev_row = ""
        if target_H and target_H > 0:
            pct = 100.0 * abs(measured_H - target_H) / target_H
            dev_row = f"| Deviation from specified H | {pct:.1f}% |"

        lines += [
            "### [3] Results",
            "| Metric | Value |",
            "|---|---|",
            f"| Max surface elevation | {out.max_elevation:+.4f} m |",
            f"| Min surface elevation | {out.min_elevation:+.4f} m |",
            f"| {measured_label} | {measured_H:.4f} m |",
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
def generate_visualization(run_id: str, format: str = "gif") -> Image:
    """
    Generate a visualization from a completed OceanWave3D simulation run.

    Reads the fort.1XX solver output files directly and renders them with
    matplotlib. The image is derived entirely from solver data — no guessing,
    no interpolation. Same run_id always produces the same image.

    Do not generate a separate chart — this IS the visualization.

    Parameters
    ----------
    run_id : str
        The run ID returned by run_simulation().
    format : str
        "gif" (default) — animated GIF cycling through all recorded time
        snapshots. "png" — single PNG of the final snapshot.

    Note: chat clients render the returned PNG inline but generally can NOT
    render animated GIFs inline. To show the user an animation, prefer
    get_visualization_link() and present the URL it returns.
    """
    from .runner import SIMULATIONS_DIR
    run_dir = SIMULATIONS_DIR / run_id
    if not run_dir.exists():
        raise ValueError(
            f"Run directory '{run_id}' not found. "
            "Check the run_id returned by run_simulation()."
        )
    if format not in ("gif", "png"):
        raise ValueError(f"format must be 'gif' or 'png', got '{format}'")

    from . import visualizer  # lazy — keeps matplotlib out of startup path

    try:
        if format == "png":
            return Image(data=visualizer.generate_final_png(str(run_dir)),
                         format="png")
        gif_data = visualizer.generate_gif_bytes(str(run_dir))
        # Keep a copy on disk for external viewing
        (run_dir / "animation.gif").write_bytes(gif_data)
        return Image(data=gif_data, format="gif")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Visualization failed: {exc}\n"
            "DO NOT generate a chart yourself — report this error to the user instead."
        ) from exc


# ---------------------------------------------------------------------------
# Tool 5: get_visualization_link
# ---------------------------------------------------------------------------

@mcp.tool()
def get_visualization_link(run_id: str, format: str = "gif",
                           compare_with: Optional[str] = None) -> str:
    """
    Create a localhost link that opens a simulation in the interactive
    results viewer in the user's browser.

    The viewer renders the solver output client-side and offers (all as
    user-controlled toggles — do not render variants yourself):
      - animated ocean cross-section: solid water fill, seabed, shaded wave
        generation/absorption zones, scale bars, optional 1.8 m human
        silhouette, axes in metres and a "t = … s" timestamp
      - x–t heatmap with a diverging blue–white–red colormap centred on the
        still-water level, colorbar labelled in metres
      - perspective 3D surface view
      - plain-language titles generated from the input parameters
      - side-by-side comparison of runs from this session with linked
        playback, toggles, and scales

    Chat clients render inline PNG tool results but NOT animated GIFs — so
    whenever the user wants the animation, a comparison, or any richer view,
    use this tool instead of generate_visualization() and hand them the link.

    Starts a small viewer server bound to 127.0.0.1 (this machine only) and
    returns the URL. The link stays valid while the MCP server is running.

    IMPORTANT: put the returned URL in your final, user-visible response as a
    clickable markdown link. A link mentioned only inside thinking/reasoning
    is invisible to the user.

    Parameters
    ----------
    run_id : str
        The run ID returned by run_simulation().
    format : str
        "gif" (default) — the viewer opens with the animation playing.
        "png" — the viewer opens paused on the final snapshot (note: for a
        still in the chat itself, generate_visualization(format="png")
        embeds it directly, which is usually better).
    compare_with : str, optional
        Comma-separated run ID(s) to open side by side with the primary run,
        with linked playback, toggles, and scales. Use when the user wants to
        compare simulations visually.
    """
    from .runner import SIMULATIONS_DIR
    run_dir = SIMULATIONS_DIR / run_id
    if not run_dir.exists():
        return (
            f"ERROR: run directory '{run_id}' not found. "
            "Check the run_id returned by run_simulation()."
        )
    if format not in ("gif", "png"):
        return f"ERROR: format must be 'gif' or 'png', got '{format}'"

    compare_ids = [c.strip() for c in (compare_with or "").split(",") if c.strip()]
    missing = [c for c in compare_ids if not (SIMULATIONS_DIR / c).exists()]
    if missing:
        return (
            f"ERROR: compare_with run(s) not found: {', '.join(missing)}. "
            "Check the run IDs returned by run_simulation()."
        )

    from . import visualizer      # lazy — keeps matplotlib out of startup path
    from . import viewer_server

    # Best effort: keep a GIF/PNG on disk so the viewer's download links work.
    # The interactive viewer reads solver data directly, so a render failure
    # must not block the link.
    try:
        target = run_dir / ("animation.gif" if format == "gif" else "final.png")
        if not target.exists():
            if format == "gif":
                visualizer.generate_and_save_gif(str(run_dir))
            else:
                target.write_bytes(visualizer.generate_final_png(str(run_dir)))
    except Exception:  # noqa: BLE001
        pass

    url = viewer_server.view_url(run_id, format, compare=compare_ids or None)
    label = ("run comparison" if compare_ids
             else "wave animation" if format == "gif" else "final frame")
    return (
        f"Interactive viewer link ready: {url}\n\n"
        f"Present it to the user in your visible response as a markdown link, "
        f"e.g. [Open the {label} in your browser]({url}). The page lets the "
        "user toggle annotations (water fill, seabed, zones, scale bars), "
        "switch between cross-section, heatmap, and 3D surface views, and "
        "compare runs from this session side by side. It is served from this "
        "machine (localhost only) and stays available while the OceanWave3D "
        "MCP server is running."
    )


# ---------------------------------------------------------------------------
# Tool 6: generate_kinematics_visualization
# ---------------------------------------------------------------------------

@mcp.tool()
def generate_kinematics_visualization(run_id: str,
                                      x_index: Optional[int] = None) -> str:
    """
    Show OceanWave3D's OWN subsurface-kinematics figures for a completed run, in
    the interactive browser viewer.

    Unlike generate_visualization (which shows the free surface), this exposes
    what the flow is doing *below* the surface — the velocity field, velocity
    potential and shear through the water column. That data is written by the
    solver to a binary kinematics file (Kinematics0N.bin) in every run but is
    not shown by the free-surface views.

    The figures are produced by OceanWave3D's own bundled post-processing script
    (utils/matlab/IO/ReadKinematics.m), run through GNU Octave — they are NOT
    Claude-generated. The six panels shown are:
      • surface elevation eta(t) at a column and eta(x) at the final time
      • vertical profiles of velocity potential phi(sigma)
      • vertical profiles of horizontal velocity u(sigma)
      • vertical profiles of vertical velocity w(sigma)
      • vertical profiles of shear du/dz(sigma)

    This tool generates the figures, then returns a localhost link that opens
    them in the interactive viewer (on its Kinematics tab) — the SAME way
    get_visualization_link presents the animation, heatmap and 3D views. Chat
    clients do not render tool-result images inline, so present the link; do
    NOT try to draw your own velocity/pressure plots.

    Parameters
    ----------
    run_id : str
        The run ID returned by run_simulation().
    x_index : int, optional
        1-based horizontal grid column for the vertical-profile plots.
        Defaults to mid-domain (the most physically active column).

    Returns
    -------
    A localhost viewer URL (open the Kinematics tab). If GNU Octave is missing
    or the run produced no kinematics data, returns a one-line explanation
    instead — relay it to the user.

    IMPORTANT: put the returned URL in your final, user-visible response as a
    clickable markdown link. A link mentioned only inside thinking/reasoning
    is invisible to the user.
    """
    from .runner import SIMULATIONS_DIR
    from . import octave_viz, viewer_server

    run_dir = SIMULATIONS_DIR / run_id
    if not run_dir.exists():
        return (
            f"ERROR: run directory '{run_id}' not found. "
            "Check the run_id returned by run_simulation()."
        )

    # Generate the figures up front so the viewer shows them immediately on open,
    # and so an Octave-missing / empty-run problem surfaces here as a clear chat
    # message rather than an empty viewer tab. Both raise RuntimeError with an
    # actionable message.
    try:
        octave_viz.generate_kinematics_plots(run_dir, x_index=x_index)
    except RuntimeError as exc:
        return str(exc)

    url = viewer_server.view_url(run_id, view="kinematics")
    return (
        f"Interactive kinematics viewer ready: {url}\n\n"
        f"Present it to the user in your visible response as a markdown link, "
        f"e.g. [Open the subsurface kinematics in your browser]({url}). The "
        "page opens on the Kinematics tab showing OceanWave3D's own "
        "ReadKinematics.m figures (surface elevation, and vertical profiles of "
        "velocity potential, horizontal/vertical velocity and shear), each as a "
        "separate panel with a Regenerate control. It is served from this "
        "machine (localhost only) and stays available while the OceanWave3D "
        "MCP server is running."
    )


# ---------------------------------------------------------------------------
# Tool: check_version
# ---------------------------------------------------------------------------

@mcp.tool()
def check_version() -> str:
    """
    Report which build of this MCP server is running.

    Returns the version tag and message set in the source code. Use this to
    verify that the client picked up the latest code after a restart.
    """
    return f"{_VERSION} - {_VERSION_MESSAGE}"


# ---------------------------------------------------------------------------
# Tool 6: check_installation
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
    docker = p["docker"]

    if p["binary_installed"]:
        return f"OceanWave3D is installed (native build) and ready at:\n  {p['binary_path']}"
    if docker["image_built"]:
        return (
            "OceanWave3D is installed as a Docker sandbox and ready.\n"
            f"  Image: {docker['image_tag']}\n"
            "  Simulations run inside the container automatically."
        )

    def mark(ok: bool) -> str:
        return "[OK]" if ok else "[MISSING]"

    lines = ["OceanWave3D is NOT installed yet. Build readiness:\n"]

    lines.append("Native build — compiler toolchain:")
    for tool, ok in p["tools"].items():
        lines.append(f"  {mark(ok)} {tool}")

    lines.append("\nDocker sandbox (alternative — no Fortran toolchain needed):")
    lines.append(f"  {mark(docker['docker_available'])} Docker daemon reachable")
    if not docker["docker_available"]:
        lines.append("    Install Docker Desktop / Docker Engine and make sure it's running.")

    lines.append(f"\nThird-party source files (folder: {p['files_dir']}):")
    for label, ok in p["tarballs"].items():
        lines.append(f"  {mark(ok)} {installer.REQUIRED_TARBALLS[label]}")

    lines.append(f"\nFortran source submodule: {mark(p['submodule_ready'])}")
    if not p["submodule_ready"]:
        lines.append("  Run: git submodule update --init")

    lines.append("")
    if p["missing_tools"] and not docker["docker_available"]:
        lines.append("Install the missing compiler tools (for a native build):")
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

    if p["can_build"] and p["can_build_docker"]:
        lines.append(
            "Everything needed is present for BOTH a native build and the Docker "
            "sandbox — call install_oceanwave3d() (auto-picks native), or "
            'install_oceanwave3d(backend="docker") to use the container.'
        )
    elif p["can_build"]:
        lines.append("Everything needed is present — call install_oceanwave3d() to build natively.")
    elif p["can_build_docker"]:
        lines.append(
            "No Fortran toolchain, but Docker is ready and the source files are "
            'present — call install_oceanwave3d() to build the sandbox image '
            "(it auto-selects Docker)."
        )
    else:
        lines.append("Resolve the items above, then call install_oceanwave3d().")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 7: install_oceanwave3d
# ---------------------------------------------------------------------------

@mcp.tool()
def install_oceanwave3d(paid_files_dir: Optional[str] = None,
                        backend: str = "auto") -> str:
    """
    Build OceanWave3D from the licensed source files and install it.

    Two backends are supported:
      • "native"  — compiles the third-party libraries (LAPACK/BLAS, SPARSKIT2,
        Harwell) and links the solver onto this machine (needs a Fortran
        toolchain: gfortran/make, or MSYS2 on Windows).
      • "docker"  — builds a self-contained sandbox image from the same three
        tarballs and runs simulations inside the container (needs only Docker —
        no Fortran toolchain). Easiest for users without a compiler set up.

    Either way it runs in the background (several minutes); poll
    installation_status() to follow progress.

    Parameters
    ----------
    paid_files_dir : str, optional
        Folder containing the licensed tarballs (Harwell.tar.gz, SPARSKIT2.tar.gz,
        lapack-3.3.1.tgz). Defaults to ~/Documents/OceanWave3D_Files or the
        OCEANWAVE3D_FILES environment variable.
    backend : str, optional
        "auto" (default — native if a Fortran toolchain is present, else Docker),
        "native", or "docker".
    """
    import os
    if paid_files_dir:
        os.environ[installer.FILES_DIR_ENV] = paid_files_dir

    try:
        result = installer.start_background_install(backend=backend)
    except ValueError as exc:
        return f"ERROR: {exc}"

    chosen = result.get("backend", backend)
    if result["started"]:
        artifact = ("the Docker sandbox image" if chosen == "docker"
                    else "bin/OceanWave3D")
        return (
            f"Build started in the background ({chosen} backend; this takes several "
            "minutes).\nPoll installation_status() to follow progress; it will "
            f"report 'succeeded' when {artifact} is ready."
        )

    reason = result.get("reason")
    if reason == "already_installed":
        return f"OceanWave3D is already installed ({chosen} backend) — no build needed."
    if reason == "already_running":
        return "A build is already running. Call installation_status() to follow it."

    # missing_prerequisites — reuse the detailed readiness report.
    return "Cannot start the build yet.\n\n" + check_installation()


# ---------------------------------------------------------------------------
# Tool 8: installation_status
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

    backend = s.get("backend", "native")
    lines = [f"State: {state}", f"Backend: {backend}"]
    if s["elapsed_seconds"] is not None:
        lines.append(f"Elapsed: {s['elapsed_seconds']:.0f} s")
    artifact = "Sandbox image built" if backend == "docker" else "Binary installed"
    lines.append(f"{artifact}: {s['artifact_ready']}")
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

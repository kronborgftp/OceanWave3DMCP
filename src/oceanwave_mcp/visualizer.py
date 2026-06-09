"""
Generates deterministic visualizations from OceanWave3D fort.1XX output.

All plots are derived directly from the solver's ASCII output files — no
interpolation, no guessing, no Claude-generated data. Same fort files always
produce the same pixels.

Claude Desktop only renders image/png inline. GIFs are saved to disk; the
tool always returns a PNG for in-chat display.
"""
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

from .output_parser import SimulationOutput, load_output


# Fixed visual style — never changes regardless of input
_LINE_COLOR  = "#1f77b4"
_STILL_COLOR = "#999999"
_GRID_ALPHA  = 0.3
_DPI         = 100
_GIF_FPS     = 5
_MAX_FRAMES  = 20
_N_PANELS    = 6   # panels in the inline PNG grid


def _ensure_deps() -> None:
    """Auto-install matplotlib and Pillow if missing — silent on success."""
    needed = [
        pkg for pkg, imp in [
            ("matplotlib>=3.7.0", "matplotlib"),
            ("Pillow>=10.0.0",    "PIL"),
        ]
        if importlib.util.find_spec(imp) is None
    ]
    if needed:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + needed
        )


def _time_per_snapshot(run_dir: Path) -> float:
    """Return simulated seconds between consecutive fort snapshots."""
    params_file = run_dir / "params.json"
    if not params_file.exists():
        return 0.0
    p = json.loads(params_file.read_text())
    dt        = float(p.get("timestep_s", 0))
    num_steps = int(p.get("num_steps", 0))
    if dt <= 0 or num_steps <= 0:
        return 0.0
    stride = max(2, num_steps // 100)
    return stride * dt


def _y_lim(out: SimulationOutput) -> tuple:
    abs_max = max(abs(out.max_elevation), abs(out.min_elevation))
    margin  = abs_max * 0.20
    return (out.min_elevation - margin, out.max_elevation + margin)


def _render_ax(ax, snap_x, snap_E, t_seconds, snap_idx, total_snaps, y_lim, plt, ticker):
    """Draw one wave frame onto an existing matplotlib Axes."""
    ax.plot(snap_x, snap_E, color=_LINE_COLOR, linewidth=1.5)
    ax.axhline(0.0, color=_STILL_COLOR, linestyle="--", linewidth=0.8)
    ax.set_xlim(snap_x[0], snap_x[-1])
    ax.set_ylim(y_lim[0], y_lim[1])
    ax.set_xlabel("x [m]", fontsize=8)
    ax.set_ylabel("η [m]", fontsize=8)
    ax.set_title(f"t = {t_seconds:.1f} s", fontsize=8)
    ax.grid(True, alpha=_GRID_ALPHA)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.3f"))
    ax.tick_params(labelsize=7)


def generate_panel_png(run_dir: str, n_panels: int = _N_PANELS) -> bytes:
    """
    Generate a multi-panel PNG showing n_panels evenly-spaced time snapshots.

    Returns raw PNG bytes. This is the primary inline-display format because
    Claude Desktop only renders image/png from MCP tool results.
    """
    _ensure_deps()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    out: SimulationOutput = load_output(run_dir)
    if not out.snapshots:
        raise ValueError(f"No fort.1XX snapshot files found in {run_dir}")

    dt_snap = _time_per_snapshot(Path(run_dir))
    n       = len(out.snapshots)
    panels  = min(n_panels, n)

    step    = max(1, n // panels)
    indices = list(range(0, n, step))[:panels]
    yl      = _y_lim(out)

    cols = 3
    rows = (panels + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 5, rows * 3), dpi=_DPI)
    axes_flat = axes.flat if hasattr(axes, "flat") else [axes]

    for ax, i in zip(axes_flat, indices):
        snap = out.snapshots[i]
        _render_ax(ax, snap.x, snap.E, i * dt_snap, i, n, yl, plt, ticker)

    # hide any unused subplot slots
    for ax in list(axes_flat)[len(indices):]:
        ax.set_visible(False)

    fig.suptitle(
        f"Surface elevation η(x, t)  —  {panels} snapshots from {n} total\n"
        f"run: {Path(run_dir).name}",
        fontsize=10,
    )
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=_DPI)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def generate_and_save_gif(run_dir: str, max_frames: int = _MAX_FRAMES) -> Path:
    """
    Generate an animated GIF and save it to run_dir/animation.gif.

    Returns the Path to the saved file. The GIF is not returned inline because
    Claude Desktop does not render image/gif from MCP tool results.
    """
    _ensure_deps()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    from PIL import Image as PILImage

    out: SimulationOutput = load_output(run_dir)
    if not out.snapshots:
        raise ValueError(f"No fort.1XX snapshot files found in {run_dir}")

    dt_snap = _time_per_snapshot(Path(run_dir))
    n       = len(out.snapshots)
    step    = max(1, n // max_frames)
    indices = list(range(0, n, step))[:max_frames]
    yl      = _y_lim(out)

    frames = []
    for i in indices:
        snap = out.snapshots[i]
        fig, ax = plt.subplots(figsize=(10, 4), dpi=_DPI)
        _render_ax(ax, snap.x, snap.E, i * dt_snap, i, n, yl, plt, ticker)
        ax.set_title(
            f"t = {i * dt_snap:.2f} s   (snapshot {i + 1}/{n})",
            fontsize=10,
        )
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", dpi=_DPI)
        plt.close(fig)
        buf.seek(0)
        frames.append(PILImage.open(buf).copy())

    gif_path = Path(run_dir) / "animation.gif"
    frames[0].save(
        gif_path,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / _GIF_FPS),
        loop=0,
        optimize=False,
    )
    return gif_path

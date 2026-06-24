"""
Generates deterministic visualizations from OceanWave3D fort.1XX output.

All plots are derived directly from the solver's ASCII output files — no
interpolation, no guessing, no Claude-generated data. Same fort files always
produce the same pixels.

The tool returns the image inline: an animated GIF cycling through all
recorded snapshots by default, or a single PNG of the final snapshot.
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .output_parser import SimulationOutput, load_output


# Fixed visual style — never changes regardless of input
_LINE_COLOR  = "#1f77b4"
_STILL_COLOR = "#999999"
_GRID_ALPHA  = 0.3
_DPI         = 100
_GIF_FPS     = 5

# matplotlib's pyplot keeps a process-global figure registry, and the backend
# selection + first import are likewise process-global, none of it thread-safe.
# run_simulation now pre-renders the GIF on a background thread, which can overlap
# with an explicit generate_visualization() call on a FastMCP worker thread (and
# with the startup warm_up() thread), so every touch of matplotlib — import,
# use("Agg"), and rendering — happens under this single lock.
_RENDER_LOCK = threading.Lock()


def _pyplot():
    """Import the Agg pyplot stack and return (plt, ticker).

    MUST be called with _RENDER_LOCK held: selecting the backend and the first
    `import matplotlib.pyplot` mutate process-global state and would otherwise
    race a concurrent render or the warm_up() thread. After the first call these
    are cheap no-ops (matplotlib caches the backend and the import)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    return plt, ticker


def atomic_write_bytes(path, data: bytes) -> None:
    """Write `data` to `path` atomically: a temp file in the same directory, then
    os.replace into place.

    Several code paths render and write the SAME deterministic file (the
    background pre-render in run_simulation, generate_visualization, and
    get_visualization_link). A plain write_bytes from two of them at once — or a
    daemon thread killed by process exit mid-write — leaves a truncated/corrupt
    file. os.replace is atomic on POSIX and Windows, so any reader sees either the
    old complete file or the new one, and concurrent writers simply end with the
    last (identical) bytes winning."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def warm_up() -> None:
    """Pay the cold first-import cost of the plotting stack up front, off the
    critical path. The first `import matplotlib.pyplot` (+ numpy, Pillow) in a
    fresh process is slow on Windows; doing it here (e.g. from a startup daemon
    thread) keeps the first real render fast. Imports only — renders nothing."""
    _ensure_deps()
    with _RENDER_LOCK:  # serialize backend + first import with any live render
        _pyplot()
        from PIL import Image  # noqa: F401  (warm Pillow too)


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
    """±120 % of the global peak across all snapshots — fixed per run."""
    peak = max(abs(out.max_elevation), abs(out.min_elevation))
    return (-1.2 * peak, 1.2 * peak)


def _render_ax(ax, snap_x, snap_E, t_seconds, snap_idx, total_snaps, y_lim, plt, ticker):
    """Draw one wave frame onto an existing matplotlib Axes."""
    ax.plot(snap_x, snap_E, color=_LINE_COLOR, linewidth=1.5)
    ax.axhline(0.0, color=_STILL_COLOR, linestyle="--", linewidth=0.8)
    ax.set_xlim(snap_x[0], snap_x[-1])
    ax.set_ylim(y_lim[0], y_lim[1])
    ax.set_xlabel("x [m]", fontsize=8)
    ax.set_ylabel("η [m]", fontsize=8)
    ax.set_title(
        f"t = {t_seconds:.2f} s   (snapshot {snap_idx + 1}/{total_snaps})",
        fontsize=10,
    )
    ax.grid(True, alpha=_GRID_ALPHA)
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.3f"))
    ax.tick_params(labelsize=7)


def _frame_png(out: SimulationOutput, i: int, dt_snap: float, y_lim: tuple,
               plt, ticker) -> io.BytesIO:
    """Render snapshot i as a PNG and return the buffer."""
    snap = out.snapshots[i]
    fig, ax = plt.subplots(figsize=(10, 4), dpi=_DPI)
    _render_ax(ax, snap.x, snap.E, i * dt_snap, i, len(out.snapshots),
               y_lim, plt, ticker)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=_DPI)
    plt.close(fig)
    buf.seek(0)
    return buf


def _load(run_dir: str) -> SimulationOutput:
    out: SimulationOutput = load_output(run_dir)
    if not out.snapshots:
        raise ValueError(f"No fort.1XX snapshot files found in {run_dir}")
    return out


def generate_final_png(run_dir: str) -> bytes:
    """Single PNG of the final recorded snapshot. Returns raw PNG bytes."""
    _ensure_deps()
    out     = _load(run_dir)
    dt_snap = _time_per_snapshot(Path(run_dir))
    i       = len(out.snapshots) - 1
    yl      = _y_lim(out)
    # Backend selection, first import and render all under the one lock.
    with _RENDER_LOCK:
        plt, ticker = _pyplot()
        return _frame_png(out, i, dt_snap, yl, plt, ticker).read()


def generate_gif_bytes(run_dir: str) -> bytes:
    """
    Animated GIF cycling through all recorded time snapshots.

    Returns raw GIF bytes. Every frame is read directly from the fort.1XX
    files — nothing is interpolated or estimated.
    """
    _ensure_deps()
    from PIL import Image as PILImage

    out     = _load(run_dir)
    dt_snap = _time_per_snapshot(Path(run_dir))
    yl      = _y_lim(out)

    # Render every frame's PNG under the lock (pyplot's figure registry is not
    # thread-safe); decode the resulting PNG buffers into PIL frames outside it,
    # so the lock isn't held across the cheap, thread-safe Pillow work.
    with _RENDER_LOCK:
        plt, ticker = _pyplot()
        png_bufs = [_frame_png(out, i, dt_snap, yl, plt, ticker)
                    for i in range(len(out.snapshots))]
    frames = [PILImage.open(b).copy() for b in png_bufs]

    buf = io.BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / _GIF_FPS),
        loop=0,
        optimize=False,
    )
    return buf.getvalue()


def generate_and_save_gif(run_dir: str) -> Path:
    """Generate the animated GIF and save it to run_dir/animation.gif (atomically,
    since the background pre-render and explicit viz calls can write it at once)."""
    gif_path = Path(run_dir) / "animation.gif"
    atomic_write_bytes(gif_path, generate_gif_bytes(run_dir))
    return gif_path

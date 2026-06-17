"""
Parses OceanWave3D ASCII output files (fort.101, fort.102, …).

Each file stores free-surface snapshots written at regular time intervals.
Format (4 columns per row, blank line between snapshots):

    x   y   E   P

where E is the surface elevation [m] and P is the velocity potential [m²/s].
For 2D runs (Ny=1), y is constant and can be ignored.
"""
import math
import os
import statistics
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Snapshot:
    x: List[float]          # x positions [m]
    E: List[float]          # surface elevation [m]
    P: List[float]          # velocity potential [m²/s]


@dataclass
class SimulationOutput:
    snapshots: List[Snapshot]
    run_dir: str
    files_found: List[str]

    # --- derived statistics (filled by _compute_stats) ---
    max_elevation: float = 0.0
    min_elevation: float = 0.0
    wave_height_measured: float = 0.0  # max_E - min_E over whole domain, last half of run
    wave_height_interior: float = 0.0  # robust H in the clean propagation interior (see _compute_stats)
    rms_elevation: float = 0.0
    num_snapshots: int = 0
    num_x_points: int = 0

    def summary(self) -> str:
        lines = [
            f"Output files : {', '.join(self.files_found)}",
            f"Snapshots    : {self.num_snapshots}",
            f"Grid points  : {self.num_x_points}",
            f"Max elevation: {self.max_elevation:.5f} m",
            f"Min elevation: {self.min_elevation:.5f} m",
            f"Wave height  : {self.wave_height_measured:.5f} m  (max - min, steady state)",
            f"RMS elevation: {self.rms_elevation:.5f} m",
        ]
        return "\n".join(lines)


def parse_fort_file(path: str) -> List[Snapshot]:
    """Parse a single fort.1XX ASCII file into a list of Snapshots."""
    snapshots: List[Snapshot] = []
    x_buf: List[float] = []
    E_buf: List[float] = []
    P_buf: List[float] = []

    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                # blank line → end of snapshot
                if x_buf:
                    snapshots.append(Snapshot(list(x_buf), list(E_buf), list(P_buf)))
                    x_buf.clear(); E_buf.clear(); P_buf.clear()
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                x_buf.append(float(parts[0]))
                E_buf.append(float(parts[2]))
                P_buf.append(float(parts[3]) if len(parts) > 3 else 0.0)
            except ValueError:
                continue

    # flush last block (file may not end with blank line)
    if x_buf:
        snapshots.append(Snapshot(list(x_buf), list(E_buf), list(P_buf)))

    return snapshots


def load_output(run_dir: str) -> SimulationOutput:
    """
    Load all fort.1XX files from run_dir and return a SimulationOutput.

    Each fort.NNN file contains exactly one time snapshot (one block of
    Nx rows: x y E P).  Files are sorted numerically; fort.100 is the
    initial condition (t=0), fort.101 onward are time steps.
    fort.23 and fort.999 are auxiliary files and are skipped.
    """
    def _sort_key(name: str) -> int:
        return int(name[5:])

    # fort.100 = t=0 snapshot, fort.101.. = time steps.
    # fort.999 = bathymetry (written by OceanWave3DT0Setup nr=899).
    # fort.23 and other low-numbered units are auxiliary I/O files.
    # Accept only fort.100..fort.898 (wave-field snapshots).
    candidates = sorted(
        (f for f in os.listdir(run_dir)
         if f.startswith("fort.") and f[5:].isdigit()
         and 100 <= int(f[5:]) <= 898),
        key=_sort_key,
    )

    all_snapshots: List[Snapshot] = []
    for fname in candidates:
        snaps = parse_fort_file(os.path.join(run_dir, fname))
        all_snapshots.extend(snaps)

    out = SimulationOutput(
        snapshots=all_snapshots,
        run_dir=run_dir,
        files_found=candidates,
    )
    _compute_stats(out)
    return out


def _compute_stats(out: SimulationOutput) -> None:
    if not out.snapshots:
        return

    out.num_snapshots = len(out.snapshots)
    out.num_x_points = len(out.snapshots[0].x)

    all_E = [e for s in out.snapshots for e in s.E]
    out.max_elevation = max(all_E)
    out.min_elevation = min(all_E)

    # steady-state wave height: use the last half of snapshots
    steady = out.snapshots[len(out.snapshots) // 2 :]
    steady_E = [e for s in steady for e in s.E]
    out.wave_height_measured = max(steady_E) - min(steady_E)

    # Robust wave height in the clean propagation interior. The whole-domain
    # max-min above is inflated by the generation zone (wave-maker overshoot)
    # and the absorption zone (small reflections build a standing-wave bump),
    # so it overstates H for an open propagating-wave run. Here we take each
    # column's crest-to-trough over the steady window and the MEDIAN across the
    # central half of the domain: every interior column sweeps the full
    # crest-trough (= H for a regular wave), and the median ignores the few
    # zone-adjacent columns that run high. _format_recap uses this for
    # scenarios that have relaxation zones; closed domains keep the global one.
    nx = min(len(s.E) for s in steady)
    if nx >= 4:
        col_crest_trough = [
            max(s.E[i] for s in steady) - min(s.E[i] for s in steady)
            for i in range(nx)
        ]
        interior = col_crest_trough[nx // 4 : nx - nx // 4]
        out.wave_height_interior = statistics.median(interior)
    else:
        out.wave_height_interior = out.wave_height_measured

    n = len(all_E)
    out.rms_elevation = math.sqrt(sum(e * e for e in all_E) / n) if n else 0.0


def snapshots_to_text_table(out: SimulationOutput, max_snapshots: int = 5) -> str:
    """
    Return a compact text representation of selected snapshots
    (suitable for returning to the LLM as context).
    """
    if not out.snapshots:
        return "No output data found."

    step = max(1, len(out.snapshots) // max_snapshots)
    selected = out.snapshots[::step][:max_snapshots]

    lines = ["snapshot_index  x[m]     E[m]      P[m²/s]"]
    for idx, snap in zip(range(0, len(out.snapshots), step), selected):
        for i in range(0, len(snap.x), max(1, len(snap.x) // 8)):
            lines.append(
                f"{idx:>14}  {snap.x[i]:>7.3f}  {snap.E[i]:>9.5f}  {snap.P[i]:>10.5f}"
            )
        lines.append("")  # blank between snapshots

    return "\n".join(lines)

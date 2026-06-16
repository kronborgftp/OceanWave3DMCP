"""
Run OceanWave3D's *own* bundled MATLAB post-processing scripts via GNU Octave.

This module does NOT author any plots. It invokes the scripts that ship inside
the OceanWave3D source tree (OceanWave3D-Fortran90/utils/matlab/) and saves the
figures those scripts draw. Concretely it runs the bundled

    utils/matlab/IO/ReadKinematics.m

which reads the binary kinematics file (Kinematics0N.bin) that OceanWave3D
already writes in every run, computes the subsurface kinematics (and pressure),
and — with its built-in `Plots` switch turned on — draws the canonical
OceanWave3D kinematics figures:

    * surface elevation eta(t) at a point
    * surface elevation eta(x) and slope at the final time
    * vertical profiles of the velocity potential phi(sigma)
    * vertical profiles of the horizontal velocity u(sigma)
    * vertical profiles of the vertical velocity w(sigma)
    * vertical profiles of the shear du/dz(sigma)

The only edit applied to the bundled script is flipping its own hard-coded
`Plots='no'` to `Plots='yes'` so its plotting block runs; the plotting code
itself is OceanWave3D's, unchanged. We then save whatever figures it created.

Requires GNU Octave (a free MATLAB-compatible engine). If it is missing the
caller is given an install hint rather than a cryptic failure.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

# Repo layout: this file is src/oceanwave_mcp/octave_viz.py → repo root is 2 up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_UTILS_MATLAB = _REPO_ROOT / "OceanWave3D-Fortran90" / "utils" / "matlab"
_READKIN = _UTILS_MATLAB / "IO" / "ReadKinematics.m"

# gfortran (our build) writes unformatted sequential files with 4-byte record
# markers → ReadKinematics.m's 32-bit reader. (Verified against real output.)
_NBITS = 32

# A complete kinematics file always carries the header record (grid metadata,
# sigma coordinate) plus at least one time slice, which is many kB. Anything
# smaller than this means the solver opened the file but never wrote usable
# data — treat it as "no kinematics" rather than trying (and failing) to plot.
_MIN_KIN_BYTES = 64

# How long to let Octave run before giving up (loading + differentiating a
# multi-MB kinematics file + rendering ~6 figures takes a few seconds).
_TIMEOUT_S = 180

INSTALL_HINT = (
    "GNU Octave is required to run OceanWave3D's bundled visualization scripts, "
    "but it was not found on PATH. Install it, then try again:\n"
    "  Fedora/RHEL  : sudo dnf install -y octave\n"
    "  Debian/Ubuntu: sudo apt-get install -y octave\n"
    "  macOS (brew) : brew install octave"
)


def octave_binary() -> Optional[str]:
    """Return the path to an Octave executable, or None if not installed."""
    for name in ("octave-cli", "octave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def octave_available() -> bool:
    return octave_binary() is not None


def kinematics_file(run_dir: Path, idn: int = 1) -> Optional[Path]:
    """Locate the kinematics binary for output region `idn`.

    OceanWave3D names it Kinematics0N.bin for N<10 (e.g. Kinematics01.bin) and
    KinematicsNN.bin otherwise — the same convention ReadKinematics.m expects.
    """
    name = f"Kinematics{idn:02d}.bin" if idn < 10 else f"Kinematics{idn}.bin"
    f = run_dir / name
    return f if f.is_file() else None


def has_kinematics_data(run_dir, idn: int = 1) -> bool:
    """True if the run has a non-empty kinematics file worth plotting.

    A 0-byte file (the solver opened it but never time-stepped) does not count —
    callers should treat that run as having no subsurface kinematics.
    """
    kin = kinematics_file(Path(run_dir), idn)
    return kin is not None and kin.stat().st_size >= _MIN_KIN_BYTES


def _patched_readkinematics(ip_expr: str = "round(nx/2)+1") -> str:
    """The bundled ReadKinematics.m with its own Plots switch turned on.

    Two minimal edits, both to the script's own parameters — the plotting code
    itself is OceanWave3D's, unchanged:
      * flip the hard-coded `Plots='no';` to `'yes';` so its plotting runs;
      * set the horizontal evaluation point `ip` for the profile plots. The
        script ships with `ip=1` (the domain boundary, near-quiescent) and a
        commented-out `ip=round(nx/2)+1` (mid-domain); we use the latter form
        so the profiles land on a physically meaningful column.
    """
    text = _READKIN.read_text()
    patched = text.replace("\nPlots='no';", "\nPlots='yes';")
    if patched == text:  # guard against upstream renaming the flag
        raise RuntimeError(
            "Could not enable plotting in the bundled ReadKinematics.m "
            "(the expected \"Plots='no';\" line was not found)."
        )
    # Clamp into range in Octave so a caller-supplied index can't go out of bounds.
    new = patched.replace(
        "ip=1; jp=1; % The horizontal grid point position to plot out",
        f"ip=min(max({ip_expr},1),nx); jp=1; % evaluation column set by MCP",
    )
    if new == patched:  # guard against upstream rewording the line
        raise RuntimeError(
            "Could not set the evaluation column in the bundled "
            "ReadKinematics.m (the expected `ip=1; jp=1;` line was not found)."
        )
    return new


def _driver(readkin_path: Path, run_dir: Path, out_dir: Path, idn: int) -> str:
    """Octave driver: set inputs, run the bundled script, save its figures."""
    # gnuplot toolkit renders to file without an X display.
    return f"""
graphics_toolkit('gnuplot');
addpath('{_UTILS_MATLAB / 'IO'}');
addpath('{_UTILS_MATLAB / 'Analysis'}');
addpath('{_UTILS_MATLAB / 'visualization'}');
cd('{run_dir}');
Nbits={_NBITS}; idn={idn};
ok = true;
try
  source('{readkin_path}');   % OceanWave3D's own reader + plotting block
catch err
  ok = false;
  fprintf(2, 'ReadKinematics error: %s\\n', err.message);
end
figs = sort(get(0, 'children'));
items = struct('file', {{}}, 'title', {{}});
for k = 1:numel(figs)
  f = figs(k);
  ttl = '';
  ax = get(f, 'currentaxes');
  if ~isempty(ax)
    h = get(ax, 'title');
    if ~isempty(h)
      ttl = get(h, 'string');
      if iscell(ttl), ttl = strjoin(ttl, ' '); end
    end
    if isempty(ttl)            % some bundled plots set only a y-label
      hy = get(ax, 'ylabel');
      if ~isempty(hy), ttl = get(hy, 'string'); end
      if iscell(ttl), ttl = strjoin(ttl, ' '); end
    end
  end
  fn = sprintf('kinematics_%02d.png', k);
  print(f, fullfile('{out_dir}', fn), '-dpng', '-S900,560');
  items(end+1).file = fn;
  items(end).title = ttl;
end
fid = fopen(fullfile('{out_dir}', 'kinematics_index.json'), 'w');
fputs(fid, jsonencode(items));
fclose(fid);
printf('SAVED %d figures\\n', numel(figs));
if ~ok, exit(3); end
"""


def generate_kinematics_plots(run_dir, idn: int = 1,
                              x_index: Optional[int] = None) -> List[dict]:
    """Run the bundled kinematics visualization and save its figures.

    Returns a list of {"file": <name>, "path": <abs path>, "title": <caption>}
    for the PNGs OceanWave3D's ReadKinematics.m produced, written into run_dir.

    x_index : optional 1-based horizontal grid column for the vertical-profile
        plots. Defaults to mid-domain (the script author's own commented-out
        choice). Clamped into range by the script.

    Raises RuntimeError (with an actionable message) if Octave is missing, the
    kinematics file is absent, or the script produced no figures.
    """
    # Absolute path is essential: the driver cd's into the run dir, after which
    # any relative output path would resolve against the wrong directory.
    run_dir = Path(run_dir).resolve()
    octave = octave_binary()
    if octave is None:
        raise RuntimeError(INSTALL_HINT)
    if not run_dir.is_dir():
        raise RuntimeError(f"Run directory not found: {run_dir}")

    kin = kinematics_file(run_dir, idn)
    if kin is None:
        raise RuntimeError(
            f"No kinematics file (Kinematics{idn:02d}.bin) in {run_dir}. "
            "It is written automatically by runs that request kinematics "
            "output; re-run the simulation if this run predates that."
        )
    # A 0-byte (or truncated header) file means the solver opened the kinematics
    # file but never time-stepped — typically the run diverged or crashed during
    # setup (steep/unstable wave parameters). Give that diagnosis instead of
    # letting ReadKinematics.m fail deep inside with a cryptic Octave error.
    if kin.stat().st_size < _MIN_KIN_BYTES:
        raise RuntimeError(
            f"The kinematics file for this run is empty "
            f"({kin.stat().st_size} bytes), so there is nothing to plot. The "
            "simulation requested kinematics output but did not complete "
            "time-stepping — it most likely diverged or crashed during setup "
            "(often too steep a wave for the grid/time step). Re-run with more "
            "stable parameters and the subsurface kinematics will be available."
        )

    # Clear stale figures so a partial re-run can't leave orphans behind.
    for old in run_dir.glob("kinematics_*.png"):
        old.unlink()
    (run_dir / "kinematics_index.json").unlink(missing_ok=True)

    work = Path(tempfile.mkdtemp(prefix="ow3d_kinviz_"))
    try:
        ip_expr = "round(nx/2)+1" if x_index is None else str(int(x_index))
        readkin = work / "ReadKinematics_plots.m"
        readkin.write_text(_patched_readkinematics(ip_expr))
        driver = work / "driver.m"
        driver.write_text(_driver(readkin, run_dir, run_dir, idn))

        proc = subprocess.run(
            [octave, "--norc", str(driver)],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
            env={"DISPLAY": ""},  # force headless
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

    index = run_dir / "kinematics_index.json"
    figures: List[dict] = []
    if index.is_file():
        try:
            for item in json.loads(index.read_text()):
                p = run_dir / item["file"]
                if p.is_file():
                    figures.append({"file": item["file"], "path": str(p),
                                    "title": item.get("title", "")})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    if not figures:
        tail = (proc.stdout + proc.stderr)[-600:]
        raise RuntimeError(
            "OceanWave3D's ReadKinematics.m produced no figures.\n"
            f"Octave output:\n{tail}"
        )
    return figures


def list_kinematics_plots(run_dir) -> List[dict]:
    """Return previously generated kinematics figures for a run (may be empty)."""
    run_dir = Path(run_dir)
    index = run_dir / "kinematics_index.json"
    if not index.is_file():
        return []
    try:
        items = json.loads(index.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for item in items:
        p = run_dir / item.get("file", "")
        if p.is_file():
            out.append({"file": item["file"], "path": str(p),
                        "title": item.get("title", "")})
    return out

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
    "but it was not found on PATH, and the Docker sandbox image (which bundles "
    "Octave) is not built either. Pick one:\n"
    "  • Build the Docker sandbox — it includes Octave, so kinematics can be "
    "rendered inside the container with nothing extra on this machine:\n"
    "        install_oceanwave3d(backend=\"docker\")\n"
    "  • Or install GNU Octave on this machine:\n"
    "        Fedora/RHEL  : sudo dnf install -y octave\n"
    "        Debian/Ubuntu: sudo apt-get install -y octave\n"
    "        macOS (brew) : brew install octave"
)


def octave_binary() -> Optional[str]:
    """Return the path to a host Octave executable, or None if not installed."""
    for name in ("octave-cli", "octave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def octave_available() -> bool:
    """True if a HOST Octave is installed (ignores the Docker sandbox)."""
    return octave_binary() is not None


def _docker_octave_ready() -> bool:
    """True if the Docker sandbox image (which bundles Octave) is available.

    Goes through runner.select_backend('docker'), which CACHES a positive result
    for the process lifetime — so the viewer hitting /api/kinematics repeatedly
    on a Docker-only machine doesn't spawn a fresh ~20 s `docker image inspect`
    every time (a built image never becomes un-built within a session).
    """
    from . import runner  # lazy: avoid an import cycle at module load
    return runner.select_backend("docker") is not None


def kinematics_available() -> bool:
    """True if kinematics figures can be rendered by EITHER backend.

    Host Octave (fast, no container) or the Docker sandbox image (which bundles
    Octave). Used by the viewer to decide whether to offer the "generate" action.
    """
    return octave_available() or _docker_octave_ready()


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
    # Clamp the time-slice window so SHORT runs still render every figure. The
    # script's `itp=[it-20:it]` assumes >=21 stored kinematics slices; on a short
    # run (small num_periods — a very common first try) it<21 makes it-20<=0, so
    # itp holds non-positive indices and the FIRST subsurface-profile plot throws,
    # aborting the four velocity/potential/shear profile figures (the headline
    # kinematics deliverable) while leaving only the two surface plots. Lower-bound
    # the window at 1 so the profiles always render.
    clamped = new.replace("itp=[it-20:it];", "itp=[max(it-20,1):it];")
    if clamped == new:  # guard against upstream rewording the line
        raise RuntimeError(
            "Could not clamp the time-slice window in the bundled "
            "ReadKinematics.m (the expected `itp=[it-20:it];` line was not found)."
        )
    return clamped


def _driver(readkin_path: str, run_dir: str, out_dir: str, idn: int,
            utils_matlab: str) -> str:
    """Octave driver: set inputs, run the bundled script, save its figures.

    All paths are passed as strings already expressed in the namespace where
    Octave runs (host absolute paths for the host backend; container paths such
    as /work and /opt/oceanwave3d/utils/matlab for the Docker backend).

    The figure manifest is emitted to STDOUT as `__OW3D_FIG__\\t<file>\\t<title>`
    lines (parsed by the Python caller, which then writes kinematics_index.json).
    Deliberately NOT via jsonencode: that builtin only exists in Octave >= 7, but
    the Ubuntu 22.04 sandbox ships Octave 6.4 — printf works on every version and
    on the host too.
    """
    # gnuplot toolkit renders to file without an X display.
    return f"""
graphics_toolkit('gnuplot');
addpath('{utils_matlab}/IO');
addpath('{utils_matlab}/Analysis');
addpath('{utils_matlab}/visualization');
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
  ttl = regexprep(ttl, '[\\t\\r\\n]', ' ');   % keep the manifest one tab-delimited line
  fn = sprintf('kinematics_%02d.png', k);
  print(f, fullfile('{out_dir}', fn), '-dpng', '-S900,560');
  printf('__OW3D_FIG__\\t%s\\t%s\\n', fn, ttl);
end
printf('SAVED %d figures\\n', numel(figs));
if ~ok, exit(3); end
"""


def _run_host(run_dir: Path, staging: Path, idn: int, ip_expr: str):
    """Run the kinematics driver via the host's Octave. Returns (stdout, stderr, fail).

    The driver cd's into run_dir (where Kinematics01.bin lives) but writes its
    scripts and figures into `staging` — a throwaway dir, so a failed render
    never clobbers a run's previously-good figures (they are promoted from
    staging only on success).
    """
    octave = octave_binary()
    readkin = staging / "ReadKinematics_plots.m"
    readkin.write_text(_patched_readkinematics(ip_expr))
    driver = staging / "driver.m"
    # Host backend: every path is a host absolute path (native separators).
    driver.write_text(_driver(str(readkin), str(run_dir), str(staging),
                               idn, str(_UTILS_MATLAB)))
    try:
        proc = subprocess.run(
            [octave, "--norc", str(driver)],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
            env={"DISPLAY": ""},  # force headless
        )
    except subprocess.TimeoutExpired:
        return "", "", (f"Octave timed out after {_TIMEOUT_S}s while rendering "
                        "the kinematics figures.")
    return proc.stdout, proc.stderr, None


def _run_docker(run_dir: Path, staging: Path, idn: int, ip_expr: str):
    """Run the kinematics driver via Octave inside the sandbox container.

    Returns (stdout, stderr, fail). `staging` is a subdir of run_dir (so it is
    inside the bind mount); the container sees it under /work/<name>. Scripts and
    figures are written there and promoted into run_dir only on success.
    """
    from . import docker_runner  # lazy: avoid an import cycle at module load

    name = staging.name  # e.g. ".kinviz" — relative to /work inside the container
    (staging / "ReadKinematics_plots.m").write_text(_patched_readkinematics(ip_expr))
    # Container backend: scripts + figures live under /work/<name>, the bundled
    # utils under the in-image path, and the run dir (cd target) maps to /work.
    (staging / "driver.m").write_text(_driver(
        f"/work/{name}/ReadKinematics_plots.m", "/work", f"/work/{name}", idn,
        docker_runner.UTILS_MATLAB_CONTAINER))
    ex = docker_runner.run_octave(run_dir, f"{name}/driver.m", _TIMEOUT_S)
    if ex.launch_error:
        return "", "", ex.launch_error
    if ex.timed_out:
        return "", "", (f"Octave timed out after {_TIMEOUT_S}s in the sandbox "
                        "while rendering the kinematics figures.")
    return ex.stdout, ex.stderr, None


def _collect_figures(run_dir: Path, staging: Path, stdout: str) -> List[dict]:
    """Promote the rendered figures from `staging` into run_dir, atomically-ish.

    Parses the driver's `__OW3D_FIG__` stdout manifest, copies each produced PNG
    out of the staging dir into run_dir, removes any orphan kinematics_*.png left
    by an earlier larger render, and (re)writes kinematics_index.json. run_dir is
    mutated ONLY when at least one figure was produced, so a failed render leaves
    a prior good set untouched. Returns the list of promoted figures."""
    figures: List[dict] = []
    index_items: List[dict] = []
    for line in stdout.splitlines():
        if not line.startswith("__OW3D_FIG__\t"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        fname, title = parts[1], parts[2]
        src = staging / fname
        if src.is_file():
            dest = run_dir / fname
            shutil.copy2(str(src), str(dest))  # overwrites; works across drives
            index_items.append({"file": fname, "title": title})
            figures.append({"file": fname, "path": str(dest), "title": title})
    if index_items:
        produced = {it["file"] for it in index_items}
        # Drop orphans from a prior, larger render so the gallery matches the index.
        for old in run_dir.glob("kinematics_*.png"):
            if old.name not in produced:
                old.unlink()
        # Same {file,title} shape list_kinematics_plots() / the viewer expect.
        (run_dir / "kinematics_index.json").write_text(json.dumps(index_items))
    return figures


def generate_kinematics_plots(run_dir, idn: int = 1,
                              x_index: Optional[int] = None) -> List[dict]:
    """Run the bundled kinematics visualization and save its figures.

    Returns a list of {"file": <name>, "path": <abs path>, "title": <caption>}
    for the PNGs OceanWave3D's ReadKinematics.m produced, written into run_dir.

    Renders via the host's GNU Octave when present (fastest), otherwise inside
    the Docker sandbox image (which bundles Octave) — so a Docker-backend user
    who never installed a host toolchain still gets kinematics.

    x_index : optional 1-based horizontal grid column for the vertical-profile
        plots. Defaults to mid-domain (the script author's own commented-out
        choice). Clamped into range by the script.

    Raises RuntimeError (with an actionable message) if neither Octave backend is
    available, the kinematics file is absent/empty, or no figures were produced.
    """
    # Absolute path is essential: the driver cd's into the run dir, after which
    # any relative output path would resolve against the wrong directory.
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise RuntimeError(f"Run directory not found: {run_dir}")

    # Pick the rendering backend: host Octave first (no container overhead),
    # then the sandbox image. Neither → actionable install/build hint.
    if octave_available():
        backend = "host"
    elif _docker_octave_ready():
        backend = "docker"
    else:
        raise RuntimeError(INSTALL_HINT)

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

    # Render into a throwaway staging dir, then promote the figures into run_dir
    # only if the render actually produced some — so a failed (re)generation never
    # destroys a run's previously-good figures. The docker backend needs its
    # staging inside run_dir (it must be reachable through the /work bind mount);
    # the host backend can stage anywhere, so use a system temp dir.
    ip_expr = "round(nx/2)+1" if x_index is None else str(int(x_index))
    if backend == "docker":
        staging = run_dir / ".kinviz"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
    else:
        staging = Path(tempfile.mkdtemp(prefix="ow3d_kinviz_"))
    try:
        if backend == "host":
            stdout, stderr, fail = _run_host(run_dir, staging, idn, ip_expr)
        else:
            stdout, stderr, fail = _run_docker(run_dir, staging, idn, ip_expr)
        if fail:
            raise RuntimeError(fail)

        figures = _collect_figures(run_dir, staging, stdout)
        if not figures:
            tail = (stdout + stderr)[-600:]
            where = "the sandbox container" if backend == "docker" else "the host Octave"
            raise RuntimeError(
                "OceanWave3D's ReadKinematics.m produced no figures "
                f"(via {where}).\nOctave output:\n{tail}"
            )
        return figures
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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

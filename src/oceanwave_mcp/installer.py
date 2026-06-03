"""
Builds OceanWave3D and its third-party libraries from the user-provided
(licensed) source tarballs, so non-experts don't have to wrestle with
makefiles and Fortran compiler flags.

The build chain (all compiled with legacy flags for modern gfortran):

  1. LAPACK + BLAS  (lapack-3.3.1.tgz)  -> lib/liblapack.a, lib/libblas.a
  2. SPARSKIT2      (SPARSKIT2.tar.gz)  -> lib/libskit.a
  3. Harwell        (Harwell.tar.gz)    -> lib/libharwell.a
  4. OceanWave3D    (the submodule)     -> bin/OceanWave3D

Because the full build takes several minutes, install runs in a detached
background process that streams to a log file; callers poll installation_status().

This module imports nothing from `mcp` so it can also run as a CLI:
    python -m oceanwave_mcp.installer --build
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Optional

from .runner import BINARY_PATH, _REPO_ROOT  # reuse repo-root + binary location

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

LIB_DIR = _REPO_ROOT / "lib"
BIN_DIR = _REPO_ROOT / "bin"
BUILD_DEPS_DIR = _REPO_ROOT / "build_deps"
SUBMODULE_DIR = _REPO_ROOT / "OceanWave3D-Fortran90"
INSTALL_STATE_DIR = _REPO_ROOT / "simulations" / ".install"
STATUS_FILE = INSTALL_STATE_DIR / "status.json"
LOG_FILE = INSTALL_STATE_DIR / "install.log"

DEFAULT_FILES_DIR = Path.home() / "Documents" / "OceanWave3D_Files"
FILES_DIR_ENV = "OCEANWAVE3D_FILES"

REQUIRED_TARBALLS = {
    "harwell": "Harwell.tar.gz",
    "sparskit": "SPARSKIT2.tar.gz",
    "lapack": "lapack-3.3.1.tgz",
}

# Where each third-party source archive can be obtained. Only Harwell/HSL is
# license-restricted; LAPACK and SPARSKIT2 are freely available. For DTU course
# work the whole bundle is usually provided by the OceanWave3D maintainers.
FILE_SOURCES = {
    "harwell": {
        "name": "Harwell Subroutine Library (HSL)",
        "url": "https://www.hsl.rl.ac.uk/",
        "note": "Free for academic use (registration required); paid licence for commercial use.",
    },
    "sparskit": {
        "name": "SPARSKIT2 (Y. Saad)",
        "url": "https://www-users.cse.umn.edu/~saad/software/SPARSKIT/",
        "note": "Freely available for research use.",
    },
    "lapack": {
        "name": "LAPACK 3.3.1",
        "url": "https://www.netlib.org/lapack/",
        "note": "Open source (BSD); older releases are archived on Netlib.",
    },
}
MAINTAINER_CONTACT = "apek@dtu.dk"  # OceanWave3D lead developer (DTU)

# Legacy Fortran (1990s-2011 code) needs these to compile on gfortran 10+.
LEGACY_FFLAGS = "-std=legacy -fallow-argument-mismatch -ffree-line-length-none"
# OceanWave3D's own optimisation flags (adds -fno-automatic, required by the solver).
OW3D_OPTFLAGS = f"-O2 {LEGACY_FFLAGS} -fno-automatic"

REQUIRED_TOOLS = ("gfortran", "make", "ar", "ranlib")


# ---------------------------------------------------------------------------
# Locating the paid source files
# ---------------------------------------------------------------------------

def paid_files_dir() -> Path:
    """Directory holding the licensed source tarballs."""
    env = os.environ.get(FILES_DIR_ENV)
    return Path(env).expanduser() if env else DEFAULT_FILES_DIR


def _files_dir_readme() -> str:
    """Drop-in instructions written into the (empty) source-files folder."""
    lines = [
        "Place the three OceanWave3D third-party source archives in THIS folder,",
        "then ask Claude to install OceanWave3D (or call install_oceanwave3d()).",
        "",
        "Required files (exact names):",
    ]
    for label, fname in REQUIRED_TARBALLS.items():
        src = FILE_SOURCES[label]
        lines.append(f"  - {fname}")
        lines.append(f"      {src['name']} — {src['url']}")
        lines.append(f"      {src['note']}")
    lines += [
        "",
        f"DTU course work: the bundle is usually provided by the OceanWave3D",
        f"maintainers — contact {MAINTAINER_CONTACT}.",
        "",
        "To use a different folder, set the OCEANWAVE3D_FILES environment variable.",
    ]
    return "\n".join(lines) + "\n"


def ensure_files_dir() -> Path:
    """
    Make sure the source-files folder exists so the user has an obvious place to
    drop the archives. Creates it (and a README explaining what goes inside) when
    missing, then returns the path.
    """
    d = paid_files_dir()
    d.mkdir(parents=True, exist_ok=True)
    readme = d / "README_PUT_FILES_HERE.txt"
    if not readme.exists():
        try:
            readme.write_text(_files_dir_readme())
        except OSError:
            pass
    return d


def _system_install_hint() -> str:
    """OS-specific command to install the missing Fortran toolchain."""
    if sys.platform == "win32":
        return ("Install MSYS2 (https://www.msys2.org/), open the MinGW64 shell and run:\n"
                "    pacman -S mingw-w64-x86_64-gcc-fortran make")
    if sys.platform == "darwin":
        return "brew install gcc make    # provides gfortran"
    # Linux: detect package manager
    if shutil.which("dnf"):
        return "sudo dnf install gcc-gfortran make"
    if shutil.which("apt"):
        return "sudo apt install gfortran make"
    return "Install gfortran and make via your system package manager."


# ---------------------------------------------------------------------------
# Prerequisite checking
# ---------------------------------------------------------------------------

def check_prerequisites() -> dict:
    """
    Inspect everything needed to build OceanWave3D and return a structured report.
    """
    tools = {name: shutil.which(name) is not None for name in REQUIRED_TOOLS}

    # Create the drop-in folder up front so there's always a clear place for the
    # user to put the archives, even on the very first check.
    files_dir = ensure_files_dir()
    tarballs = {
        label: (files_dir / fname).exists()
        for label, fname in REQUIRED_TARBALLS.items()
    }

    submodule_ready = (SUBMODULE_DIR / "makefile").exists()
    binary_installed = BINARY_PATH.exists()

    missing_tools = [t for t, ok in tools.items() if not ok]
    missing_file_labels = [l for l, ok in tarballs.items() if not ok]
    missing_files = [REQUIRED_TARBALLS[l] for l in missing_file_labels]

    can_build = (
        not missing_tools
        and not missing_files
        and submodule_ready
    )

    return {
        "tools": tools,
        "missing_tools": missing_tools,
        "tool_install_hint": _system_install_hint() if missing_tools else "",
        "files_dir": str(files_dir),
        "tarballs": tarballs,
        "missing_files": missing_files,
        "missing_file_labels": missing_file_labels,
        "file_sources": FILE_SOURCES,
        "maintainer_contact": MAINTAINER_CONTACT,
        "submodule_ready": submodule_ready,
        "binary_installed": binary_installed,
        "binary_path": str(BINARY_PATH),
        "can_build": can_build,
    }


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

def _log(log, msg: str) -> None:
    log.write(msg + "\n")
    log.flush()


def _run(cmd, cwd: Path, log, env: Optional[dict] = None) -> None:
    """Run a build command, streaming combined output to the log. Raise on failure."""
    _log(log, f"\n$ (cd {cwd}) {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)} (cwd={cwd})")


def _extract(tarball: Path, dest: Path, log) -> Path:
    """Extract a tarball into dest, returning the top-level extracted directory."""
    _log(log, f"Extracting {tarball.name} -> {dest}")
    with tarfile.open(tarball) as tf:
        top = tf.getnames()[0].split("/")[0]
        tf.extractall(dest)
    return dest / top


def build_lapack(files_dir: Path, log) -> None:
    """Build BLAS + LAPACK static libs -> lib/libblas.a, lib/liblapack.a."""
    src = _extract(files_dir / REQUIRED_TARBALLS["lapack"], BUILD_DEPS_DIR, log)

    # make.inc drives the build; start from the shipped example and add legacy flags.
    make_inc = src / "make.inc"
    text = (src / "make.inc.example").read_text()
    text = text.replace("OPTS     =", f"OPTS     = -O2 {LEGACY_FFLAGS}")
    text = text.replace("NOOPT    = -g -O0", f"NOOPT    = -O0 {LEGACY_FFLAGS}")
    make_inc.write_text(text)

    _run(["make", "blaslib"], cwd=src, log=log)
    _run(["make", "lapacklib"], cwd=src, log=log)

    # PLAT=_LINUX in the example -> blas_LINUX.a / lapack_LINUX.a at the root.
    blas = _find_archive(src, ["blas_LINUX.a", "librefblas.a", "libblas.a"])
    lapack = _find_archive(src, ["lapack_LINUX.a", "liblapack.a"])
    _install_lib(blas, LIB_DIR / "libblas.a", log)
    _install_lib(lapack, LIB_DIR / "liblapack.a", log)


def build_sparskit(files_dir: Path, log) -> None:
    """Build SPARSKIT2 -> lib/libskit.a."""
    src = _extract(files_dir / REQUIRED_TARBALLS["sparskit"], BUILD_DEPS_DIR, log)
    # Top makefile defaults to F77=f77 / OPT=-c -O; override for gfortran + legacy.
    _run(
        ["make", "F77=gfortran", f"OPT=-c -O2 {LEGACY_FFLAGS}"],
        cwd=src, log=log,
    )
    skit = _find_archive(src, ["libskit.a"])
    _install_lib(skit, LIB_DIR / "libskit.a", log)


def build_harwell(files_dir: Path, log) -> None:
    """Build Harwell -> lib/libharwell.a."""
    src = _extract(files_dir / REQUIRED_TARBALLS["harwell"], BUILD_DEPS_DIR, log)
    # Ship .o files may be from an old compiler; remove so make recompiles from .f.
    for obj in src.glob("*.o"):
        obj.unlink()
    # makefile installs to $(installd); point it straight at our lib dir.
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    _run(
        ["make", "FF=gfortran", f"FFLAGS=-O3 {LEGACY_FFLAGS} -fno-automatic",
         f"installd={LIB_DIR}"],
        cwd=src, log=log,
    )
    if not (LIB_DIR / "libharwell.a").exists():
        # Fallback: archive whatever objects were produced.
        objs = [str(o.name) for o in src.glob("*.o")]
        if objs:
            _run(["ar", "-rc", str(LIB_DIR / "libharwell.a"), *objs], cwd=src, log=log)
    if not (LIB_DIR / "libharwell.a").exists():
        raise RuntimeError("Harwell build did not produce libharwell.a")


def build_oceanwave3d(log) -> None:
    """Link OceanWave3D against the freshly built libs -> bin/OceanWave3D."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    binary_name = "OceanWave3D.exe" if sys.platform == "win32" else "OceanWave3D"
    # The makefile sets BUILDDIR = $(PWD)/build. $(PWD) reads the PWD env var,
    # which subprocess does NOT update when changing cwd, so it would resolve to
    # the repo root. Override BUILDDIR explicitly (command-line assignments beat
    # the in-file value) and create it first since the Release target doesn't.
    build_dir = SUBMODULE_DIR / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    _run(
        ["make", "Release",
         "FC=gfortran",
         f"BUILDDIR={build_dir}",
         f"INSTALLDIR={BIN_DIR}",
         f"PROGNAME={binary_name}",
         f"LIBDIRS=-L{LIB_DIR}",
         "LINLIB=-lharwell -lskit -llapack -lblas",
         f"OPTFLAGS={OW3D_OPTFLAGS}"],
        cwd=SUBMODULE_DIR, log=log,
    )
    if not BINARY_PATH.exists():
        raise RuntimeError(f"Build finished but binary not found at {BINARY_PATH}")


def _find_archive(root: Path, names: list[str]) -> Path:
    """Find the first existing archive among candidate names (recursive fallback)."""
    for name in names:
        direct = root / name
        if direct.exists():
            return direct
    for name in names:
        for hit in root.rglob(name):
            return hit
    raise RuntimeError(f"None of {names} were produced under {root}")


def _install_lib(src: Path, dest: Path, log) -> None:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _log(log, f"Installed {dest.name} <- {src}")


def build_all(log) -> None:
    """Run the full build in dependency order. Raises on the first failure.

    Library builds are idempotent: an already-present lib/*.a is left in place so
    retries (e.g. after fixing a later step) don't recompile LAPACK from scratch.
    """
    files_dir = paid_files_dir()
    BUILD_DEPS_DIR.mkdir(parents=True, exist_ok=True)
    LIB_DIR.mkdir(parents=True, exist_ok=True)

    _log(log, "=== [1/4] Building LAPACK + BLAS ===")
    if (LIB_DIR / "liblapack.a").exists() and (LIB_DIR / "libblas.a").exists():
        _log(log, "liblapack.a + libblas.a already present — skipping.")
    else:
        build_lapack(files_dir, log)

    _log(log, "=== [2/4] Building SPARSKIT2 ===")
    if (LIB_DIR / "libskit.a").exists():
        _log(log, "libskit.a already present — skipping.")
    else:
        build_sparskit(files_dir, log)

    _log(log, "=== [3/4] Building Harwell ===")
    if (LIB_DIR / "libharwell.a").exists():
        _log(log, "libharwell.a already present — skipping.")
    else:
        build_harwell(files_dir, log)

    _log(log, "=== [4/4] Linking OceanWave3D ===")
    build_oceanwave3d(log)
    _log(log, "\n*** OceanWave3D installed successfully. ***")


# ---------------------------------------------------------------------------
# Status file helpers
# ---------------------------------------------------------------------------

def _write_status(**fields) -> None:
    INSTALL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(fields, indent=2))


def _read_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text())
    except (ValueError, OSError):
        return {}


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _log_tail(n: int = 40) -> str:
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# Background install orchestration
# ---------------------------------------------------------------------------

def start_background_install() -> dict:
    """
    Validate prerequisites and launch the build in a detached background process.

    Returns {"started": bool, ...}. When started is False, "reason" explains why.
    """
    prereq = check_prerequisites()

    if prereq["binary_installed"]:
        return {"started": False, "reason": "already_installed", "prereq": prereq}

    status = _read_status()
    if status.get("state") == "running" and _pid_alive(status.get("pid", 0)):
        return {"started": False, "reason": "already_running", "prereq": prereq}

    if not prereq["can_build"]:
        return {"started": False, "reason": "missing_prerequisites", "prereq": prereq}

    INSTALL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "w")  # fresh log per run
    proc = subprocess.Popen(
        [sys.executable, "-m", "oceanwave_mcp.installer", "--build"],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(_REPO_ROOT),
    )
    _write_status(state="running", pid=proc.pid, started=time.time())
    return {"started": True, "pid": proc.pid, "prereq": prereq}


def installation_status() -> dict:
    """Report on the current/last install: state, elapsed time, and a log tail."""
    status = _read_status()
    state = status.get("state", "none")

    # Reconcile a stale "running" record: the detached process may have exited.
    if state == "running":
        if BINARY_PATH.exists():
            state = "succeeded"
        elif not _pid_alive(status.get("pid", 0)):
            state = "failed"

    started = status.get("started")
    elapsed = (time.time() - started) if started else None
    return {
        "state": state,
        "elapsed_seconds": elapsed,
        "binary_installed": BINARY_PATH.exists(),
        "error": status.get("error", ""),
        "log_tail": _log_tail(),
    }


# ---------------------------------------------------------------------------
# CLI entry point (invoked as the detached build process)
# ---------------------------------------------------------------------------

def _build_main() -> int:
    with open(LOG_FILE, "a") as log:
        try:
            build_all(log)
            _write_status(state="succeeded", finished=time.time(),
                          started=_read_status().get("started"))
            return 0
        except Exception as exc:  # noqa: BLE001
            _log(log, f"\n!!! BUILD FAILED: {exc}")
            _write_status(state="failed", finished=time.time(), error=str(exc),
                          started=_read_status().get("started"))
            return 1


if __name__ == "__main__":
    if "--build" in sys.argv:
        sys.exit(_build_main())
    # Default: print a prerequisite report.
    print(json.dumps(check_prerequisites(), indent=2))

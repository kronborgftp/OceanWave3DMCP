"""
Docker sandbox backend for OceanWave3D.

An alternative to the native build (installer.py): instead of compiling the
solver onto the host with a local Fortran toolchain, build a container image
from the same three user-supplied tarballs and run every simulation inside it.
This lets a user who only has Docker — no gfortran/MSYS2 — still run OceanWave3D,
and isolates each solve in a throwaway container.

Two responsibilities:
  • build_image_blocking() — stage a clean build context (Dockerfile + the three
    tarballs + the Fortran source submodule) and `docker build` the image.
  • run() — `docker run` the solver on one prepared run directory, bind-mounted
    at /work, returning the same (returncode, stdout, stderr) shape the native
    runner produces so the caller's success/parse logic is backend-agnostic.

Detection helpers (docker_available / image_built) let the runner pick a backend
and let the MCP report readiness. Importing this module never shells out — all
docker calls happen inside the functions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import installer  # paths, REQUIRED_TARBALLS, paid_files_dir, SUBMODULE_DIR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_TAG = os.environ.get("OCEANWAVE3D_IMAGE", "oceanwave3d-mcp:latest")
DOCKERFILE = installer._REPO_ROOT / "docker" / "Dockerfile"
# Staged under build_deps/ so it inherits the existing .gitignore entry.
BUILD_CONTEXT_DIR = installer.BUILD_DEPS_DIR / ".docker_context"

# Where the Dockerfile copies OceanWave3D's bundled MATLAB/Octave scripts inside
# the image (see docker/Dockerfile). octave_viz points the in-container Octave
# driver here when it runs the kinematics post-processing in the sandbox.
UTILS_MATLAB_CONTAINER = "/opt/oceanwave3d/utils/matlab"

# Short ceiling for the quick liveness/inspect probes so a stuck or unreachable
# Docker daemon can't hang an MCP tool call. The actual build/run get their own
# (much longer / caller-supplied) limits.
_PROBE_TIMEOUT = 20

_CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class ExecResult:
    """Backend-agnostic result of executing the solver once."""
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    launch_error: str = ""  # set when the container could not be launched at all


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def docker_path() -> str | None:
    """Absolute path to the docker CLI, or None if it isn't installed."""
    return shutil.which("docker")


def docker_available() -> bool:
    """True if the docker CLI is present AND the daemon answers."""
    docker = docker_path()
    if not docker:
        return False
    try:
        # `version` (unlike `--version`) contacts the daemon for the server
        # version, so a non-zero exit means the daemon is down/unreachable.
        proc = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def image_built() -> bool:
    """True if the OceanWave3D sandbox image already exists locally."""
    docker = docker_path()
    if not docker:
        return False
    try:
        proc = subprocess.run(
            [docker, "image", "inspect", IMAGE_TAG],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status() -> dict:
    """Structured readiness report for the MCP's check_installation()."""
    available = docker_available()
    return {
        "docker_path": docker_path(),
        "docker_available": available,
        "image_tag": IMAGE_TAG,
        "image_built": image_built() if available else False,
        "dockerfile": str(DOCKERFILE),
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _stage_context(files_dir: Path, log) -> Path:
    """
    Assemble a minimal, clean build context: the Dockerfile, the three source
    tarballs, and a copy of the Fortran source (sans build/ and VCS junk). A
    dedicated context keeps `docker build` fast and reproducible regardless of
    what else is lying around the repo.
    """
    ctx = BUILD_CONTEXT_DIR
    if ctx.exists():
        shutil.rmtree(ctx)
    ctx.mkdir(parents=True)

    installer._log(log, f"Staging Docker build context at {ctx}")
    shutil.copy2(DOCKERFILE, ctx / "Dockerfile")

    missing = [f for f in installer.REQUIRED_TARBALLS.values()
               if not (files_dir / f).exists()]
    if missing:
        raise RuntimeError(
            f"Missing source tarball(s) in {files_dir}: {', '.join(missing)}"
        )
    for fname in installer.REQUIRED_TARBALLS.values():
        shutil.copy2(files_dir / fname, ctx / fname)
        installer._log(log, f"  + {fname}")

    if not (installer.SUBMODULE_DIR / "makefile").exists():
        raise RuntimeError(
            f"OceanWave3D source not found at {installer.SUBMODULE_DIR}. "
            "Run: git submodule update --init"
        )
    shutil.copytree(
        installer.SUBMODULE_DIR, ctx / "OceanWave3D-Fortran90",
        ignore=shutil.ignore_patterns("build", ".git", ".bzr", "*.o", "*.a"),
    )
    installer._log(log, "  + OceanWave3D-Fortran90/ (source)")
    return ctx


def build_image_blocking(files_dir: Path, log) -> None:
    """Build the sandbox image, streaming docker output to `log`. Raises on failure."""
    docker = docker_path()
    if not docker:
        raise RuntimeError("Docker CLI not found on PATH. Install Docker Desktop / Docker Engine.")
    if not docker_available():
        raise RuntimeError(
            "Docker is installed but the daemon is not responding. "
            "Start Docker Desktop (or the docker service) and try again."
        )

    ctx = _stage_context(files_dir, log)
    cmd = [docker, "build", "-t", IMAGE_TAG, "-f", str(ctx / "Dockerfile"), str(ctx)]
    installer._log(log, f"\n$ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd, stdout=log, stderr=subprocess.STDOUT, text=True,
        creationflags=_CREATE_NO_WINDOW,
    )
    # Best-effort cleanup of the (large) staged context once the image is built.
    try:
        shutil.rmtree(ctx)
    except OSError:
        pass
    if proc.returncode != 0:
        raise RuntimeError(f"docker build failed (exit {proc.returncode}); see the log above.")
    if not image_built():
        raise RuntimeError("docker build reported success but the image is not present.")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _container_name(run_dir: Path) -> str:
    """A unique, docker-legal container name derived from the run directory."""
    safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in run_dir.name)
    return f"ow3d_{safe}"[:120]


def _mount_source(run_dir: Path) -> str:
    """Bind-mount `source=` string in the form Docker accepts on this OS.

    On Windows the run dir is a backslash path (D:\\repo\\sim\\<id>). The
    `--mount` CSV parser mishandles backslashes, so Docker Desktop wants a
    forward-slash drive path (D:/repo/sim/<id>); Path.as_posix() produces exactly
    that. Elsewhere the native string is already correct.
    """
    if sys.platform == "win32":
        return run_dir.as_posix()
    return str(run_dir)


def _isolation_args(run_dir: Path) -> list:
    """Common `docker run` args to bind-mount run_dir at /work, portably.

    Shared by the solver run and the in-container kinematics (Octave) run so the
    ownership/SELinux handling stays identical for both.
    """
    args: list = []
    # On POSIX run as the host user so bind-mounted outputs aren't root-owned (the
    # host parser/viewer must read and delete them). Docker Desktop (win/mac) maps
    # ownership on the host side, so skip there.
    if sys.platform not in ("win32", "darwin") and hasattr(os, "getuid"):
        args += ["--user", f"{os.getuid()}:{os.getgid()}"]
        # On SELinux-enforcing hosts (Fedora/RHEL/CentOS) an unlabeled bind mount
        # is unreadable/unwritable by the confined container, so the solver would
        # fail with a cryptic permission error. Disabling label confinement for
        # this throwaway run avoids relabeling host files and is a harmless no-op
        # on non-SELinux Linux. (`--mount` has no :z/:Z option, unlike `-v`.)
        args += ["--security-opt", "label=disable"]
    args += ["--mount", f"type=bind,source={_mount_source(run_dir)},target=/work"]
    return args


# Substrings of common Docker bind-mount / file-sharing failures. When `docker
# run` exits non-zero with one of these, the solver never started — surface a
# targeted hint instead of a misleading "solver reported an error".
_MOUNT_FAILURE_HINTS = (
    "is not shared", "not shared from", "drive has not been shared",
    "filesharing", "file sharing", "mounts denied", "denied the request",
    "bind source path does not exist", "error while creating mount source path",
    "are not shared from the host",
    # Forms Docker emits when a bind source is in a shape/location it can't mount
    # (legacy Hyper-V backend, path edge cases) — surface the same hint, not a
    # misleading "solver reported an error".
    "invalid mount config", "mount path must be absolute", "invalid mount path",
)


def _mount_failure_message(run_dir: Path, stderr: str) -> str:
    """A clear bind-mount-failure explanation (drive sharing), or '' if unrelated."""
    low = stderr.lower()
    if not any(h in low for h in _MOUNT_FAILURE_HINTS):
        return ""
    return (
        "The sandbox container could not access the run directory via the bind "
        f"mount ({run_dir}). On Windows/macOS, make sure the drive/folder is "
        "shared with Docker Desktop (Settings → Resources → File sharing), or "
        "use the WSL2 backend, which shares all drives automatically. "
        f"Docker error:\n{stderr.strip()}"
    )


def run(run_dir: Path, inp_filename: str, timeout: int) -> ExecResult:
    """
    Execute the solver inside the sandbox container on a prepared run directory.

    `run_dir` is bind-mounted at /work, so fort.* / LOG.txt / Kinematics*.bin are
    written straight into it (visible to the host parser). Returns an ExecResult
    mirroring a native subprocess run; never raises for ordinary failures.
    """
    docker = docker_path()
    if not docker:
        return ExecResult(launch_error="Docker CLI not found on PATH.")
    if not image_built():
        return ExecResult(launch_error=(
            f"Sandbox image '{IMAGE_TAG}' is not built. "
            "Run install_oceanwave3d() (Docker backend) first."
        ))

    name = _container_name(run_dir)
    cmd = [docker, "run", "--rm", "--name", name,
           *_isolation_args(run_dir), IMAGE_TAG, inp_filename]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
        )
        # A bind-mount/file-sharing failure means the solver never ran; report it
        # as a launch error (actionable) rather than a generic solver failure.
        if proc.returncode != 0:
            hint = _mount_failure_message(run_dir, proc.stderr)
            if hint:
                return ExecResult(launch_error=hint)
        return ExecResult(returncode=proc.returncode,
                          stdout=proc.stdout, stderr=proc.stderr)
    except subprocess.TimeoutExpired:
        # The `docker run` client was killed, but the container keeps running —
        # stop it explicitly so it doesn't linger and hold the bind mount.
        try:
            subprocess.run([docker, "kill", name], capture_output=True,
                           timeout=_PROBE_TIMEOUT, creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        return ExecResult(timed_out=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return ExecResult(launch_error=f"Failed to launch container: {exc}")


def run_octave(run_dir: Path, driver_rel: str, timeout: int) -> ExecResult:
    """
    Run `octave-cli --norc <driver>` inside the sandbox on a prepared run dir.

    Mirrors run(), but overrides the image entrypoint to Octave so OceanWave3D's
    bundled ReadKinematics.m post-processing can render its figures inside the
    container (no host Octave needed). `driver_rel` is the driver script's path
    RELATIVE to run_dir (e.g. ".kinviz/driver.m"); since run_dir is bind-mounted
    at /work, the container sees it under /work. The driver writes its PNGs to
    /work, i.e. straight back into run_dir on the host.
    """
    docker = docker_path()
    if not docker:
        return ExecResult(launch_error="Docker CLI not found on PATH.")
    if not image_built():
        return ExecResult(launch_error=(
            f"Sandbox image '{IMAGE_TAG}' is not built."
        ))

    name = _container_name(run_dir) + "_kin"
    container_driver = "/work/" + driver_rel.replace("\\", "/")
    cmd = [docker, "run", "--rm", "--name", name,
           *_isolation_args(run_dir),
           "--entrypoint", "octave-cli", IMAGE_TAG,
           "--norc", container_driver]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            hint = _mount_failure_message(run_dir, proc.stderr)
            if hint:
                return ExecResult(launch_error=hint)
        return ExecResult(returncode=proc.returncode,
                          stdout=proc.stdout, stderr=proc.stderr)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run([docker, "kill", name], capture_output=True,
                           timeout=_PROBE_TIMEOUT, creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
        return ExecResult(timed_out=True)
    except (OSError, subprocess.SubprocessError) as exc:
        return ExecResult(launch_error=f"Failed to launch container: {exc}")

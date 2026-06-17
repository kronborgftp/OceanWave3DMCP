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
    cmd = [docker, "run", "--rm", "--name", name]
    # On POSIX, run as the host user so the bind-mounted outputs aren't root-owned.
    # On Windows/macOS Docker Desktop handles ownership on the host side, so skip.
    if sys.platform not in ("win32", "darwin") and hasattr(os, "getuid"):
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    cmd += [
        "--mount", f"type=bind,source={run_dir},target=/work",
        IMAGE_TAG, inp_filename,
    ]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
        )
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

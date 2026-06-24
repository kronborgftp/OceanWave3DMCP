"""
Extract subsurface kinematics from OceanWave3D's binary kinematics file for the
viewer's animated overlays — without Octave.

Two products, both from the solver's own u/w data (no analytical guessing):

  * field   — an instantaneous velocity field (u, w) on an evenly-spaced grid,
              drawn as animated arrows over the cross-section.
  * orbits  — particle trajectories: each path is obtained by integrating the
              time-varying velocity field, so it shows the orbital motion a water
              particle traces as the wave passes (the classic kinematics-over-
              time figure). Computed at the solver's native time resolution.

Binary layout (gfortran unformatted sequential, 4-byte record markers):
  rec 1  : 9 ints (xbeg..tstride), dt (f8), nz (int)
  rec 2  : grid x,y,h,hx,hy interleaved (5*nx*ny f8)
  rec 3  : sigma (nz f8)         [0 = bed, 1 = surface; may include a ghost < 0]
  per timestep: eta, etax, etay (nx*ny f8 each),
                phi, u, v, w, wz, wx, wy, uz, ux, uy, vz, vx, vy (nx*ny*nz f8 each)
We use eta, u (2nd volume array) and w (4th); the rest is skipped.
"""
import json
from pathlib import Path
from typing import Optional

import numpy as np

from .octave_viz import _MIN_KIN_BYTES, kinematics_file

FIELD_JSON = "kinematics_field.json"
ORBITS_JSON = "kinematics_orbits.json"

# Field overlay: keep the JSON small and the arrows uncrowded.
_MAX_COLS = 16
_MAX_FRAMES = 80
_Z_LEVELS = 7                       # evenly-spaced layers (no surface clustering)


# --- low-level record helpers ------------------------------------------------

def _peek(buf, off):                # doubles in the record at off
    return int(np.frombuffer(buf, "<i4", 1, off)[0]) // 8


def _read(buf, off, count):         # read `count` f8 after the marker; advance
    arr = np.frombuffer(buf, "<f8", count, off + 4)
    return arr, off + 4 + count * 8 + 4


def _skip(buf, off):
    n = int(np.frombuffer(buf, "<i4", 1, off)[0])
    return off + 4 + n + 4


def _open(run_dir, idn):
    """Read the header + grid; return (buf, off, meta) or None if unusable."""
    kin = kinematics_file(Path(run_dir), idn)
    if kin is None or kin.stat().st_size < _MIN_KIN_BYTES:
        return None
    buf = kin.read_bytes()
    hdr = np.frombuffer(buf, "<i4", 9, 4)
    dt = float(np.frombuffer(buf, "<f8", 1, 40)[0])
    nz = int(np.frombuffer(buf, "<i4", 1, 48)[0])
    xbeg, xend, xstride, ybeg, yend, ystride, tbeg, tend, tstride = (int(v) for v in hdr)
    nx = (xend - xbeg) // xstride + 1
    ny = (yend - ybeg) // ystride + 1
    nt = (tend - tbeg) // tstride + 1
    if ny != 1 or nx < 2 or nz < 2:
        return None
    off = _skip(buf, 0)                              # header record
    grid, off = _read(buf, off, 5 * nx * ny)
    x = grid[0::5].astype(float)
    h = grid[2::5].astype(float)
    sigma_all, off = _read(buf, off, nz)
    zsel = np.where((sigma_all >= -1e-9) & (sigma_all <= 1 + 1e-9))[0]
    if zsel.size < 2:
        return None
    meta = {"nx": nx, "ny": ny, "nz": nz, "nt": nt, "dt_kin": dt * tstride,
            "x": x, "h": h, "sigma": sigma_all[zsel].astype(float), "zsel": zsel,
            "surf_n": nx * ny, "vol_n": nx * ny * nz}
    return buf, off, meta


def _step_timestep(buf, off, meta, want):
    """Advance past one timestep. If want, return (off, eta, u2d, w2d) else (off, None…)."""
    surf_n, vol_n, nx, nz, zsel = (meta["surf_n"], meta["vol_n"],
                                   meta["nx"], meta["nz"], meta["zsel"])
    eta = None
    if want:
        eta, off = _read(buf, off, surf_n)
    else:
        off = _skip(buf, off)
    off = _skip(buf, off)          # etax
    off = _skip(buf, off)          # etay
    u_arr = w_arr = None
    vi = 0
    while off + 8 <= len(buf) and _peek(buf, off) == vol_n:
        if want and vi == 1:
            u_arr, off = _read(buf, off, vol_n)
        elif want and vi == 3:
            w_arr, off = _read(buf, off, vol_n)
        else:
            off = _skip(buf, off)
        vi += 1
    if want and u_arr is not None and w_arr is not None:
        return off, eta, u_arr.reshape(nx, nz)[:, zsel], w_arr.reshape(nx, nz)[:, zsel]
    return off, None, None, None


# --- instantaneous velocity field (arrow overlay) ----------------------------

def extract_field(run_dir, idn: int = 1) -> Optional[dict]:
    opened = _open(run_dir, idn)
    if opened is None:
        return None
    buf, off, meta = opened
    nx, nt, sig = meta["nx"], meta["nt"], meta["sigma"]
    col = np.unique(np.linspace(0, nx - 1, min(_MAX_COLS, nx)).round().astype(int))
    keep = set(np.unique(
        np.linspace(0, nt - 1, min(_MAX_FRAMES, nt)).round().astype(int)).tolist())
    # Resample velocities onto evenly-spaced layers so arrows don't bunch up.
    z_lvl = np.linspace(0.08, 1.0, _Z_LEVELS)

    frames, times = [], []
    it = 0
    while off + 8 <= len(buf) and it < nt:
        if _peek(buf, off) != meta["surf_n"]:
            break
        want = it in keep
        off, eta, u2, w2 = _step_timestep(buf, off, meta, want)
        if want and u2 is not None:
            uc = np.array([np.interp(z_lvl, sig, u2[c]) for c in col])
            wc = np.array([np.interp(z_lvl, sig, w2[c]) for c in col])
            frames.append({
                "eta": np.round(eta[col], 5).tolist(),
                "u": np.round(uc, 5).tolist(),
                "w": np.round(wc, 5).tolist(),
            })
            times.append(round(it * meta["dt_kin"], 4))
        it += 1

    if not frames:
        return None
    return {
        "x": np.round(meta["x"][col], 4).tolist(),
        "h": np.round(meta["h"][col], 4).tolist(),
        "sigma": np.round(z_lvl, 5).tolist(),
        "times": times,
        "frames": frames,
    }


# --- particle orbits (kinematics-over-time overlay) --------------------------

def _bilinear(u2, x_grid, sig, pos_x, sig_pos):
    """Sample a [col, layer] field at (pos_x, sig_pos) — linear in x and sigma."""
    dx = x_grid[1] - x_grid[0]
    ci = (pos_x - x_grid[0]) / dx
    ci = min(max(ci, 0.0), len(x_grid) - 1.0)
    c0 = int(np.floor(ci)); c1 = min(c0 + 1, len(x_grid) - 1); fx = ci - c0
    v0 = np.interp(sig_pos, sig, u2[c0])
    v1 = np.interp(sig_pos, sig, u2[c1])
    return v0 * (1 - fx) + v1 * fx


def extract_orbits(run_dir, idn: int = 1,
                   seed_cols: int = 11, seed_levels: int = 4,
                   periods: float = 1.0) -> Optional[dict]:
    """Integrate particle paths through the time-varying velocity field.

    Each path is a water particle's trajectory over ~`periods` wave periods,
    started in the run's steady window. Calm-region paths (no real motion) are
    dropped so only meaningful orbits are returned.
    """
    opened = _open(run_dir, idn)
    if opened is None:
        return None
    buf, off, meta = opened
    nx, nt, dt_k, sig = meta["nx"], meta["nt"], meta["dt_kin"], meta["sigma"]
    x_grid, h = meta["x"], meta["h"]

    params_f = Path(run_dir) / "params.json"
    T = 0.0
    if params_f.exists():
        try:
            T = float(json.loads(params_f.read_text()).get("wave_period_s") or 0)
        except (ValueError, OSError):
            T = 0.0
    if T <= 0:
        T = nt * dt_k / 6.0
    win = max(8, int(round(periods * T / dt_k)))
    it0 = min(int(0.55 * nt), max(0, nt - 1 - win))

    # Skip to the window start, then read the window at full time resolution.
    it = 0
    while it < it0 and off + 8 <= len(buf) and _peek(buf, off) == meta["surf_n"]:
        off, *_ = _step_timestep(buf, off, meta, False)
        it += 1
    U, Wv, Eta = [], [], []
    for _ in range(win):
        if off + 8 > len(buf) or _peek(buf, off) != meta["surf_n"]:
            break
        off, eta, u2, w2 = _step_timestep(buf, off, meta, True)
        if u2 is None:
            break
        U.append(u2); Wv.append(w2); Eta.append(eta)
    nframes = len(U)
    if nframes < 6:
        return None

    x0, x1 = float(x_grid[0]), float(x_grid[-1])
    seed_x = np.linspace(0.12, 0.88, seed_cols) * (x1 - x0) + x0
    seed_s = np.linspace(0.25, 0.92, seed_levels)
    hmean = float(np.mean(h))

    orbits = []
    for sx in seed_x:
        # depth at the seed column (≈ flat-ish), surface from frame 0
        hc = float(np.interp(sx, x_grid, h))
        eta0 = float(np.interp(sx, x_grid, Eta[0]))
        for ss in seed_s:
            px, pz = float(sx), -hc + ss * (hc + eta0)
            path = [[round(px, 3), round(pz, 3)]]
            for fi in range(nframes):
                eta_here = float(np.interp(px, x_grid, Eta[fi]))
                denom = hc + eta_here
                sigp = (pz + hc) / denom if denom > 1e-6 else 0.0
                sigp = min(max(sigp, 0.0), 1.0)
                u = _bilinear(U[fi], x_grid, sig, px, sigp)
                w = _bilinear(Wv[fi], x_grid, sig, px, sigp)
                px = min(max(px + u * dt_k, x0), x1)
                pz = min(max(pz + w * dt_k, -hc), eta_here)
                path.append([round(px, 3), round(pz, 3)])
            arr = np.array(path)
            extent = float(np.hypot(np.ptp(arr[:, 0]), np.ptp(arr[:, 1])))
            if extent > 0.01:        # drop near-stationary (calm-region) seeds
                orbits.append({"path": path})

    if not orbits:
        return None
    return {"orbits": orbits, "period": round(T, 4), "dt": round(dt_k, 4),
            "frames": nframes}


# --- cached payloads ---------------------------------------------------------

def _cached(run_dir, fname, extractor):
    run_dir = Path(run_dir)
    cache = run_dir / fname
    if cache.is_file():
        try:
            return json.loads(cache.read_text())
        except (ValueError, OSError):
            pass
    data = extractor(run_dir)
    if data is None:
        return None
    try:
        cache.write_text(json.dumps(data))
    except OSError:
        pass
    return data


def field_payload(run_dir) -> Optional[dict]:
    return _cached(run_dir, FIELD_JSON, extract_field)


def orbits_payload(run_dir) -> Optional[dict]:
    return _cached(run_dir, ORBITS_JSON, extract_orbits)

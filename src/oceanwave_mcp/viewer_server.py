"""
Localhost viewer for OceanWave3D visualizations.

Chat clients (e.g. Claude Desktop) render PNG tool results inline but NOT
animated GIFs, so the MCP server hands out http://127.0.0.1:<port>/... links
instead. The link opens an interactive viewer (static HTML/JS, no external
dependencies) that renders the solver output client-side:

- annotated cross-section (water fill, seabed, generation/absorption zones,
  scale bars, human silhouette, real units + timestamp)
- x–t heatmap with a diverging blue–white–red colormap centred on still water
- perspective 3D surface view
- side-by-side comparison of runs from this session with linked playback,
  toggles, and scales

Endpoints:
  /                      interactive viewer (run picker opens automatically)
  /view/<run_id>         interactive viewer for one run (?compare=id1,id2)
  /api/runs              JSON list of runs on disk (+ "this session" flags)
  /api/data/<run_id>     JSON payload: params, x grid, η snapshots, times,
                         zones, depth — everything read from solver output
  /api/kinematics/<run_id>[?generate=1]
                         list (or run) OceanWave3D's bundled ReadKinematics.m
                         subsurface-kinematics figures, via Octave
  /static/<file>         viewer assets (html/js/css)
  /files/<run_id>/<f>    raw .gif/.png downloads from a run directory

- Started lazily on first use; daemon thread, exits with the MCP server.
- Binds to 127.0.0.1 only — never reachable from the network.
- All data served is read directly from solver output files; nothing is
  interpolated or estimated.
"""
import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from . import octave_viz
from .output_parser import load_output
from .runner import SESSION_RUN_IDS, SIMULATIONS_DIR

_PREFERRED_PORT = 8417  # falls back to an OS-assigned port if taken

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
_CONTENT_TYPES = {".gif": "image/gif", ".png": "image/png"}
_FORMAT_FILES = {"gif": "animation.gif", "png": "final.png"}

_SCENARIO_PHRASES = {
    "stream_function_wave": "nonlinear regular wave",
    "linear_regular_wave": "linear regular wave",
    "nonlinear_standing_wave": "standing wave",
}

_lock = threading.Lock()
_server = None
_port = None


# ---------------------------------------------------------------------------
# Run metadata helpers
# ---------------------------------------------------------------------------

def _safe_run_dir(run_id: str):
    """Resolve run_id to a directory directly under simulations/, else None."""
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    run_dir = (SIMULATIONS_DIR / run_id).resolve()
    if run_dir.parent != SIMULATIONS_DIR.resolve() or not run_dir.is_dir():
        return None
    return run_dir


def _load_params(run_dir: Path) -> dict:
    params_file = run_dir / "params.json"
    if not params_file.exists():
        return {}
    try:
        p = json.loads(params_file.read_text())
        return p if isinstance(p, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _num(v) -> str:
    """Compact human-readable number: 1.0 → '1', 0.080 → '0.08'."""
    return f"{float(v):g}"


def plain_title(params: dict, run_id: str) -> str:
    """Plain-language run title from the input schema.

    '0.08 m waves, 1 s period, 1 m water depth — nonlinear regular wave'
    Falls back to the run_id when params.json is missing.
    """
    H = params.get("wave_height_m")
    T = params.get("wave_period_s")
    h = params.get("water_depth_m")
    if H is None or T is None or h is None:
        return run_id
    title = f"{_num(H)} m waves, {_num(T)} s period, {_num(h)} m water depth"
    phrase = _SCENARIO_PHRASES.get(params.get("scenario", ""))
    if phrase:
        title += f" — {phrase}"
    return title


def _zones(params: dict) -> list:
    """Zone annotations for the viewer.

    New runs store them in params.json (inp_builder). For runs recorded
    before that, re-derive from the fixed 1/8+1/8+1/2+1/4 zone layout the
    builders have always used.
    """
    z = params.get("zones")
    if isinstance(z, list):
        return z
    Lx = params.get("domain_length_m")
    if params.get("scenario") in ("stream_function_wave", "linear_regular_wave") and Lx:
        Lx = float(Lx)
        return [
            {"x0": 0.0, "x1": round(Lx / 4.0, 4),
             "kind": "generation", "label": "waves created here"},
            {"x0": round(6.0 * Lx / 8.0, 4), "x1": round(Lx, 4),
             "kind": "absorption", "label": "waves absorbed here"},
        ]
    return []


def _dt_snapshot(params: dict) -> float:
    """Simulated seconds between consecutive fort snapshots (0 if unknown).

    Mirrors the output stride chosen by inp_builder: stride = max(2, N//100).
    """
    try:
        dt = float(params.get("timestep_s", 0) or 0)
        num_steps = int(params.get("num_steps", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if dt <= 0 or num_steps <= 0:
        return 0.0
    stride = max(2, num_steps // 100)
    return stride * dt


def _has_snapshots(run_dir: Path) -> bool:
    """Cheap check: does the run contain any fort.1XX wave-field output?"""
    try:
        return any(
            f.name.startswith("fort.") and f.name[5:].isdigit()
            and 100 <= int(f.name[5:]) <= 898
            for f in run_dir.iterdir()
        )
    except OSError:
        return False


# ---------------------------------------------------------------------------
# JSON payloads
# ---------------------------------------------------------------------------

def _runs_payload() -> dict:
    runs = []
    if SIMULATIONS_DIR.is_dir():
        for d in sorted(SIMULATIONS_DIR.iterdir(), reverse=True):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if not _has_snapshots(d):
                continue
            params = _load_params(d)
            runs.append({
                "id": d.name,
                "title": plain_title(params, d.name),
                "scenario": params.get("scenario"),
                "session": d.name in SESSION_RUN_IDS,
            })
    return {"runs": runs}


def _run_payload(run_id: str):
    """Full data payload for one run, or None if it has no usable output."""
    run_dir = _safe_run_dir(run_id)
    if run_dir is None:
        return None
    try:
        out = load_output(str(run_dir))
    except Exception:  # noqa: BLE001
        return None
    if not out.snapshots:
        return None

    params = _load_params(run_dir)
    dt_snap = _dt_snapshot(params)
    n = len(out.snapshots)
    times = ([round(i * dt_snap, 4) for i in range(n)] if dt_snap > 0
             else list(range(n)))

    depth = params.get("water_depth_m")
    try:
        depth = float(depth) if depth is not None else None
    except (TypeError, ValueError):
        depth = None

    return {
        "run_id": run_id,
        "title": plain_title(params, run_id),
        "params": params,
        "x": [round(v, 4) for v in out.snapshots[0].x],
        "eta": [[round(e, 6) for e in s.E] for s in out.snapshots],
        "times": times,
        "has_time": dt_snap > 0,
        "dt_snapshot": dt_snap,
        "depth": depth,
        "zones": _zones(params),
        "max_elevation": out.max_elevation,
        "min_elevation": out.min_elevation,
        "session": run_id in SESSION_RUN_IDS,
        "has_gif": (run_dir / _FORMAT_FILES["gif"]).exists(),
        "has_png": (run_dir / _FORMAT_FILES["png"]).exists(),
        # Subsurface kinematics figures (OceanWave3D's ReadKinematics.m, via
        # Octave). True only if a NON-EMPTY kinematics binary exists to plot
        # from — a 0-byte file means the run never time-stepped.
        "has_kinematics_data": octave_viz.has_kinematics_data(run_dir),
        "kinematics_ready": (run_dir / "kinematics_index.json").exists(),
    }


def _kinematics_payload(run_id: str, generate: bool) -> dict:
    """List (or generate) the bundled kinematics figures for a run.

    Returns {"figures": [{file,title,url}], "octave": bool, "error": str|None}.
    Generation runs OceanWave3D's own ReadKinematics.m through Octave.
    """
    from . import octave_viz

    run_dir = _safe_run_dir(run_id)
    if run_dir is None:
        return {"figures": [], "octave": octave_viz.kinematics_available(),
                "error": f"run '{run_id}' not found"}

    error = None
    if generate:
        try:
            octave_viz.generate_kinematics_plots(run_dir)
        except RuntimeError as exc:  # octave missing / no data / script error
            error = str(exc)

    figures = [
        {"file": f["file"], "title": f["title"],
         "url": f"/files/{quote(run_id)}/{quote(f['file'])}"}
        for f in octave_viz.list_kinematics_plots(run_dir)
    ]
    # "octave" here means "kinematics can be rendered" — by host Octave OR the
    # Docker sandbox image (which bundles Octave). Drives the viewer's gate
    # between the "generate" button and the "not available" note.
    return {"figures": figures, "octave": octave_viz.kinematics_available(),
            "error": error}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _error_page(title: str) -> str:
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head>"
        f"<body style=\"font-family:system-ui;background:#1a1d21;color:#e8e8e8;"
        f"text-align:center;margin-top:4rem\"><h1>{html.escape(title)}</h1>"
        "<p><a style=\"color:#6ab0f3\" href=\"/\">back to the viewer</a></p>"
        "</body></html>"
    )


class _Handler(BaseHTTPRequestHandler):
    server_version = "OceanWave3DViewer/2.0"

    def log_message(self, fmt, *args):  # noqa: A002
        pass  # stdout/stderr belong to the MCP stdio transport — stay quiet

    def _send(self, code: int, ctype: str, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, page: str, code: int = 200) -> None:
        self._send(code, "text/html; charset=utf-8", page.encode("utf-8"))

    def _send_json(self, obj, code: int = 200) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj).encode("utf-8"))

    def _send_404(self) -> None:
        self._send_html(_error_page("404 — not found"), code=404)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]

        if not parts or (parts[0] == "view" and len(parts) == 2):
            # The viewer app reads the run id (and ?compare=, ?format=) from
            # its own URL, so the same page serves / and /view/<run_id>.
            self._send_static("viewer.html")
        elif parts[0] == "static" and len(parts) == 2:
            self._send_static(parts[1])
        elif parts[0] == "api" and len(parts) == 2 and parts[1] == "runs":
            self._send_json(_runs_payload())
        elif parts[0] == "api" and len(parts) == 3 and parts[1] == "data":
            payload = _run_payload(parts[2])
            if payload is None:
                self._send_json({"error": f"run '{parts[2]}' not found or has no output"},
                                code=404)
            else:
                self._send_json(payload)
        elif parts[0] == "api" and len(parts) == 3 and parts[1] == "kinematics":
            from urllib.parse import parse_qs
            generate = parse_qs(parsed.query).get("generate", ["0"])[0] == "1"
            self._send_json(_kinematics_payload(parts[2], generate))
        elif (parts[0] == "api" and len(parts) == 4 and parts[1] == "kinematics"
              and parts[3] == "zip"):
            self._send_kinematics_zip(parts[2])
        elif parts[0] == "files" and len(parts) == 3:
            self._send_image(parts[1], parts[2])
        else:
            self._send_404()

    def _send_static(self, fname: str) -> None:
        ctype = _STATIC_TYPES.get(Path(fname).suffix.lower())
        valid = (ctype is not None
                 and "/" not in fname and "\\" not in fname and ".." not in fname)
        target = _STATIC_DIR / fname
        if not valid or not target.is_file():
            self._send_404()
            return
        self._send(200, ctype, target.read_bytes())

    def _send_image(self, run_id: str, fname: str) -> None:
        run_dir = _safe_run_dir(run_id)
        ctype = _CONTENT_TYPES.get(("." + fname.rsplit(".", 1)[-1]).lower())
        valid = (run_dir is not None and ctype is not None
                 and "/" not in fname and "\\" not in fname and ".." not in fname)
        if valid:
            target = (run_dir / fname).resolve()
            valid = target.parent == run_dir and target.is_file()
        if not valid:
            self._send_404()
            return
        self._send(200, ctype, target.read_bytes())

    def _send_kinematics_zip(self, run_id: str) -> None:
        """Bundle a run's generated kinematics PNGs into a single ZIP download."""
        import io
        import zipfile

        run_dir = _safe_run_dir(run_id)
        if run_dir is None:
            self._send_404()
            return
        pngs = sorted(run_dir.glob("kinematics_*.png"))
        if not pngs:
            self._send_404()
            return
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in pngs:
                zf.write(p, arcname=p.name)
        self._send(200, "application/zip", buf.getvalue())


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Default SO_REUSEADDR on Windows would let two instances bind the same
    # port; disable it so a conflict falls through to an OS-assigned port.
    allow_reuse_address = False


def ensure_running() -> int:
    """Start the viewer server if it isn't running; return the bound port."""
    global _server, _port
    with _lock:
        if _server is None:
            try:
                srv = _Server(("127.0.0.1", _PREFERRED_PORT), _Handler)
            except OSError:
                srv = _Server(("127.0.0.1", 0), _Handler)
            threading.Thread(target=srv.serve_forever, daemon=True,
                             name="oceanwave-viewer").start()
            _server, _port = srv, srv.server_address[1]
        return _port


def view_url(run_id: str, fmt: str = "gif", compare=None, view=None) -> str:
    """Return the browser URL for a run's interactive viewer (starts the server).

    fmt sets the initial playback state: "gif" autoplays the animation,
    "png" opens paused on the final snapshot. compare is an optional list of
    additional run_ids to open side by side with linked controls. view selects
    the initial tab ("section", "heatmap", "surface", "kinematics"); when None
    the viewer opens on its default cross-section view.
    """
    port = ensure_running()
    url = f"http://127.0.0.1:{port}/view/{quote(run_id)}?format={fmt}"
    if view:
        url += "&view=" + quote(view)
    if compare:
        url += "&compare=" + ",".join(quote(r) for r in compare)
    return url

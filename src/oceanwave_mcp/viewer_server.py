"""
Localhost viewer for OceanWave3D visualizations.

Chat clients (e.g. Claude Desktop) render PNG tool results inline but NOT
animated GIFs, so the MCP server hands out http://127.0.0.1:<port>/... links
instead — the user clicks the link and the browser plays the animation.

- Started lazily on first use; daemon thread, exits with the MCP server.
- Binds to 127.0.0.1 only — never reachable from the network.
- Serves only .gif/.png files located directly inside a run directory under
  simulations/, plus a small HTML viewer page per run.
"""
import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .runner import SIMULATIONS_DIR

_PREFERRED_PORT = 8417  # falls back to an OS-assigned port if taken

_CONTENT_TYPES = {".gif": "image/gif", ".png": "image/png"}
_FORMAT_FILES = {"gif": "animation.gif", "png": "final.png"}
_FORMAT_LABELS = {"gif": "Animation — all recorded snapshots",
                  "png": "Final snapshot"}

_lock = threading.Lock()
_server = None
_port = None


def _safe_run_dir(run_id: str):
    """Resolve run_id to a directory directly under simulations/, else None."""
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        return None
    run_dir = (SIMULATIONS_DIR / run_id).resolve()
    if run_dir.parent != SIMULATIONS_DIR.resolve() or not run_dir.is_dir():
        return None
    return run_dir


_PAGE_STYLE = """
  body { font-family: system-ui, sans-serif; background: #1a1d21; color: #e8e8e8;
         margin: 2rem; text-align: center; }
  h1   { font-size: 1.1rem; font-weight: 600; }
  p    { color: #9a9a9a; font-size: .85rem; }
  a    { color: #6ab0f3; }
  img  { max-width: 95vw; background: #fff; border-radius: 6px; padding: 8px;
         margin-top: 1rem; }
  ul   { list-style: none; padding: 0; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title>"
        f"<style>{_PAGE_STYLE}</style></head>\n"
        f"<body>\n{body}\n</body></html>"
    )


def _view_page(run_id: str, fmt: str) -> str:
    run_dir = _safe_run_dir(run_id)
    if run_dir is None:
        return _page("Not found", f"<h1>Run not found</h1><p>{html.escape(run_id)}</p>")

    rid = html.escape(run_id)
    fname = _FORMAT_FILES.get(fmt)
    if fname is None:
        return _page("Bad format", "<h1>Unknown format</h1><p>Use ?format=gif or ?format=png.</p>")

    other = "png" if fmt == "gif" else "gif"
    switch = ""
    if (run_dir / _FORMAT_FILES[other]).exists():
        switch = f' &middot; <a href="/view/{rid}?format={other}">switch to {other}</a>'

    if (run_dir / fname).exists():
        content = f'<img src="/files/{rid}/{fname}" alt="OceanWave3D {fmt} for {rid}">'
    else:
        content = (f"<p>No {fmt} has been rendered for this run yet. "
                   "Ask Claude for a visualization link to generate it.</p>")

    body = (f"<h1>OceanWave3D &mdash; {rid}</h1>"
            f"<p>{_FORMAT_LABELS[fmt]}{switch}</p>{content}")
    return _page(f"OceanWave3D — {run_id}", body)


def _index_page() -> str:
    items = []
    if SIMULATIONS_DIR.is_dir():
        for d in sorted(SIMULATIONS_DIR.iterdir(), reverse=True):
            if d.is_dir() and any((d / f).exists() for f in _FORMAT_FILES.values()):
                rid = html.escape(d.name)
                items.append(f'<li><a href="/view/{rid}">{rid}</a></li>')
    body = "<h1>OceanWave3D visualizations</h1>"
    body += f"<ul>{''.join(items)}</ul>" if items else "<p>No rendered runs yet.</p>"
    return _page("OceanWave3D visualizations", body)


class _Handler(BaseHTTPRequestHandler):
    server_version = "OceanWave3DViewer/1.0"

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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]

        if not parts:
            self._send_html(_index_page())
        elif parts[0] == "view" and len(parts) == 2:
            fmt = parse_qs(parsed.query).get("format", ["gif"])[0]
            self._send_html(_view_page(parts[1], fmt))
        elif parts[0] == "files" and len(parts) == 3:
            self._send_image(parts[1], parts[2])
        else:
            self._send_html(_page("Not found", "<h1>404</h1>"), code=404)

    def _send_image(self, run_id: str, fname: str) -> None:
        run_dir = _safe_run_dir(run_id)
        ctype = _CONTENT_TYPES.get(("." + fname.rsplit(".", 1)[-1]).lower())
        valid = (run_dir is not None and ctype is not None
                 and "/" not in fname and "\\" not in fname and ".." not in fname)
        if valid:
            target = (run_dir / fname).resolve()
            valid = target.parent == run_dir and target.is_file()
        if not valid:
            self._send_html(_page("Not found", "<h1>404</h1>"), code=404)
            return
        self._send(200, ctype, target.read_bytes())


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


def view_url(run_id: str, fmt: str) -> str:
    """Return the browser URL for a run's visualization (starts the server)."""
    port = ensure_running()
    return f"http://127.0.0.1:{port}/view/{run_id}?format={fmt}"

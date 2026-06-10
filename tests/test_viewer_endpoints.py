"""Smoke test for the interactive viewer server endpoints (run manually).

Usage: python tests/test_viewer_endpoints.py
Needs at least one completed run under simulations/ with an animation.gif.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp import viewer_server  # noqa: E402

port = viewer_server.ensure_running()
base = f"http://127.0.0.1:{port}"
print(f"viewer on {base}")


def get(path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


# 1. run list
status, ctype, body = get("/api/runs")
runs = json.loads(body)["runs"]
print(f"/api/runs -> {status} {ctype}, {len(runs)} runs")
for r in runs:
    print("   ", r["id"], "| title:", r["title"], "| session:", r["session"])
assert status == 200 and runs, "expected at least one run"

rid = runs[0]["id"]

# 2. run data payload
status, ctype, body = get(f"/api/data/{rid}")
d = json.loads(body)
print(f"/api/data/{rid} -> {status}, keys: {sorted(d.keys())}")
assert status == 200
assert len(d["eta"]) == len(d["times"]), "eta/times length mismatch"
assert len(d["eta"][0]) == len(d["x"]), "snapshot/x length mismatch"
print(f"    snapshots={len(d['eta'])} nx={len(d['x'])} depth={d['depth']} "
      f"dt_snap={d['dt_snapshot']} has_time={d['has_time']} zones={d['zones']}")
print(f"    title: {d['title']}")
print(f"    payload size: {len(body)/1024:.0f} KB")

# 3. viewer pages and static assets
for path, expect in [("/", "text/html"), (f"/view/{rid}", "text/html"),
                     (f"/view/{rid}?format=gif&compare={rid}", "text/html"),
                     ("/static/viewer.js", "text/javascript")]:
    status, ctype, body = get(path)
    print(f"{path} -> {status} {ctype} ({len(body)} bytes)")
    assert status == 200 and expect in ctype

# 4. gif download still served (from any run that has one rendered)
gif_rid = next(
    (r["id"] for r in runs
     if json.loads(get(f"/api/data/{r['id']}")[2]).get("has_gif")), None)
if gif_rid:
    status, ctype, body = get(f"/files/{gif_rid}/animation.gif")
    print(f"/files/{gif_rid}/animation.gif -> {status} {ctype} ({len(body)/1024:.0f} KB)")
    assert status == 200 and ctype == "image/gif"
else:
    print("no run with a rendered GIF — skipping /files/ check")

# 5. 404 paths
for path in ["/api/data/nope", "/files/../etc/passwd", "/view/a/b", "/static/..%2fserver.py"]:
    try:
        status, _, _ = get(path)
    except urllib.error.HTTPError as e:
        status = e.code
    print(f"{path} -> {status}")
    assert status == 404, f"expected 404 for {path}"

# 6. view_url helper with compare
url = viewer_server.view_url(rid, "gif", compare=[runs[-1]["id"]])
print("view_url:", url)

print("ALL ENDPOINT TESTS PASSED")

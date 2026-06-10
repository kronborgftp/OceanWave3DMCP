"""Render the interactive viewer in headless Edge and save screenshots.

Usage: python tests/test_viewer_screenshots.py
Edit the run IDs below to match runs present under simulations/ on this
machine. Screenshots land in viewer_shots/ (gitignored).
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oceanwave_mcp import runner, viewer_server  # noqa: E402

# Simulate "this session" runs so the session badge/filter path is exercised
runner.SESSION_RUN_IDS.append("20260610_155032_viewer_test_streamfunc")
runner.SESSION_RUN_IDS.append("20260610_151852_standing_wave_lowres_Nx17_Nz6_")

port = viewer_server.ensure_running()
base = f"http://127.0.0.1:{port}"
rid = "20260610_155032_viewer_test_streamfunc"
other = "20260610_151852_standing_wave_lowres_Nx17_Nz6_"

edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not Path(edge).exists():
    edge = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"

outdir = Path("viewer_shots")
outdir.mkdir(exist_ok=True)

shots = {
    "section.png": f"{base}/view/{rid}",
    "section_paused.png": f"{base}/view/{rid}?format=png",
    "heatmap.png": f"{base}/view/{rid}?view=heatmap&format=png",
    "surface.png": f"{base}/view/{rid}?view=surface&format=png",
    "compare.png": f"{base}/view/{rid}?compare={other}&format=png",
}

for name, url in shots.items():
    out = outdir / name
    cmd = [edge, "--headless", "--disable-gpu", f"--screenshot={out.resolve()}",
           "--window-size=1500,950", "--virtual-time-budget=6000",
           "--no-first-run", "--hide-scrollbars", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    time.sleep(0.3)
    print(name, "->", "OK" if out.exists() else f"FAILED rc={r.returncode}\n{r.stderr[-400:]}")

print("done")

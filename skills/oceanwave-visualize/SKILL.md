---
name: oceanwave-visualize
description: "Generates deterministic wave surface elevation visualizations from OceanWave3D solver output. Use this skill whenever the user asks to visualize, plot, animate, or show wave results."
---

## OceanWave3D Visualization Skill

This skill governs all visualization of OceanWave3D simulation results for this session.

---

### When to activate

Activate this skill whenever the user asks to:
- "show the wave", "plot the results", "visualize", "animate", "generate a GIF"
- see what the wave looks like
- compare snapshots or time evolution
- embed any image related to a simulation

---

### Strict rules — follow exactly, no exceptions

1. **Always call `generate_visualization(run_id=...)`** to produce the image.
   Never generate your own matplotlib, Chart.js, or any other code that draws
   a wave. Claude-generated wave plots are incorrect because they guess the
   wave shape rather than reading the actual solver output.

2. **The image returned by `generate_visualization` IS the visualization.**
   Embed it directly. Do not add a second chart below it.

3. **Do not describe what the wave should look like** before or after the image.
   The image speaks for itself. You may state the run ID and format used.

4. **Determinism guarantee**: the same `run_id` always produces the same image.
   The tool reads fort.1XX files written by the Fortran solver. Nothing is
   interpolated or estimated.

5. **Format selection**:
   - Default: `format="gif"` — animated GIF cycling through all recorded
     time snapshots. Use this unless the user explicitly asks for a still.
   - `format="png"` — single PNG of the final snapshot. Use when the user
     asks for "a snapshot", "the final frame", or "a still image".

---

### What the image contains

Each frame shows:
- **Blue line**: free-surface elevation η(x, t) in metres, read directly from
  the fort.1XX output file for that time step.
- **Grey dashed line**: still water level (η = 0).
- **x-axis**: horizontal domain position in metres (exact grid coordinates).
- **y-axis**: elevation in metres, scaled to ±120 % of the global peak across
  all snapshots (fixed — never changes per run).
- **Title**: `t = X.XX s  (snapshot N/M)` — computed from timestep and stride
  stored in `params.json`.

---

### Example usage

```
User: Show me the wave from the last run.
→ Call: generate_visualization(run_id="<last run_id>", format="gif")
→ Embed the returned GIF. Say: "Animated GIF — <N> snapshots from run <run_id>."

User: Give me a still of the final frame.
→ Call: generate_visualization(run_id="<run_id>", format="png")
→ Embed the returned PNG. Say: "Final snapshot from run <run_id>."
```

---

### What NOT to do

- Do not write Python/JS code that plots `sin(kx - ωt)` or any analytical form.
- Do not draw a wave using markdown or ASCII art.
- Do not call `get_detailed_results` just to plot the numbers yourself.
- Do not add "note: the wave shows…" prose after the image.

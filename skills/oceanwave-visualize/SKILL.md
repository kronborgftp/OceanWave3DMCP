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

### Two delivery modes — pick by what the user wants to see

| User wants | Tool to call | What the user gets |
|---|---|---|
| A still image | `generate_visualization(run_id=..., format="png")` | PNG of the final snapshot, rendered inline in the chat |
| The animation | `get_visualization_link(run_id=..., format="gif")` | A `http://127.0.0.1:...` link that plays the animation in their browser |

Chat clients can render inline **PNG** tool results but **NOT animated GIFs**.
Never return a GIF inline expecting it to play — for anything animated, use
`get_visualization_link` and present the URL it returns as a clickable
markdown link, e.g. `[Open the wave animation](http://127.0.0.1:8417/view/...)`.

---

### Strict rules — follow exactly, no exceptions

1. **Always use the MCP tools above** to produce the image or link.
   Never generate your own matplotlib, Chart.js, or any other code that draws
   a wave. Claude-generated wave plots are incorrect because they guess the
   wave shape rather than reading the actual solver output.

2. **The image or link MUST appear in your final, user-visible response.**
   Never leave it only inside a thinking/reasoning block — the user cannot
   see anything there. If you called the tool while reasoning, embed the
   image (or repeat the link) again in the visible answer. A response whose
   only image is inside thinking is a failed response.

3. **The image returned by `generate_visualization` IS the visualization.**
   Embed it directly. Do not add a second chart below it.

4. **Do not describe what the wave should look like** before or after the
   image or link. The visualization speaks for itself. You may state the run
   ID, the format used, and (for links) that it opens in the browser.

5. **Determinism guarantee**: the same `run_id` always produces the same
   image. The tools read fort.1XX files written by the Fortran solver.
   Nothing is interpolated or estimated.

---

### What the visualization contains

Each frame shows:
- **Blue line**: free-surface elevation η(x, t) in metres, read directly from
  the fort.1XX output file for that time step.
- **Grey dashed line**: still water level (η = 0).
- **x-axis**: horizontal domain position in metres (exact grid coordinates).
- **y-axis**: elevation in metres, scaled to ±120 % of the global peak across
  all snapshots (fixed — never changes per run).
- **Title**: `t = X.XX s  (snapshot N/M)` — computed from timestep and stride
  stored in `params.json`.

The viewer link serves the same deterministic render from this machine only
(localhost); it stays available while the OceanWave3D MCP server is running.

---

### Example usage

```
User: Show me the wave from the last run. / Animate it.
→ Call: get_visualization_link(run_id="<run_id>", format="gif")
→ Visible reply: "[Open the wave animation in your browser](<returned URL>)
   — <N> snapshots from run <run_id>."

User: Give me a still of the final frame.
→ Call: generate_visualization(run_id="<run_id>", format="png")
→ Embed the returned PNG in the visible response.
   Say: "Final snapshot from run <run_id>."
```

---

### What NOT to do

- Do not write Python/JS code that plots `sin(kx - ωt)` or any analytical form.
- Do not draw a wave using markdown or ASCII art.
- Do not call `get_detailed_results` just to plot the numbers yourself.
- Do not return an inline GIF and call it an animation — it will not play;
  use the viewer link instead.
- Do not bury the image or link in a thinking block (rule 2).
- Do not add "note: the wave shows…" prose after the image.

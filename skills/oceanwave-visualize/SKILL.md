---
name: oceanwave-visualize
description: "Generates deterministic wave surface elevation visualizations from OceanWave3D solver output. Use this skill whenever the user asks to visualize, plot, animate, compare, or show wave results."
---

## OceanWave3D Visualization Skill

This skill governs all visualization of OceanWave3D simulation results for this session.

---

### When to activate

Activate this skill whenever the user asks to:
- "show the wave", "plot the results", "visualize", "animate", "generate a GIF"
- see what the wave looks like
- compare two or more runs visually / side by side
- see a heatmap, 3D view, or annotated picture of the simulation
- embed any image related to a simulation

---

### Two delivery modes — pick by what the user wants to see

| User wants | Tool to call | What the user gets |
|---|---|---|
| A still image in the chat | `generate_visualization(run_id=..., format="png")` | PNG of the final snapshot, rendered inline in the chat |
| Anything else (animation, annotated view, heatmap, 3D, comparison) | `get_visualization_link(run_id=...)` | A `http://127.0.0.1:...` link that opens the **interactive viewer** in their browser |

Chat clients can render inline **PNG** tool results but **NOT animated GIFs**
and cannot host interactive views. For anything beyond a single still image,
use `get_visualization_link` and present the URL it returns as a clickable
markdown link, e.g. `[Open the wave animation](http://127.0.0.1:8417/view/...)`.

To compare runs visually, pass the other run ID(s):
`get_visualization_link(run_id="<primary>", compare_with="<other>,<other2>")` —
the viewer shows each run in its own plot side by side, with playback time,
view mode, toggles, and scales linked across all plots.

---

### What the interactive viewer offers (all user-controlled toggles)

The link opens a viewer that renders the solver output client-side. Do NOT
re-render variants yourself — the user can switch all of this in the browser:

- **Three views**: animated ocean **cross-section**, **x–t heatmap** with a
  diverging blue–white–red colormap centred on still water (red = above,
  blue = below, colorbar labelled in metres), and a perspective **3D surface**.
- **Annotations**: solid water fill below the surface with sand below the
  seabed and sky above; the seabed profile; shaded wave generation /
  absorption zones labelled "waves created here" / "waves absorbed here";
  labelled horizontal and vertical scale bars; an optional 1.8 m human
  silhouette; axes in metres and a `t = … s` timestamp.
- **Plain-language titles** generated from the input parameters
  ("0.08 m waves, 1 s period, 1 m water depth — nonlinear regular wave")
  instead of raw run IDs.
- **Run comparison**: a Runs panel lists every run from the current session
  (older on-disk runs behind a "show all" toggle); selected runs display
  side by side with everything except the plots themselves linked.
- Playback controls: play/pause, time scrubber, speed; the heatmap can be
  scrubbed by clicking/dragging.

URL parameters you may use when constructing or explaining links:
`?compare=id1,id2` (side-by-side), `?view=section|heatmap|surface`
(initial view), `?format=png` (open paused on the final snapshot).

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
   ID, mention the viewer's toggles/views exist, and (for links) that it
   opens in the browser.

5. **Determinism guarantee**: the same `run_id` always produces the same
   image and the same viewer data. The tools read fort.1XX files written by
   the Fortran solver. Nothing is interpolated or estimated.

---

### What the visualization contains

Inline PNG (from `generate_visualization`):
- **Blue line**: free-surface elevation η(x, t) in metres, read directly from
  the fort.1XX output file for that time step.
- **Grey dashed line**: still water level (η = 0).
- **x-axis**: horizontal domain position in metres (exact grid coordinates).
- **y-axis**: elevation in metres, scaled to ±120 % of the global peak across
  all snapshots (fixed — never changes per run).
- **Title**: `t = X.XX s  (snapshot N/M)` — computed from timestep and stride
  stored in `params.json`.

Interactive viewer (from `get_visualization_link`): the same solver data,
rendered with the annotations and views described above. Times come from
`timestep_s` × output stride, positions from the exact grid coordinates, zone
extents from the zone layout stored in `params.json`.

The viewer is served from this machine only (localhost); it stays available
while the OceanWave3D MCP server is running.

---

### Example usage

```
User: Show me the wave from the last run. / Animate it.
→ Call: get_visualization_link(run_id="<run_id>", format="gif")
→ Visible reply: "[Open the wave animation in your browser](<returned URL>)
   — toggles for water fill, seabed, zones, heatmap and 3D view are on the page."

User: Give me a still of the final frame.
→ Call: generate_visualization(run_id="<run_id>", format="png")
→ Embed the returned PNG in the visible response.
   Say: "Final snapshot from run <run_id>."

User: Compare this run with the previous one.
→ Call: get_visualization_link(run_id="<run_A>", compare_with="<run_B>")
→ Visible reply: "[Compare the two runs side by side](<returned URL>)
   — playback and scales are linked across the plots."
```

---

### What NOT to do

- Do not write Python/JS code that plots `sin(kx - ωt)` or any analytical form.
- Do not draw a wave using markdown or ASCII art.
- Do not call `get_detailed_results` just to plot the numbers yourself.
- Do not return an inline GIF and call it an animation — it will not play;
  use the viewer link instead.
- Do not render multiple PNG variants (zoomed, annotated, etc.) — the viewer's
  toggles already cover that; hand out one link.
- Do not bury the image or link in a thinking block (rule 2).
- Do not add "note: the wave shows…" prose after the image.

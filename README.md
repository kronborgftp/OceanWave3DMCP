# OceanWave3D MCP Server

An MCP (Model Context Protocol) server that wraps the **OceanWave3D** nonlinear ocean wave simulator, enabling non-expert users to run wave simulations through natural language in Claude Desktop or any MCP-compatible chat interface.

This project is part of the DTU Software Technology Bachelor course project:  
*"Integration of Scientific Computing MCP-powered Agents in Chat-based Applications"*  
Supervised by Prof. (Assoc.) Allan Peter Engsig-Karup, DTU Compute.

---

## What it does

A user can type a plain-English request like:

> *"Run a stream function wave simulation with wave height 0.1 m, depth 1.5 m, period 2 seconds"*

The MCP server translates this into a complete OceanWave3D parameter file, executes the Fortran simulation binary, parses the output, and returns wave statistics and a free-surface elevation time series — all without the user needing to know anything about the `.inp` file format or Fortran.

Results can then be explored in an **interactive localhost viewer**: an animated, annotated ocean cross-section, an x–t heatmap, a perspective 3D surface, and side-by-side comparison of runs — see [Interactive results viewer](#interactive-results-viewer).

### Example result

The prompt above produces:

| Metric | Value |
|---|---|
| Max elevation | 0.073 m |
| Min elevation | −0.065 m |
| Wave height (steady state) | 0.137 m |
| RMS elevation | 0.016 m |

Surface elevation E [m] and velocity potential P [m²/s] across the 46 m domain at a mid-simulation snapshot. The wave is fully developed in the active region (x ≈ 11–17 m) with generation on the left and a calm absorbing zone on the right.

---

## Architecture

```
User (Claude Desktop chat)
        │  natural language
        ▼
  Claude (LLM)  ──── MCP tools ────►  OceanWave3D MCP Server (Python)
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                         inp_builder     runner.py      output_parser
                         (generates    (subprocess,    (parses fort.1XX
                          .inp file)    cwd isolation)  ASCII snapshots)
                                              │
                                              ▼
                                    bin/OceanWave3D  (Fortran90 binary)
                                              │
                                              ▼
                                    fort.100 … fort.NNN  (wave snapshots)
                                              │
                                ┌─────────────┴─────────────┐
                                ▼                           ▼
                        visualizer.py                viewer_server.py
                        (GIF/PNG via               (localhost JSON API +
                         matplotlib)               interactive viewer)
                                                            │
                                                            ▼
                                              User's browser (127.0.0.1)
```

### Repository layout

```
OceanWaveMCP/
├── src/oceanwave_mcp/
│   ├── server.py          # FastMCP server — exposes the MCP tools to the LLM
│   ├── inp_builder.py     # Translates human parameters → .inp file
│   ├── runner.py          # Runs the binary in an isolated directory
│   ├── output_parser.py   # Parses fort.1XX ASCII output → wave statistics
│   ├── visualizer.py      # Deterministic GIF/PNG rendering (matplotlib)
│   ├── viewer_server.py   # Localhost server: data API + interactive viewer
│   ├── static/            # viewer.html / viewer.js — the interactive viewer app
│   └── installer.py       # Builds OceanWave3D + deps from licensed source tarballs
├── skills/                # Claude skill governing visualization behaviour
├── tests/                 # Manual smoke tests (endpoints, browser screenshots)
├── OceanWave3D-Fortran90/ # Git submodule — prof's Fortran source
├── bin/OceanWave3D        # Compiled binary (macOS/Linux, gitignored)
├── bin/OceanWave3D.exe    # Compiled binary (Windows, gitignored)
├── simulations/           # Per-run output directories (gitignored)
└── pyproject.toml
```

---

## MCP Tools

The server exposes these tools to Claude:

### `list_scenarios()`
Returns all available simulation types with parameter descriptions.

**Available scenarios:**

| Scenario | Description |
|---|---|
| `stream_function_wave` | Nonlinear regular wave using stream function theory |
| `linear_regular_wave` | Small-amplitude linear wave with generation/absorption zones |
| `nonlinear_standing_wave` | Classic benchmark — standing wave in a closed domain |

---

### `run_simulation(scenario, ...)`
Runs a simulation and returns statistics.

**Parameters** (all optional — sensible defaults apply):

| Parameter | Unit | Description |
|---|---|---|
| `scenario` | — | One of the scenarios above |
| `wave_height` | m | Crest-to-trough wave height H |
| `water_depth` | m | Still-water depth h |
| `wave_period` | s | Wave period T |
| `domain_length` | m | Horizontal domain Lx (auto-computed if omitted) |
| `grid_points_x` | — | Horizontal grid points Nx (default 129) |
| `num_periods` | — | Simulation duration in wave periods (default 15) |
| `nonlinear` | bool | Fully nonlinear equations (default `true`) |

**Returns:** run ID, wall time, measured wave height, max/min/RMS elevation.

---

### `get_detailed_results(run_id, max_snapshots=5)`
Re-reads a completed run and returns a table of surface elevation E and velocity potential P at selected time snapshots across the domain.

---

### `generate_visualization(run_id, format="gif")`
Renders a deterministic visualization from the fort.1XX solver output with matplotlib — `"png"` returns a still of the final snapshot (renders inline in chat), `"gif"` an animation cycling through all recorded snapshots. Same `run_id` always produces the same image; nothing is interpolated or invented.

### `get_visualization_link(run_id, format="gif", compare_with=None)`
Returns a `http://127.0.0.1:...` link that opens the run in the [interactive results viewer](#interactive-results-viewer) in the user's browser. Pass `compare_with="other_run_id"` (comma-separated for several) to open runs side by side with linked playback, toggles, and scales. Chat clients cannot play animated GIFs inline, so this is the tool to use for animations and anything richer than a single still.

---

### `check_installation()`
Reports whether the OceanWave3D solver is built and ready, and — if not — exactly what is missing (compiler toolchain, licensed source files, submodule).

### `install_oceanwave3d(paid_files_dir=None)`
Builds OceanWave3D from the licensed source files (LAPACK/BLAS, SPARSKIT2, Harwell) and links the solver. Runs in the **background** (the build takes several minutes); poll `installation_status()` to follow progress.

### `installation_status()`
Reports build progress: `running`, `succeeded`, `failed`, or `none`, plus the tail of the build log for diagnosing failures.

---

## Interactive results viewer

`get_visualization_link` starts a small web server bound to **127.0.0.1 only** (never reachable from the network) and returns a link to an interactive viewer. The viewer is a dependency-free HTML/JS app that fetches the parsed solver output as JSON and renders it client-side — every pixel is derived from the fort.1XX snapshot data and `params.json`, so toggling options re-renders instantly without re-running matplotlib.

### Views

| View | What it shows |
|---|---|
| **Cross-section** (default) | Animated η(x) profile drawn as an ocean cross-section: solid blue water below the surface, sand below the seabed, sky above |
| **Heatmap (x–t)** | The full space-time history at once, with a diverging blue–white–red colormap centred on still-water level (red = above, blue = below) and a colorbar labelled in metres. Click/drag to scrub time |
| **3D surface** | Perspective-rendered, light-shaded water surface (the 2D profile extruded along y — long-crested), animated over time |

### Annotation toggles

All of these are user-controlled checkboxes on the page:

- **Fill water / Seabed** — ocean-style rendering instead of a bare line graph; the bottom profile explains shoaling-type behaviour
- **Zones** — the wave **generation** and **absorption** zones are shaded and labelled *"waves created here"* / *"waves absorbed here"*, so waves appearing at the left edge don't look like an artifact. Zone extents are stored in each run's `params.json` (and re-derived for older runs)
- **Scale bars** — separate labelled horizontal and vertical bars (e.g. "2 m"), honest under vertical exaggeration; the applied exaggeration factor is displayed
- **Person (1.8 m)** — a human silhouette standing on the seabed for instant scale intuition
- **Units & time** — axes in metres and a `t = … s` timestamp overlay (computed from `timestep_s` × output stride), instead of grid indices and step counts
- **Plain titles** — auto-generated plain-language titles from the input schema: *"0.08 m waves, 1 s period, 1 m water depth — nonlinear regular wave"* instead of `20260610_155032_viewer_test_streamfunc`

### Comparing runs

The **Runs** panel lists every run from the current session (older on-disk runs behind an *"include runs from earlier sessions"* toggle). Selecting several runs shows each in its own plot side by side, while **everything else is linked**: playback time, play/pause, view mode, all annotation toggles, and (via *Lock scales*) the elevation scale, so amplitudes are directly comparable.

Useful URL parameters: `?compare=id1,id2` (side-by-side), `?view=section|heatmap|surface` (initial view), `?format=png` (open paused on the final snapshot).

The classic `animation.gif` / `final.png` files are still rendered and remain available as download links on the page.

---

## Installation

> **Tip:** If you have the licensed source tarballs, you can skip the manual build
> below and let the MCP build everything for you — see
> [Automated install via the MCP](#automated-install-via-the-mcp).

### Prerequisites

- Python 3.11+
- Claude Desktop
- gfortran (macOS/Linux) or MSYS2/MinGW-w64 (Windows)

### 1. Clone the repository

```bash
git clone https://github.com/kronborgftp/OceanWave3DMCP.git
cd OceanWave3DMCP
git submodule update --init   # pulls OceanWave3D-Fortran90 source
```

### 2. Build the binary

The Fortran source is in `OceanWave3D-Fortran90/`. Edit `OceanWave3D-Fortran90/common.mk` and set:
- `INSTALLDIR` → `../../bin`
- `FC` → `gfortran`

Then run `make` from within `OceanWave3D-Fortran90/`.

#### macOS

Install gfortran via Homebrew, then build:

```bash
brew install gcc          # provides gfortran
cd OceanWave3D-Fortran90
make
cd ..
```

The binary is installed to `bin/OceanWave3D`.

#### Linux

Install gfortran via your package manager, then build:

```bash
# Debian/Ubuntu
sudo apt install gfortran

# Fedora/RHEL
sudo dnf install gcc-gfortran

cd OceanWave3D-Fortran90
make
cd ..
```

The binary is installed to `bin/OceanWave3D`.

#### Windows

The recommended approach is **MSYS2** with the MinGW-w64 toolchain:

1. Install [MSYS2](https://www.msys2.org/) and open the **MSYS2 MinGW64** shell.
2. Install gfortran and make:
   ```bash
   pacman -S mingw-w64-x86_64-gcc-fortran make
   ```
3. Build:
   ```bash
   cd OceanWave3D-Fortran90
   make
   cd ..
   ```

The binary is installed to `bin/OceanWave3D.exe`. The MCP server detects Windows automatically and looks for the `.exe` suffix.

> **Alternative**: Use [WSL2](https://learn.microsoft.com/en-us/windows/wsl/) (Windows Subsystem for Linux) and follow the Linux instructions above.

See `OceanWave3D-Fortran90/README` for full build details.

### Automated install via the MCP

Instead of building by hand, the MCP server can compile OceanWave3D for you from
the licensed source tarballs.

**Requirements:**

1. **gfortran + make** must already be installed (the server will *not* run `sudo`
   for you). On Fedora: `sudo dnf install gcc-gfortran make`; Debian/Ubuntu:
   `sudo apt install gfortran make`; macOS: `brew install gcc make`; Windows:
   install MSYS2 and run `pacman -S mingw-w64-x86_64-gcc-fortran make`.

   On **Windows** the automated installer finds the MSYS2 toolchain itself: it
   prepends `C:\msys64\mingw64\bin` (gfortran/gcc/ar/ranlib) and `C:\msys64\usr\bin`
   (`make` plus the Unix shell utilities the legacy makefiles invoke) to the build
   subprocess PATH — without touching your system PATH. If MSYS2 lives elsewhere,
   set `OCEANWAVE3D_MSYS2_ROOT` to its root. The resulting `bin\OceanWave3D.exe` is
   statically linked, so it runs without MSYS2 on PATH.
2. Three third-party source archives must be present in a folder — by default
   `~/Documents/OceanWave3D_Files`, or any folder set via the
   `OCEANWAVE3D_FILES` environment variable. **The MCP creates this folder for
   you** (with a `README_PUT_FILES_HERE.txt` listing what to drop in) the first
   time you run `check_installation()`:
   - `Harwell.tar.gz` — Harwell Subroutine Library (HSL), <https://www.hsl.rl.ac.uk/>
     (free for academic use; licensed for commercial use)
   - `SPARSKIT2.tar.gz` — SPARSKIT2 by Y. Saad,
     <https://www-users.cse.umn.edu/~saad/software/SPARSKIT/> (free for research)
   - `lapack-3.3.1.tgz` — LAPACK 3.3.1, <https://www.netlib.org/lapack/>
     (open source)

   Only Harwell/HSL is licence-restricted; LAPACK and SPARSKIT2 are freely
   available. For DTU course work the bundle is usually provided by the
   OceanWave3D maintainers (apek@dtu.dk). `check_installation()` reports exactly
   which files are missing and where to get each.
3. The Fortran submodule must be checked out: `git submodule update --init`.

**Workflow (from the chat):**

> *Is OceanWave3D installed?* → runs `check_installation()`
>
> *Install it* → runs `install_oceanwave3d()` (builds in the background)
>
> *How's the build going?* → runs `installation_status()`

The compiled libraries land in `lib/` and the solver binary in `bin/`. The build
log is written to `simulations/.install/install.log`.

#### How the automated build works (`installer.py`)

The build chain compiles four components in dependency order, all with legacy
Fortran flags (`-std=legacy -fallow-argument-mismatch -ffree-line-length-none`)
so 1990s–2011 code compiles cleanly on modern gfortran:

1. **LAPACK + BLAS** (`lapack-3.3.1.tgz`) → `lib/liblapack.a`, `lib/libblas.a`
2. **SPARSKIT2** (`SPARSKIT2.tar.gz`) → `lib/libskit.a`
3. **Harwell** (`Harwell.tar.gz`) → `lib/libharwell.a`
4. **OceanWave3D** (the submodule) → `bin/OceanWave3D` (or `.exe` on Windows,
   statically linked so it runs without MSYS2 on PATH)

Each library step is **idempotent** — if its `.a` already exists in `lib/`, the
step is skipped, so a retry after fixing a later failure doesn't recompile
LAPACK from scratch.

Because the full build takes several minutes, `install_oceanwave3d()` launches
it as a **detached background process** (`python -m oceanwave_mcp.installer
--build`) rather than blocking the tool call. On Windows this is more than a
convenience: `make → gfortran → ar` are console apps, and without isolation a
console control event (Ctrl+C/Ctrl+Break) sent to one of them propagates to the
MCP server's process group and kills the server mid-build (seen by Claude
Desktop as "Server disconnected"). The installer detaches with
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows and `start_new_session`
(setsid) on POSIX so the build shares no console or process group with the
server.

Progress is tracked via two files under `simulations/.install/`:
- `status.json` — machine-readable state (`running` / `succeeded` / `failed`),
  PID, and timestamps; `installation_status()` reconciles a stale "running"
  record (e.g. after a crash) by checking whether the binary now exists or the
  PID is still alive.
- `install.log` — the combined stdout/stderr of every build command, tailed by
  `installation_status()` for diagnosing failures.

You can also drive the builder directly from a shell for debugging:
```bash
python -m oceanwave_mcp.installer            # prints a prerequisite report (JSON)
python -m oceanwave_mcp.installer --build    # runs the build in the foreground
```

### 3. Install the Python package

```bash
pip install -e .
```

### 4. Connect to Claude Desktop

The Claude Desktop config file location is platform-dependent.

**macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "oceanwave": {
      "command": "/usr/local/bin/oceanwave-mcp"
    }
  }
}
```

Replace the path with the output of `which oceanwave-mcp`.

**Linux** — `~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "oceanwave": {
      "command": "/home/<your-user>/.local/bin/oceanwave-mcp"
    }
  }
}
```

Replace the path with the output of `which oceanwave-mcp`.

**Windows** — `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "oceanwave": {
      "command": "C:\\Users\\<your-user>\\AppData\\Local\\Programs\\Python\\Python311\\Scripts\\oceanwave-mcp.exe"
    }
  }
}
```

Replace the path with the output of `where oceanwave-mcp` in a Command Prompt.

Restart Claude Desktop after editing the config.

---

## Example prompts

Once connected, open Claude Desktop and try:

**Basic simulation:**
> *Run a stream function wave simulation with wave height 0.1 m, depth 1.5 m, period 2 seconds*

**Comparison:**
> *Run two simulations — one linear and one nonlinear — both with wave height 0.05 m, depth 2 m, period 1.5 s. Compare the wave heights.*

**Parameter exploration:**
> *What happens to the wave height if I double the steepness? Start with H=0.05 m, h=1 m, T=1 s and compare with H=0.1 m.*

**Detailed output:**
> *Get the detailed free-surface time series from the last run*

**Visualization:**
> *Show me the wave animation* — returns a localhost link to the interactive viewer

**Visual comparison:**
> *Compare the linear and nonlinear runs side by side* — opens both runs next to each other with linked playback and scales

---

## Technical notes

**Why `<-` on every line of the `.inp` file?**  
OceanWave3D's `.inp` format uses inline comments (`<- description`) that gfortran's list-directed reader treats as an end-of-record marker. Without them, the reader silently consumes tokens across line boundaries, misaligning all parameters and causing the solver to hang indefinitely with an empty log file. All generated `.inp` files include these markers.

**Output files**  
Each run creates an isolated directory under `simulations/`. OceanWave3D writes one `fort.NNN` ASCII file per stored time snapshot (columns: x, y, E, P). `fort.999` contains bathymetry (water depth), not wave elevation, and is excluded from statistics. The range `fort.100`–`fort.898` contains wave-field snapshots. `params.json` stores the resolved input parameters, including the generation/absorption zone extents used by the viewer's zone annotations.

**Viewer server**  
The interactive viewer binds to `127.0.0.1` on port 8417 (falling back to an OS-assigned port if taken), starts lazily on the first `get_visualization_link` call, and runs as a daemon thread that exits with the MCP server. It serves only files inside run directories under `simulations/` plus its own static assets — no directory listings, no path traversal.

**Minimum grid requirements**  
The default finite-difference stencil uses order γ=3, requiring at least Nz=7 vertical layers. The inp builder enforces a minimum of Nz=9.

**Run timeout**  
Claude Desktop aborts an MCP tool call after roughly 240 s. `run_simulation` kills a runaway solve (e.g. a steep or over-resolved case that hits the solver's max iterations) after `TIMEOUT_SECONDS` (default **180 s**, overridable via `OCEANWAVE3D_SIM_TIMEOUT`) and returns a clean "Simulation timed out" result well inside that window, instead of letting the client time out the whole connection.

---

## References

- [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)
- Engsig-Karup, A.P., Bingham, H.B. and Lindberg, O. (2009). *An efficient flexible-order model for 3D nonlinear water waves*. Journal of Computational Physics, 228, 2100–2118.
- [OceanWave3D Fortran source](https://github.com/apengsigkarup/OceanWave3D-Fortran90)

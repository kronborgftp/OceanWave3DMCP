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
```

### Repository layout

```
OceanWaveMCP/
├── src/oceanwave_mcp/
│   ├── server.py          # FastMCP server — exposes 3 tools to the LLM
│   ├── inp_builder.py     # Translates human parameters → .inp file
│   ├── runner.py          # Runs the binary in an isolated directory
│   └── output_parser.py   # Parses fort.1XX ASCII output → wave statistics
├── OceanWave3D-Fortran90/ # Git submodule — prof's Fortran source
├── bin/OceanWave3D        # Compiled binary (macOS/Linux, gitignored)
├── bin/OceanWave3D.exe    # Compiled binary (Windows, gitignored)
├── simulations/           # Per-run output directories (gitignored)
└── pyproject.toml
```

---

## MCP Tools

The server exposes three tools to Claude:

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

### `check_installation()`
Reports whether the OceanWave3D solver is built and ready, and — if not — exactly what is missing (compiler toolchain, licensed source files, submodule).

### `install_oceanwave3d(paid_files_dir=None)`
Builds OceanWave3D from the licensed source files (LAPACK/BLAS, SPARSKIT2, Harwell) and links the solver. Runs in the **background** (the build takes several minutes); poll `installation_status()` to follow progress.

### `installation_status()`
Reports build progress: `running`, `succeeded`, `failed`, or `none`, plus the tail of the build log for diagnosing failures.

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
   `sudo apt install gfortran make`; macOS: `brew install gcc make`.
2. The three licensed tarballs must be present in a folder — by default
   `~/Documents/OceanWave3D_Files`, or any folder set via the
   `OCEANWAVE3D_FILES` environment variable:
   - `Harwell.tar.gz`
   - `SPARSKIT2.tar.gz`
   - `lapack-3.3.1.tgz`
3. The Fortran submodule must be checked out: `git submodule update --init`.

**Workflow (from the chat):**

> *Is OceanWave3D installed?* → runs `check_installation()`
>
> *Install it* → runs `install_oceanwave3d()` (builds in the background)
>
> *How's the build going?* → runs `installation_status()`

The compiled libraries land in `lib/` and the solver binary in `bin/`. The build
log is written to `simulations/.install/install.log`.

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

---

## Technical notes

**Why `<-` on every line of the `.inp` file?**  
OceanWave3D's `.inp` format uses inline comments (`<- description`) that gfortran's list-directed reader treats as an end-of-record marker. Without them, the reader silently consumes tokens across line boundaries, misaligning all parameters and causing the solver to hang indefinitely with an empty log file. All generated `.inp` files include these markers.

**Output files**  
Each run creates an isolated directory under `simulations/`. OceanWave3D writes one `fort.NNN` ASCII file per stored time snapshot (columns: x, y, E, P). `fort.999` contains bathymetry (water depth), not wave elevation, and is excluded from statistics. The range `fort.100`–`fort.898` contains wave-field snapshots.

**Minimum grid requirements**  
The default finite-difference stencil uses order γ=3, requiring at least Nz=7 vertical layers. The inp builder enforces a minimum of Nz=9.

---

## References

- [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro)
- Engsig-Karup, A.P., Bingham, H.B. and Lindberg, O. (2009). *An efficient flexible-order model for 3D nonlinear water waves*. Journal of Computational Physics, 228, 2100–2118.
- [OceanWave3D Fortran source](https://github.com/apengsigkarup/OceanWave3D-Fortran90)

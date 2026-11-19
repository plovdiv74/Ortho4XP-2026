# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
Ortho4XP builds orthophoto scenery tiles (1°×1° lat/lon) for the X-Plane flight simulator. It is a pure-Python app (Tkinter GUI + CLI) that shells out to bundled native binaries in `Utils/{win,lin,mac}/` (Triangle4XP mesher, DSFTool, nvcompress, 7z). Version 1.40 targets XP12 water requirements.

## Commands
- Run GUI: `python Ortho4XP.py` (must be run from the repo root — all data paths are relative to `.`, see `src/O4_File_Names.py`)
- Run CLI for one tile: `python Ortho4XP.py <lat> <lon> [<provider_code> <zl>]` (without provider/zl it reads the tile's existing config)
- Local venv: `env/` (Python 3.12, Windows) → `env\Scripts\python.exe Ortho4XP.py`
- Python deps: `pip install -r requirements.txt` (pinned to the versions in `env/`). `scikit-fmm` is effectively required: `O4_Mask_Utils.py` imports it unconditionally, even though the README calls it optional. GDAL (`osgeo`) is optional and only used by `O4_DEM_Utils.py` when present.
- Rebuild Triangle4XP (C, `Utils/src/`): CMake via `Utils/CMakeLists.txt`; helper scripts `Utils/run/{configure,build,install}.py`, cross-compile toolchains in `Utils/toolchains/`.
- There is no test suite, linter config, or CI. Verify changes by running the affected step on a real tile.

## Architecture
**Build pipeline** — four sequential steps, each taking a `CFG.Tile` object (see `Ortho4XP.py` and `TILE.build_all`):
1. `VMAP.build_poly_file` (`O4_Vector_Map.py`) — downloads OSM data via Overpass (`O4_OSM_Utils.py`), merges airports/roads/water/sea/patches into a planar vector map → `.poly`/`.node` input files.
2. `MESH.build_mesh` (`O4_Mesh_Utils.py`) — samples DEM (`O4_DEM_Utils.py`), runs external `Triangle4XP` via `subprocess` → `.mesh` file.
3. `MASK.build_masks` (`O4_Mask_Utils.py`) — rasterizes water triangles into PNG alpha masks for sea/water transitions.
4. `TILE.build_tile` (`O4_Tile_Utils.py`) — runs `DSF.build_dsf` in one thread, which pushes needed textures onto a download queue; a download thread fetches imagery (`O4_Imagery_Utils.py`) and feeds a convert queue processed by `max_convert_slots` workers (`O4_Parallel_Utils.py`) for JPEG→DDS. DSF is written as `.dsf.tmp` and renamed on success.

**Configuration (non-obvious)** — `O4_Config_Utils.py` holds a `cfg_vars` dict. Each entry with a `"module"` key is a *global variable injected into that module* (`UI`, `TILE`, `IMG`, `OSM`, `OVL`, ...) via `exec` at import time; entries without one are tile-level vars copied onto each `Tile` instance. Consequences:
- To add a setting, add it to `cfg_vars` (and to the relevant list such as `list_tile_vars` if per-tile); don't hard-code module globals elsewhere.
- `CFG` must be imported **last** (it mutates other modules' globals).
- Precedence: defaults → global `Ortho4XP.cfg` (generated at first launch, not in git) → per-tile `Ortho4XP_<lat><lon>.cfg` in the tile build dir.

**Shared state / UI** — `O4_UI_Utils.py` holds process-wide globals: `red_flag` (user abort; long loops must check it and return), `is_working` (re-entrancy guard), `verbosity`, `cleaning_level`. Use `UI.vprint` / `UI.lvprint` (log to `Ortho4XP.log`) rather than `print`. `UI.gui` is `None` in CLI mode, so GUI calls must be guarded.

**OSM data sources** — all OSM requests go through `OSM.get_overpass_data(query, bbox)`, which returns raw Overpass-style XML parsed line by line by `OSM_layer.update_dicosm` (the exact XML line layout matters). Results are cached per tile in `OSM_data/` as `.osm.bz2` and reused before any request. If `osm_pbf_source` is set (a `.osm.pbf` file or a directory of Geofabrik extracts), `O4_OSM_Local.py` answers the same queries offline with pyosmium instead of Overpass. Public Overpass servers reject requests without a real User-Agent and are often overloaded.

**Imagery providers** — data-driven, not code: `Providers/**/*.lay` files (key=value: `url_template`, `grid_type`, `max_zl`, `imagery_dir`, ...) and `.comb` files for combined providers, loaded by `IMG.initialize_*_dict()`. Python hooks for custom URLs live in `Providers/O4_Custom_URL.py` (the Providers dir is on `sys.path`). `Extents/` and `Filters/` hold provider coverage polygons and color filters.

**File naming** — all tile/path naming (e.g. `zOrtho4XP_+45-002`, `Earth nav data/+40-010/+45-002.dsf`) goes through `O4_File_Names.py`; reuse its helpers.

**Code style** — functions communicate success via `return 1` / `return 0`, with heavy use of broad `try/except`. Keep new code consistent with that convention. `src/Unused/` is dead code.

## Workflow Orchestration
### 1. Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately - don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity
### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution
### 3. Self-Improvement Loop
- After ANY correction from the user: update 'tasks/lessons.md' with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project
### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness
### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes - don't over-engineer
- Challenge your own work before presenting it
### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests - then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how
## Task Management
1. **Plan First**: Write plan to 'tasks/todo.md' with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to 'tasks/todo.md'
6. **Capture Lessons**: Update 'tasks/lessons.md' after corrections
## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

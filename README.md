# edem-deck-editor

**Edit Altair EDEM simulation decks from the command line — no GUI clicking, no pre-processing API needed.**

[中文说明 →](README.zh-CN.md)

An agent skill + two Python CLIs that read and write EDEM deck files directly with `h5py`.
It started in a ball-mill DEM parameter study and was generalized for **any** EDEM 2020–2022.2 simulation workflow: contact calibration sweeps (angle of repose, hopper/silo discharge), single- or multi-species particle studies, rotating drums, mixers, vibrating screens, and batch matrix generation — without touching the GUI.

## What it does

| Capability | Where the value lives | Guard rails |
|---|---|---|
| Kinematics (rotation & generic) | `GeometryGroups/*/Kinematics/*` | `--rpm` / `--speed` + `--geometry` selector + `--clockwise`; `--kinematic-attr` edits any motion attribute preserving HDF5 dtype |
| Particle size / scaling | `ParticleTypes/*/spheres` | Single-sphere recomputes `raw volume/mass/inertia` & sets `auto properties=1`; multi-sphere proportionally scales child radii & `pos` and advises `--particle-properties` |
| Factories (count & mass) | `Factories/*` (`num to create` / `mass to create`) | `0` sets `enabled=0` safely; `--fill-duration` derives creation rate; `--list` flags duplicate particle bindings |
| Materials & contacts | `Materials`, `Interactions` compound tables | Updating density auto-syncs single-sphere mass/inertia; supports full `e,sf,rf` or single-field `--interaction-e/-sf/-rf` for calibration sweeps |
| Periodic boundaries & gravity | `Domain@domain {x,y,z}Periodic`, `@gravity` | Untouched unless explicitly passed via CLI or matrix column |
| Physics model on/off | `PhysicsModels/*/order` | Syncs `enable rolling friction` attribute |
| Solver settings | `.dfg` `PROCESSOR:GENERAL:*` / `PROCESSOR:WRITE:*` | Timestep, grid cell size, Track Collisions, save interval, total time, threads; preserves template defaults when omitted |
| Template export | Any `.dem` → starter `.csv` | `--export-template-csv` inspects your deck and emits exact declarative column headers |
| Generic batch derivation | `.csv` or `.xlsx` → N clean 0-second decks | Works for any species/factory/contact count; emits `run_batch_edem.ps1` (Windows) and/or `run_batch_edem.sh` (Linux/macOS) |

## Why this instead of `edempy` or an MCP server?

- **Why not `edempy`?**
  - **EDEM ≤ 2022.2**: the bundled `edempy` (0.2.0 on 2022.1) is post-processing read-only — there is no pre-processing write API, and it crashes on import under `numpy >= 2.0` (`numpy.string_` removed).
  - **EDEM ≥ 2022.3**: Altair added an official Deck Editing API (`Deck(..., mode='w')`). If you are on a newer version, prefer that supported contract.
  - **In between**: this tool directly writes what EDEM writes — `_data/0.h5` (`CreatorData/0`) and the sibling `.dfg` (`KEYPATHVALUE` text).
- **Why Skill + deterministic CLI instead of an MCP server?**
  - An MCP server injects tool schemas into every turn even when you aren't running EDEM, and looping 20–100 cases through LLM tool calls burns hundreds of thousands of tokens while risking parameter drift.
  - A thin `SKILL.md` + declarative CSV/XLSX matrix + local Python CLI executes an entire parameter sweep in <1 second with zero drift and >95% lower token overhead.

## Install

Requires Python 3.8+.

```bash
pip install h5py numpy
# Optional: only required if reading .xlsx matrices (.csv needs no extra package)
pip install openpyxl
```

Set `EDEM_BIN` once if you want generated batch scripts to pick up your solver binary automatically:

```powershell
# Windows PowerShell
$env:EDEM_BIN = Join-Path $env:ProgramFiles "Altair\2022.1\EDEM\bin\edem.exe"
```

```bash
# Linux / HPC cluster
export EDEM_BIN="/opt/altair/2022.1/edem/bin/edem"
```

## Usage

### 1. Inspect any deck (read-only)

```bash
python scripts/edem_set_param.py ./cases/template.dem --list
```

### 2. Edit a single deck

```bash
# Rotating drum / mill example
python scripts/edem_set_param.py ./cases/case.dem \
    --geometry Drum --rpm 40 --clockwise \
    --diameter-mm 18 --radius-material Ore \
    --factory-mass "Feed_Factory=5.0" --fill-duration 1.0 \
    --interaction-sf "Ore~Wall=0.45" --interaction-rf "Ore~Wall=0.05" \
    --timestep 5e-6
```

### 3. Export a matrix template directly from your deck & batch-derive cases

```bash
# Inspect your deck and generate a starter CSV with exact factory/particle/contact column names
python scripts/edem_set_param.py ./cases/template.dem --export-template-csv ./cases/matrix.csv

# Dry-run the matrix (works with .csv or .xlsx)
python scripts/edem_batch.py \
    --template ./cases/template.dem \
    --matrix ./cases/matrix.csv \
    --out-dir ./cases/batch \
    --dry-run

# Generate clean 0-second decks + Windows/Linux solver queues
python scripts/edem_batch.py \
    --template ./cases/template.dem \
    --matrix ./cases/matrix.csv \
    --out-dir ./cases/batch \
    --shell both
```

### Declarative matrix column syntax

Every column (except `case_id`) is optional — include only the parameters you want to sweep:

- `case_id` (required), `case_type` (optional note)
- `rpm` / `rpm:<geometry>`, `speed_rad` / `speed_rad:<geometry>`, `kin:<geometry>:<attr>`
- `d_mm:<particle_or_material>`, `r_m:<target>`, `scale:<target>` (blank or `<= 0` safely skipped)
- `count:<factory>`, `mass_kg:<factory>`, `rate:<factory>`, `fill_duration_s`
- `density:<material>`, `shear:<material>`, `poisson:<material>`
- `interaction:<A~B>` (`e,sf,rf`) or single-field `e:<A~B>`, `sf:<A~B>`, `rf:<A~B>`
- `periodic_x`, `periodic_y`, `periodic_z`, `gravity`
- `timestep_s`, `timestep_us`, `cell_size_m`, `sim_time_s`, `save_interval_s`, `track_collisions`, `threads`

See ready-to-use examples in `examples/`:
- [`examples/contact_calibration.csv`](examples/contact_calibration.csv) — friction/restitution calibration sweep
- [`examples/single_species_sweep.csv`](examples/single_species_sweep.csv) — single-species speed/size/mass feed sweep
- [`examples/cases.example.csv`](examples/cases.example.csv) — dual-media mill matrix (including single-species `count=0` validation rows)

Legacy two-species Excel headers (via `edem_batch_from_xlsx.py` or `edem_batch.py --xlsx ...`) remain 100% supported.

## Output

- Single-deck edits modify in place after automatic backup to `<deck dir>/_edem_backup/<deck>_<timestamp>/` (`.dem`, `0.h5`, `.dfg`). Disable with `--no-backup`.
- Batch mode creates `--out-dir/<case>/<case>.dem` + `<case>_data/0.h5` + sibling `.dfg`/sidecars (with `num timesteps` reset to `1`), plus `run_batch_edem.ps1` and/or `run_batch_edem.sh`.

## Upstream dependencies

| Dependency | Why |
|---|---|
| `h5py` | HDF5 read/write on `0.h5` / `.dem` |
| `numpy` | Compound-dataset round-trips |
| `openpyxl` | Optional, `.xlsx` matrices only |
| Altair EDEM (2020–2022.2) | Running the solver. This tool never launches the GUI. |

## Repo layout

```
SKILL.md                         agent-facing instructions (host-agnostic)
scripts/edem_set_param.py        single-deck inspector, editor & CSV template exporter
scripts/edem_batch.py            generic CSV/XLSX matrix → N decks + .ps1/.sh queues
scripts/edem_batch_from_xlsx.py  backward-compatible wrapper calling edem_batch.py
examples/                        starter CSV matrices (calibration, single-species, dual-media)
tests/test_edem_deck_editor.py   self-contained synthetic HDF5 + .dfg test suite
```

Run tests locally (no EDEM installation required):

```bash
python -m unittest discover -s tests -v
```

## Boundaries

Tested against **EDEM 2022.1** (HDF5 `file version = 2622234`, `.dfg` `file-version = 30.0.3`). `--list` reports `file_version` and flags untested versions.

- **Multi-sphere & polyhedral inertia**: multi-sphere scaling proportionally updates child radii and local `pos`, but exact overlapping-clump mass/inertia requires passing `--particle-properties` to the EDEM solver (enabled by default in generated queue scripts). Polyhedral vertex mesh scaling is not supported.
- **Extended contact model plugins**: parameters for JKR, Bonding V2, liquid bridge, etc. live in plugin-specific HDF5 groups outside the base `Interactions` table and are not supported.
- **Reverse-engineered format**: `file version` and `.dfg` keys are Altair internals, not a public contract. Inspect with `--list` first on other versions and keep automatic backups.
- This tool only edits local simulation files you already own. Solving still requires a licensed Altair EDEM installation.

## License

MIT — see [LICENSE](LICENSE).

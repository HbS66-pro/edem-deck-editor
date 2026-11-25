# edem-deck-editor

**Edit Altair EDEM simulation decks from the command line — no GUI clicking, no pre-processing API needed.**

[中文说明 →](README.zh-CN.md)

A small agent skill + two Python CLIs that read and write EDEM deck files directly with `h5py`.
It grew out of a real DEM ball-mill parameter study: one calibrated master deck, dozens of cases
varying media size, ball counts, mill speed and contact parameters. Pointing and clicking that
matrix by hand was the bottleneck, and EDEM ≤ 2022.2 ships no API to script it.

## What it does

| Capability | Where the value lives | Guard rails |
|---|---|---|
| Mill speed | `initial angular velocity` on the rotating geometry | `--speed` (rad/s) or `--rpm` + `--clockwise` sign convention |
| Particle radius / diameter | `ParticleTypes/*/spheres` | Recomputes `raw volume`, `raw mass`, `raw inertia` from material density and sets `auto properties=1` |
| Particle count | `Factories/*@num to create` | Count `0` sets `enabled=0` instead of a silently broken factory; `--fill-duration` derives the creation rate |
| Factory → particle binding | `FactType/type names`, `FactSize` | `--list` flags two active factories bound to the same particle |
| Materials & contacts | `Materials`, `Interactions` compound tables | Multiple entries per invocation, one backup total |
| Periodic boundaries | `Domain@domain {x,y,z}Periodic` | `--list` reports them; `--periodic z=0` disables |
| Physics model on/off | `PhysicsModels/*/order` | Also syncs the `enable rolling friction` attribute |
| Solver settings | `.dfg` `PROCESSOR:GENERAL:*` / `PROCESSOR:WRITE:*` | Timestep, cell size, Track Collisions, save interval, total time, threads |
| Batch derivation | `cases.xlsx` → N clean 0-second decks | Emits a `run_batch_edem.ps1` queue for `edem.exe -c` |

## Why not just use `edempy`?

Because on the versions this targets, you can't.

- **EDEM ≤ 2022.2** — the bundled `edempy` (0.2.0 on 2022.1) is post-processing only: it reads particle
  positions, velocities and contact forces from solved timesteps. There is no API to create or modify a
  pre-processing model. It also crashes on import under `numpy >= 2.0` (`numpy.string_` was removed).
- **EDEM ≥ 2022.3** — Altair added a Deck Editing API covering bulk/equipment materials, interactions,
  particle size, factories and basic kinematics. If you are on a new version, use that instead: it is the
  supported contract, this is a reverse-engineered one.
- **In between there is nothing.** So the tool writes the files EDEM itself writes: `_data/0.h5`
  (CreatorData group) and the sibling `.dfg` (`KEYPATHVALUE` text).

That also sets the honest expectation: **this is not a supported format.** See [Boundaries](#boundaries).

## Install

Requires Python 3.8+.

```bash
pip install h5py numpy openpyxl
```

`openpyxl` is only needed by the batch script. Nothing else to install — both CLIs are single files.

Set the solver path once if you want the generated batch script to find `edem.exe`:

```powershell
$env:EDEM_BIN = Join-Path $env:ProgramFiles "Altair\2022.1\EDEM\bin\edem.exe"
```

## Usage

Inspect a deck before touching it — file state, rotation axis, factory bindings, periodic boundaries,
physics models and the `.dfg` solver block:

```bash
python scripts/edem_set_param.py ./cases/mill.dem --list
```

Change one case in engineering units (40 r/min clockwise, Φ18 mm ceramic media, 1240 balls):

```bash
python scripts/edem_set_param.py ./cases/case.dem \
    --rpm 40 --clockwise \
    --diameter-mm 18 --radius-material Ceramic \
    --ball-count "Steel_factory=463" "Ceramic_factory=1240" --fill-duration 0.5 \
    --timestep 7.5e-7 --cell-size 0.0225
```

Derive a whole matrix from an Excel table (dry run first — it prints every resolved parameter):

```bash
python scripts/edem_batch_from_xlsx.py \
    --template ./cases/mill.dem \
    --xlsx ./cases/cases.xlsx \
    --out-dir ./cases/batch \
    --clockwise --dry-run
```

Drop `--dry-run` to actually clone. Each case becomes its own folder with a fresh `0.h5`, a `num timesteps`
reset to 1, and a `.dfg` carrying its own timestep and cell size.

Columns are resolved **by header name, not position** — Chinese or English aliases (`case_id`, `rpm`,
`steel_d_mm`, `ceramic_d_mm`, `steel_count`, `ceramic_count`, `timestep_us`, or 工况编号 / 目标转速 / 钢球直径 …).
See `examples/cases.example.csv`. Point `--sheet` at the right worksheet and `--data-start-row` at the first
data row (the header row defaults to the line above it; override with `--header-row`). A missing column fails
loudly and prints what it saw instead of reading the wrong numbers.

## Output

- Modified decks in place, with an automatic backup at `<deck dir>/_edem_backup/<deck>_<timestamp>/`
  (`.dem`, `0.h5`, `.dfg`). `--no-backup` disables it.
- Batch mode: `--out-dir/<case>/<case>.dem` + `<case>_data/0.h5` + `<case>.dfg`, plus
  `run_batch_edem.ps1` that runs every case through `edem.exe -c` in sequence.

## Upstream dependencies

| Dependency | Why |
|---|---|
| `h5py` | HDF5 access to `0.h5` / `.dem` |
| `numpy` | Compound-dataset round-trips (must read the whole table, mutate, write back) |
| `openpyxl` | Batch mode only |
| Altair EDEM (2020–2022.2) | To actually solve. The tool never launches the GUI. |

## Repo layout

```
SKILL.md                        agent-facing instructions
scripts/edem_set_param.py       single-deck editor (inspect + modify)
scripts/edem_batch_from_xlsx.py matrix → N decks + solve queue
examples/cases.example.csv      input table column layout
```

`SKILL.md` is written for any agent that reads skill manifests — point yours at it, or just call the scripts
directly. Nothing in the repo depends on a specific host or vendor.

## Boundaries

Tested against **EDEM 2022.1** (HDF5 `file version = 2622234`, `.dfg` `file-version = 30.0.3`). Same-generation
behavior is expected to hold; across major versions it may not.

- **Single-sphere particles only** for the radius/mass/inertia linkage. Multi-sphere clusters need their
  child-sphere `pos` coordinates scaled too; polyhedra need the vertex mesh scaled. Workaround: run the
  solver with `--particle-properties` and let the kernel recompute.
- **`Linear Rotation` kinematics only.** Vibrating screens (`Sinusoidal Translation/Rotation`), conveyors and
  multi-level parent-child motion need a dispatch table this version doesn't have.
- **Extended contact models** (JKR, Bonding V2, liquid bridge) keep their parameters in separate plugin
  groups, not in the base `Interactions` table. Not supported.
- **Format is reverse-engineered.** `file version` and `.dfg` keys are Altair internals, not a public
  contract. Run `--list` before writing on an unfamiliar version, and keep the automatic backup.
- The tool only reads and writes simulation files you already own. Running a solver still requires a
  licensed Altair EDEM installation.

## License

MIT — see [LICENSE](LICENSE).

# ECA-RSI in one page

**What it does.** ECA-RSI turns one single-cell dataset (the standardized output of [ECA-PP](https://github.com/chansigit/eca-pp)) into a curated, reviewable atlas release. It runs per-sample QC and clustering once, then repeats cross-sample integration and annotation and lineage-level refinement on the surviving cells, round after round, until the number of cells a round removes falls below a threshold. Deterministic kernels do the computation. LLM agents make narrow decisions: which samples to include, what a cluster is, which cells to remove. Host code checks every decision before it is applied, and every removed cell is accounted for.

**Why it exists.** Removing noisy populations changes which sources of variation dominate an embedding, so a second look at the surviving cells shows populations the first one hid. Doing that by hand for hundreds of datasets does not scale; letting an agent write its own analysis code (the first attempt) was not reproducible. ECA-RSI keeps the computation fixed and the judgement narrow and checked.

## The six parts

| Part | Code | Does | Talks to the others through |
|---|---|---|---|
| Scientific kernels | `osp`, `msp`, `zmip` (separate repositories) | QC, clustering, integration, DEG, embeddings, lineage zoom-in | Python calls from the stage programs, only through each kernel's `api` module |
| Stage programs | `ecarsi/stages/` | wrap the kernels as pool tasks, validate agent proposals, publish each stage | files: a request spec in; `publication.json` and outputs out (fields in `ecarsi/contracts.py`) |
| Orchestration | `ecarsi/control/`, run by the coordinators on Temporal | decides what a dataset does next: stages, rounds, sessions, release | submits requests to the pool and turns to the agent service, polls their receipts |
| Execution pool | `ecarsi/warm_pool/` and HyperQueue | runs bounded compute requests on Slurm worker jobs, by priority | request folders under `pool/requests/` |
| Agent service | `ecarsi/agent/` and agent-harness-bridge | runs model turns (Doubao through Ark by default) with resident runners | turn folders under `bridge/` |
| Presentation | `ecarsi/ui/` (Periscope), `ecarsi/display.py`, `ecarsi/observatory.py` | renders pages from run directories, keeps each run's display zone, shows the control plane | reads run directories and display zones |

```text
 owner ── dataset spec ──▶ start-dataset
                               │
        ┌──────────────────────▼──────────── control-plane node (a Slurm job) ─────────────┐
        │  Temporal + PostgreSQL ◀──── coordinators ×4  (ecarsi/control)                    │
        │                                 │ request folders            │ turn folders        │
        │                                 ▼                            ▼                     │
        │                  pool: scheduler + HQ server      agent service + runners ─────────┼──▶ model providers
        │                  (ecarsi/warm_pool)               (ecarsi/agent)                    │
        │  pruner · fleet-status                                                              │
        └─────────────────────────────────┬───────────────────────────────────────────────────┘
                                          ▼
                 Slurm worker jobs: stage programs (ecarsi/stages) → kernels osp · msp · zmip
                                          │ publish
                                          ▼
              run work tree (scratch) ── synced after every stage ──▶ display zone (Oak) ──▶ Periscope
                                      └─ archived when the dataset completes ─▶ <run>.tar.gz (Oak)
```

`tests/test_layers.py` enforces who may import whom ([decision 0014](decisions/0014-boundaries-are-tested.md)).

## One dataset, start to finish

1. The owner writes a dataset spec (input, run directory, budgets, round policy; `examples/dataset-v2.json`) and runs `start-dataset`. It adds the result locations from `~/.config/ecarsi/results.json`.
2. A `DatasetWorkflow` starts in Temporal. Its state lives in PostgreSQL, so it survives coordinator restarts and moves of the control plane to another node.
3. **Organize** (`00-organize/`). A pool task inspects the ECA-PP products. A planning agent proposes analysis units and each source's experiment column. The host validates the plan and writes one organized input per unit.
4. **Per sample, once** (`01-per-sample/`). One OSP pool task per experiment (QC, doublets, contamination, clustering), then an annotation session per sample.
5. **Cross-sample, every round** (`rounds/roundNN/02-cross-sample/`). In round 1 an agent decides which samples enter. MSP integrates and inspects; DEG runs in batches of pool tasks; type and quality sessions annotate the clusters and propose removals; the host applies what it accepts.
6. **Zoom-in, every round** (`03-zoom-in/`). An agent plans lineages. Each lineage is subset, re-embedded and annotated in a session of its own; the lineages merge back into one labelled matrix.
7. **Decide.** The round's ledger accounts for every cell. `round_policy.decide`, with any manual override in `loop_control.json`, continues or releases. Only cell counts decide.
8. **Release** (`release/`): `final.h5ad`, the cell ledger, `needs_review`, the summary, the UMAP data.
9. After every stage, a small pool task syncs the run's **display zone** on Oak, which is what Periscope shows. When the dataset completes, the last sync also archives the whole work tree.
10. The pruner deletes the run's pool requests. The scratch work tree stays until the owner deletes it.

Every model turn goes through the agent service. A session's tool calls (reading evidence, DEG lookups) are pool tasks of the tool class, which HyperQueue runs before batch work. A failed session restarts once with the same evidence; a second failure skips that sample or lineage and flags it for review.

## Where things are

| What | Where |
|---|---|
| Settings you edit | `~/.config/ecarsi/` — `deployment.env` (images, directories, ports), `results.json`, `models.json`; INSTALL.md A.4 |
| Code and images | the source repositories, and two Apptainer images that are the only production code ([decision 0010](decisions/0010-production-code-is-the-image.md)) |
| Runtime state | `STATE` of `deployment.env`: `control/` (Temporal, logs), `pool/`, `bridge/`, `runs/` |
| Results | the display zones and archives under `display_root` and `archive_root` of `results.json` |

## Read next

- Why it is built this way: [decisions/](decisions/README.md), one page per decision.
- Layers, module map, invariants, mechanisms: [control-plane/ARCHITECTURE.md](control-plane/ARCHITECTURE.md).
- Each stage: [ORGANIZE_V2](control-plane/ORGANIZE_V2.md), [PERSAMPLE_V2](control-plane/PERSAMPLE_V2.md), [CROSSSAMPLE_V2](control-plane/CROSSSAMPLE_V2.md), [ZOOMIN_V2](control-plane/ZOOMIN_V2.md), [DATASET_V2](control-plane/DATASET_V2.md).
- Deploy and run: [INSTALL.md](../INSTALL.md). Results and the scientific rules: [README.md](../README.md).

# ECA-RSI: Recursive Self-Improvement for an Ensemble Cell Atlas

**This workflow provides iterative quality review and cell-type annotation. It converts standardized inputs into a dataset with cell-level decision records.**

ECA-RSI coordinates sample-level QC, cross-sample integration, and lineage-level refinement. The workflow starts from [ECA-PP](https://github.com/chansigit/eca-pp) outputs. It runs dedicated analysis packages. It repeats integration and refinement on the surviving cells until the process meets a numerical stopping rule. Each analysis unit receives an annotated H5AD, reports, a cell ledger, and unresolved questions for review.

The `ecarsi` Python package (0.4.9) provides the implementation, with its analysis kernels in the same repository. It runs from two Apptainer images and installs nothing on the host. New to the code: read [docs/OVERVIEW.md](docs/OVERVIEW.md). To deploy: [INSTALL.md](INSTALL.md).

## Install

The image pair is published on GitHub's container registry; pull it instead of building it:

```bash
apptainer pull rsi-control-20261006-2.sif oras://ghcr.io/chansigit/eca-rsi/rsi-control:20261006-2
apptainer pull rsi-science-20261006-2.sif oras://ghcr.io/chansigit/eca-rsi/rsi-science:20261006-2
```

The control image carries Temporal, PostgreSQL, HyperQueue and the agent SDKs; the science image carries the kernels' environment. The code of a release is a Git commit published as a *version* next to the images ([INSTALL.md A.10](INSTALL.md#a10-ship-a-code-change-as-a-version)): releases are tagged `v0.4.x` on GitHub, and a deployment runs the version it names. The two lock files in `container/` are the record of what each image holds.

## How it runs

ECA-RSI runs on a control plane: Temporal workflows decide what each dataset does next, a HyperQueue warm pool on Slurm nodes runs the computation, and a model-turn service runs the agents. Every request pins its program files by content. Code ships as versions that run side by side ([decision 0019](docs/decisions/0019-versions-side-by-side.md)): a new version is gated on a fixed dataset next to production, then made current, and running datasets move to it at their next child workflow ([0022](docs/decisions/0022-versions-per-child-workflow-and-brakes.md)); nothing waits for executions to end. Deploy it per [INSTALL.md](INSTALL.md); the design is in [docs/control-plane/](docs/control-plane/ARCHITECTURE.md). The earlier local path (`eca-rsi run`, one dataset on one machine) was removed in 0.4.0.

## Why iterate?

Feature selection and embeddings depend on the cells that you analyze. Removing
noisy populations can change which sources of variation dominate. This change
makes previously obscured populations easier to examine in the next round.

ECA-RSI examines the data at two scales. Cross-sample integration reveals
recurring populations and quality patterns across samples. Recomputing features
within a lineage exposes finer populations that the global view can hide. The
next round rebuilds the global analysis from the surviving cells' counts.
Therefore, review the analysis iteratively. A stable cell count alone does not
establish biological accuracy.

## How the packages fit together

| Component | Responsibility in this workflow |
| --- | --- |
| [ECA-PP](https://github.com/chansigit/eca-pp) | Prepare counts, gene names, species information, QC measurements, and metadata evidence. Run this component before ECA-RSI. |
| ECA-RSI (`ecarsi`) | Organize analysis units. Identify sample columns. Decide sample inclusion. Drive rounds. Assemble releases and browser pages. |
| OSP (`osp/`) | Run QC, doublet detection, contamination estimation, clustering, and annotation proposals within each sample. |
| MSP (`msp/`) | Recompute the shared feature space. Integrate samples. Inspect populations. Apply annotations and removals. |
| ZMIP (`zmip/`) | Plan lineages. Re-embed selected lineages. Refine labels. Record removals and reassignments. Merge results. |
| `harness_bridge/` | Provide the shared agent/tool interface, runtime adapters, and failure recovery. |
| [eca-grain](https://github.com/chansigit/eca-grain) (`eca-grain`) | Aggregate a unit into audited grains after release. Use these grains for downstream work such as GRN inference. Use metacell-style aggregation in one pass. Keep every cell on the ledger. This component does not modify the release. |

OSP, MSP, ZMIP and `harness_bridge` are packages of this repository (decision 0018; formerly separate repositories, now archived). The analysis packages implement computation. They also apply decisions. Agents inspect evidence. They submit structured decisions through tools. The calling package checks these decisions. MSP also uses `standissect_lite/` to identify smaller fragments within populations.

## Prepare the input

The organize stage requires ECA-PP products in this layout:

```text
eca-pp-output/
  source-A/
    standardize/
      standardized.h5ad
      result.json
    identify_columns/
      result.json           # optional metadata evidence
  source-B/
    standardize/
      standardized.h5ad
      result.json
```

The input can also be a single source directory that contains `standardize/`. Source directory names must be unique. The system ignores ECA-PP step-local `.history/` archives. The system also ignores any ECA-RSI run root, which is a directory that holds `organize/manifest.json`. Other unexpected H5AD files cause validation to fail. Keep ECA-RSI outputs outside the input tree and the source repository.

The organize stage checks upstream status and exit codes. It opens each accepted H5AD. It validates cell/gene IDs, dimensions, and finite nonnegative values in the required `layers["counts"]`. Failed or inconsistent results block processing. Rejected sources remain in the source inventory even without an H5AD. Nonblocking `needs_review` results can proceed. The system preserves their reasons. The system saves upstream results and metadata evidence as snapshots for later review.

Samples and batches come from ECA-PP's `identify_columns/result.json` (eca-pp 0.5.4 or later, [decision 0016](docs/decisions/0016-samples-and-batches-from-eca-pp.md)): its `sample_unit` names the unit (`library`, `batch`, `whole`, or `stop`), and Harmony runs only when ECA-PP recommends a batch correction. Samples above 20,000 cells run as chunks that keep their sample as the batch. A `stop` unit (a large droplet-like source with neither batch nor library) or an older ECA-PP result stops the organize stage: re-run ECA-PP's identify-columns first. Two sources that both use `sample=S1` remain separate OSP inputs. See [docs/front-integration.md](docs/front-integration.md) for mapping formats.

An explicit sample map goes in the dataset spec as `organize.sample_map` ([DATASET_V2.md](docs/control-plane/DATASET_V2.md#explicit-sample-map-optional)). Besides overriding a source's experiment column, it can declare two cell policies (`ecarsi/policies.py`). The host applies these policies deterministically and never infers them:

```json
{
  "sources": {"Lung": {"sample_column": "plate.barcode", "rationale": "Smart-seq2 plate = library"}},
  "exclude_cells": [
    {"blank": ["mouse.id", "subtissue", "cell_ontology_class"],
     "reason": "upstream_qc_blank",
     "rationale": "wells the authors' QC dropped; metadata left blank ('missing')"}
  ],
  "batch_key": "mouse.id"
}
```

`exclude_cells` rules drop cells before the host cuts any OSP subset. A `where` rule matches exact strings and applies an AND condition across columns. A `blank` rule checks for a missing-family value in every listed column. An unknown column is an error. A rule that matches no cell produces a recorded warning. Every excluded cell is recorded with its reason in the unit's `cell_exclusions.csv.gz`. The `batch_key` names the obs column that Harmony corrects by instead of the experiment. For plate = mouse x FACS gate designs, this column is the mouse. The host requires this key to be constant within every experiment (blank cells ignored) and to take at least two values. `"batch_key": false` declares the unit one batch with no batch effect: Harmony and the per-batch HVG vote are skipped. Without a map, the sample-column agent may propose exclusion rules. The system validates these rules exactly like user rules and records them as `proposed_by: agent`.

## Run the workflow

Submit a dataset spec with `start-dataset` ([INSTALL.md](INSTALL.md#d-submit-a-dataset) has the command; `examples/dataset-v2.json` is a complete spec and [docs/control-plane/DATASET_V2.md](docs/control-plane/DATASET_V2.md) explains its fields). The spec names the ECA-PP input, the run directory, the compute budgets of every stage, and the round policy: a fixed number of rounds or the automatic stopping rule.

The agents' models come from the model catalog `~/.config/ecarsi/models.json` (harness, model and URL); their API keys stay in `~/.config/ecarsi/keys.env` or `~/.bashrc`, never in a spec or log. The default backend uses the OpenAI Agents SDK to drive Doubao through Volcengine Ark, with model `doubao-seed-2-1-turbo-260628` and `ARK_API_KEY`. See [INSTALL.md](INSTALL.md#a4-configuration-and-the-launcher).

Mapping formats for explicit experiment mappings are in [docs/front-integration.md](docs/front-integration.md).

### Processing stages

1. **Organize.** Profile upstream files and propose analysis units by using their metadata.
   Then merge or split the analysis units in code.
   A conservation check requires each source cell to belong to exactly one analysis unit.
   Cross-file barcode overlap produces warnings.
   Overlap does not establish expression identity or automatically deduplicate cells.
2. **Per sample, once.** Identify the experimental-run column and run OSP on each sample.
   Every sample is one pool request with its own CPU and memory budget.
   Annotation is on; cross-sample review requires it.
3. **First round.** Decide which samples enter integration.
   Then run MSP integration, inspection, and annotation.
   Next, run ZMIP lineage refinement.
   With one included sample, MSP skips Harmony and sample-composition evidence.
4. **Later rounds.** Take the previous ZMIP survivors.
   Preserve prior labels under `rNN_*` columns.
   Rerun MSP from counts, followed by ZMIP.
   OSP and the first-round sample-inclusion decision are not repeated.
5. **Release.** Record the stopping reason.
   Collect review items.
   Write the final dataset and cell ledger.
   Update browser pages.

OSP filters cells by using its configured QC rules.
Its annotation-stage keep/flag/drop proposals remain evidence for subsequent review.
MSP annotation applies the union of preannotation candidates, inspection drop proposals, and annotation removals.
ZMIP applies local removals and label refinements.
Lineages below the ZMIP zoom threshold (default 800 cells) retain existing annotations.
The output of ZMIP inherits the global embedding from MSP.
Global re-embedding occurs in the next round.

Completion requires successful kernel execution and validated outputs.
These validated outputs include readable H5ADs, required labels, and cell conservation against removal and reassignment ledgers.
Empty placeholder files do not mark a stage complete.
Stress, dissociation and dying removals follow the dataset's `stress_policy` ([decision 0017](docs/decisions/0017-stress-population-policy.md)): by default a removal of 10 or more cells stands only on evidence the code checks, and the cells of any other stay, labelled in `retained_state`.

### Stopping rules

In automatic mode, round 1 continues. From round 2, a unit releases when:

- the current round removed **less than 1%** of its entering cells, **or fewer than 100 cells**; or
- the last three rounds each removed **less than 2%**;

and, on top of either path, the current round removed **fewer than 1000 cells** in absolute terms.
Relative rules alone allow a 400k-cell unit to release while still dropping thousands of cells per round.
The floor keeps such units going.
The round's `reason` names it (`removed 0.81% but 1,989 cells >= 1,000 floor`).
Tune it per unit with `max_removed` in `loop_control.json`.

The entering count is MSP's `integrated.h5ad`.
The outgoing count is ZMIP's `annotated_zmip.h5ad`.
These round statistics exclude earlier OSP filtering and whole-sample exclusions.
The cell ledger covers the preceding stages too.
Label wording changes are not a stopping criterion.
Unresolved biological questions accumulate in `needs_review` rather than prompting for approval.
Execution failures or missing required outputs can still stop a unit.

The spec's `round_policy.cap` sets the automatic-mode round limit (default 15).
Reaching the limit without convergence produces a forced release with a review flag.
`round_policy.rounds` (N) overrides automatic stopping.
The workflow releases after the specified total round count, including 1.
`<unit>/loop_control.json` changes both while the run goes (see [Resume and storage](#resume-and-storage)).
Check the recorded reason before interpreting a release as converged.

## Read the results

Periscope (`eca-rsi serve`, [INSTALL.md](INSTALL.md)) shows every run: the dataset page, one page per unit across all rounds, and the MSP and ZMIP `report.html` files of every stage, with a report per zoomed lineage. Pages are rendered from the run directory on every request, so a running dataset shows its current stage.

A run has two zones. Its work tree (`output_root`, on scratch) holds everything needed to resume and replay it:

```text
<output_root>/
  spec.json  publication.json
  00-organize/                  # analysis units and their inputs
  units/<unit>/
    01-per-sample/<sample>/     # OSP outputs and report
    rounds/roundNN/
      02-cross-sample/          # MSP: inclusion, integration, inspection, annotation, report
      03-zoom-in/               # ZMIP: plan, <lineage>/ outputs and reports, merged report
      ledger/                   # cell ledger and Sankey data through this round
    release/
      final.h5ad  summary.json  needs_review.md  needs_review.json
      cell_ledger.csv.gz  cell_exclusions.csv.gz  decisions.json  sankey.json  umap.json  receipt.json
```

Its display zone, `<display_root>/<collection>/<dataset>/<run_id>/` (from `~/.config/ecarsi/results.json`), holds what Periscope shows: the pages' files, the stage reports and the release, under the same relative paths. It is synced after every stage. When the dataset completes, the whole work tree is archived to `<archive_root>/<collection>/<dataset>/<run_id>.tar.gz`, and the evidence of its hard agent sessions (restarted, context reset, or repeatedly rejected) is frozen under `<archive_root>/_cases/` for later replay ([decision 0021](docs/decisions/0021-hard-case-freezing.md), `ops/replay-case.py`).

`release/final.h5ad` contains surviving cells. The final broad and fine labels are `obs["zmip_ann_coarse"]` and `obs["zmip_ann_fine"]`. Read `summary.json` for round counts and the stopping reason. Read `needs_review.md` for steps that failed without failing the run (`degraded`, listed first), uncertain labels, policy-excluded cells, excluded samples, reassignments, and other review items. MSP requires an explicit review for adjacent coarse-label pairs. The system retains unresolved boundaries and lists them here. ZMIP requires a written explanation when a UMAP island splits across lineages. Neither missing DEGs nor a fixed graph mixing percentage proves that labels should merge. The ledger and stage-specific removal CSV files (OSP `qc_removed.csv`, MSP `annotation_removed.csv`, ZMIP `zmip_removed.csv`) record cell-level history.

Report labels and explanatory text default to English. This default does not depend on the language used to discuss or launch the analysis. Other prose languages require an explicit configuration override.

The final UMAP includes its plotting data in the HTML. Zoom, hover, and legend filtering work offline when you enable JavaScript. To write a run's pages as static files, run `python -m ecarsi index /path/to/root-or-unit`. This command does not rerun any analysis.

Periscope serves the display zones under `display_root` and `more_display_roots` of `~/.config/ecarsi/results.json`, plus the runs listed in `~/.config/ecarsi/periscope-datasets.json` (`eca-rsi serve scan-add <run dir>` adds one). It picks up changes to both without a restart. It listens on `127.0.0.1`; reach it through your own `ssh -L` forward, with `--auth-file` for a password. In an attended session in the foreground, `--ngrok [--domain D]` also opens a tunnel.

## Resume and storage

A dataset is a Temporal workflow. It survives coordinator restarts and moves of the control plane to another node. Every pool request is pinned by content and kept with its receipt, so a resumed run replays what already finished instead of recomputing it. After a failure, `resume-dataset <run_id> --reason "..."` continues the run.

Agent sessions save their accepted submissions. A failed session restarts once with the same evidence (`<session>-r2`). A second failure skips the sample (labels `unannotated`, needs_review `agent_skipped`) or the lineage (cross-sample labels kept). Cross-sample sessions restart but never skip. A stage fails when skipped cells exceed 10 % of its input.

`<unit>/loop_control.json` holds the limits `cap`, `rounds`, `extra_rounds_after_convergence` and `max_removed` (read at every round boundary) and the brakes: `brake: round` (after this round), `brake: stage` (after the stage now running) and `brake: step` (before the next sample, stage, round or release). Each ends the unit's workflow as `PAUSED: ...`; clear the control, then run `resume-dataset`. The hard brake, `python -m ecarsi.control ... brake <run_id> --hard --reason ...`, terminates the dataset's workflows now and keeps every directory resumable.

Intermediate matrices live in the pool requests that computed them. The pruner deletes those requests once the run has finished (a failed run keeps them until a later run of the same dataset completes). The release keeps `final.h5ad`, the ledgers and the reports. The scratch work tree stays until you delete it; its archive and the display zone are the durable copies.

## Validation

The test suite contains 977 tests (the kernels' included). It runs inside the images in about a minute (`ops/test-lane.sh all`); GitHub runs the subset that needs no compute image on every push. See [INSTALL.md](INSTALL.md#b3-run-the-tests).

Every release passes the gate before it becomes current: dataset 11_Shietal (9,163 cells) end to end on the new version next to production, with its display zone, work archive, reports and links checked (`ops/gate.py`). The latest ran on 2026-10-09 for 0.4.9. The latest production run, ma-devheart PCW12 (32,453 cells, 12 samples), released after four rounds on 2026-10-10 with no failed task; `ops/run-health.py` reports where a run's time went.

Older validation records are in [docs/history/](docs/history/). These records include release checks, pause and recovery, the Clayton and 19Liu runs, and the fixed-task model comparison in [docs/history/eval/RESULTS.md](docs/history/eval/RESULTS.md).

These checks establish engineering behavior. They do not establish independently validated biological accuracy.

## Development and history

Start with [docs/OVERVIEW.md](docs/OVERVIEW.md): what the system does, its six parts, and one dataset from start to finish. [docs/decisions/](docs/decisions/README.md) explains why it is built this way. See [CLAUDE.md](CLAUDE.md) for source layout, conventions, and targeted checks.
See [CHANGELOG.md](CHANGELOG.md) for release changes. The
[architecture diagram](docs/diagrams/architecture.html) shows the main package
flow. [docs/control-plane/](docs/control-plane/ARCHITECTURE.md) documents the
control-plane path.

History, oldest first:

- The project keeps a v0.1 archive (`attic-v01/`) outside the repository. This archive was never in git.
- The six-step prompt loop (`run.sh` and `steps/*.md`) let agents write their own analysis scripts through Explore → Compute → Annotate → QC → Apply → Stop. Branch `primitive` preserves this loop. The path `docs/history/primitive/` also preserves it for reference. Its prompts and timings do not describe `ecarsi`.
- `ecarsi` replaced this loop with deterministic kernels and narrow agent decisions. The local path ran one dataset per machine (`eca-rsi run`); 0.4.0 removed it.
- The control-plane path was formerly the `gen2` branch. The project merged this branch in 0.3.1. The control-plane path added Temporal workflows, the HyperQueue warm pool, and the model-turn service. The project removed the earlier Dask pool (`ecarsi.pool`) in 0.3.2. Since then, the project added HyperQueue native priorities with a feasibility gate, DEG batching, resident model runners, and deployment from two images.
- 0.4.5 brought the five analysis packages into this repository (decision 0018) and 0.4.6 made code ship as side-by-side versions gated next to production (0019); 0.4.7 lets running datasets move to the current version (0022). The images are published on ghcr.io since 0.4.9.

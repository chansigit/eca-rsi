# ECA-RSI: Recursive Self-Improvement for an Ensemble Cell Atlas

**This workflow provides iterative quality review and cell-type annotation. It converts standardized inputs into a dataset with cell-level decision records.**

ECA-RSI coordinates sample-level QC, cross-sample integration, and lineage-level refinement. The workflow starts from [ECA-PP](https://github.com/chansigit/eca-pp) outputs. It runs dedicated analysis packages. It repeats integration and refinement on the surviving cells until the process meets a numerical stopping rule. Each analysis unit receives an annotated H5AD, reports, a cell ledger, and unresolved questions for review.

The `ecarsi` Python package (0.4.1) provides the implementation. It ships inside two Apptainer images. It installs nothing on the host. New to the code: read [docs/OVERVIEW.md](docs/OVERVIEW.md). To deploy: [INSTALL.md](INSTALL.md).

## How it runs

ECA-RSI runs on a control plane: Temporal workflows decide what each dataset does next, a HyperQueue warm pool on Slurm nodes runs the computation, and a model-turn service runs the agents. Every request pins its program files by content. Deploy it per [INSTALL.md](INSTALL.md); the design is in [docs/control-plane/](docs/control-plane/ARCHITECTURE.md). The earlier local path (`eca-rsi run`, one dataset on one machine) was removed in 0.4.0.

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
| [OSP](https://github.com/chansigit/osp) (`osp-sc`) | Run QC, doublet detection, contamination estimation, clustering, and annotation proposals within each sample. |
| [MSP](https://github.com/chansigit/msp) (`msp-sc`) | Recompute the shared feature space. Integrate samples. Inspect populations. Apply annotations and removals. |
| [ZMIP](https://github.com/chansigit/zmip) | Plan lineages. Re-embed selected lineages. Refine labels. Record removals and reassignments. Merge results. |
| [agent-harness-bridge](https://github.com/chansigit/agent-harness-bridge) | Provide the shared agent/tool interface, runtime adapters, and failure recovery. |
| [eca-grain](https://github.com/chansigit/eca-grain) (`eca-grain`) | Aggregate a unit into audited grains after release. Use these grains for downstream work such as GRN inference. Use metacell-style aggregation in one pass. Keep every cell on the ledger. This component does not modify the release. |

The analysis packages implement computation. They also apply decisions. Agents inspect evidence. They submit structured decisions through tools. The calling package checks these decisions. MSP also uses [standissect-lite](https://github.com/chansigit/standissect-lite) to identify smaller fragments within populations.

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

ECA-PP's `identify_columns/result.json` and derived TSV evidence are optional. RSI aligns that evidence to the original cell IDs. It identifies experiments within each source. Two sources that both use `sample=S1` remain separate OSP inputs. A technical batch column is not automatically an experimental sample column. The system supports explicit sample mappings. See [docs/front-integration.md](docs/front-integration.md) for mapping formats.

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

The agents' models come from the model catalog `~/.config/ecarsi/models.json` (harness, model and URL, in calling order); their API keys stay in `~/.bashrc`. The default backend uses the OpenAI Agents SDK to drive Doubao through Volcengine Ark, with model `doubao-seed-2-1-turbo-260628` and `ARK_API_KEY`. See [INSTALL.md](INSTALL.md#a4-configuration-and-the-launcher).

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
Stress-related expression remains evidence for review.
This release has no blanket stress-population or mitochondrial top-DEG deletion switch.

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

Its display zone, `<display_root>/<collection>/<dataset>/<run_id>/` (from `~/.config/ecarsi/results.json`), holds what Periscope shows: the pages' files, the stage reports and the release, under the same relative paths. It is synced after every stage. When the dataset completes, the whole work tree is archived to `<archive_root>/<collection>/<dataset>/<run_id>.tar.gz`.

`release/final.h5ad` contains surviving cells. The final broad and fine labels are `obs["zmip_ann_coarse"]` and `obs["zmip_ann_fine"]`. Read `summary.json` for round counts and the stopping reason. Read `needs_review.md` for steps that failed without failing the run (`degraded`, listed first), uncertain labels, policy-excluded cells, excluded samples, reassignments, and other review items. MSP requires an explicit review for adjacent coarse-label pairs. The system retains unresolved boundaries and lists them here. ZMIP requires a written explanation when a UMAP island splits across lineages. Neither missing DEGs nor a fixed graph mixing percentage proves that labels should merge. The ledger and stage-specific removal CSV files (OSP `qc_removed.csv`, MSP `annotation_removed.csv`, ZMIP `zmip_removed.csv`) record cell-level history.

Report labels and explanatory text default to English. This default does not depend on the language used to discuss or launch the analysis. Other prose languages require an explicit configuration override.

The final UMAP includes its plotting data in the HTML. Zoom, hover, and legend filtering work offline when you enable JavaScript. To write a run's pages as static files, run `python -m ecarsi.index /path/to/root-or-unit`. This command does not rerun any analysis.

Periscope serves the display zones under `display_root` and `more_display_roots` of `~/.config/ecarsi/results.json`, plus the runs listed in `~/.config/ecarsi/periscope-datasets.json` (`eca-rsi serve scan-add <run dir>` adds one). It picks up changes to both without a restart. `--ngrok`, `--domain`, and `--auth-file` support remote access.

## Resume and storage

A dataset is a Temporal workflow. It survives coordinator restarts and moves of the control plane to another node. Every pool request is pinned by content and kept with its receipt, so a resumed run replays what already finished instead of recomputing it. After a failure, `resume-dataset <run_id> --reason "..."` continues the run.

Agent sessions save their accepted submissions. A failed session restarts once with the same evidence (`<session>-r2`). A second failure skips the sample (labels `unannotated`, needs_review `agent_skipped`) or the lineage (cross-sample labels kept). Cross-sample sessions restart but never skip. A stage fails when skipped cells exceed 10 % of its input.

`<unit>/loop_control.json` is read at every round boundary: `cap`, `rounds`, `extra_rounds_after_convergence`, `max_removed`, `pause`, `stop_after_round`, and `pause_after_stage` (`crosssample` or `zoomin`). A pause ends the unit's workflow as `PAUSED: ...`; clear the control, then run `resume-dataset`.

Intermediate matrices live in the pool requests that computed them. The pruner deletes those requests once the run has finished (a failed run keeps them until a later run of the same dataset completes). The release keeps `final.h5ad`, the ledgers and the reports. The scratch work tree stays until you delete it; its archive and the display zone are the durable copies.

## Validation

The test suite contains 454 tests. It runs inside the compute image. See [INSTALL.md](INSTALL.md#b3-run-the-tests).

The latest end-to-end regression ran on 2026-10-02. The run used dataset 11_Shietal on the `20261002-1` image pair: two fixed rounds, 9,163 to 4,941 cells, with the display zone synced after every stage and the work tree archived at completion.

Older validation records are in [docs/history/](docs/history/). These records include release checks, pause and recovery, the Clayton and 19Liu runs, and the fixed-task model comparison in [eval/RESULTS.md](eval/RESULTS.md).

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

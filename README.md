# ECA-RSI: Recursive Self-Improvement for an Ensemble Cell Atlas

**Iterative quality review and cell-type annotation, from standardized inputs
to a dataset with cell-level decision records.**

ECA-RSI coordinates sample-level QC, cross-sample integration, and lineage-level
refinement. It starts from [ECA-PP](https://github.com/chansigit/eca-pp) outputs,
runs dedicated analysis packages, and repeats integration and refinement on
the surviving cells until a numerical stopping rule is met. Each analysis
unit gets an annotated H5AD, reports, a cell ledger, and unresolved questions
for review.

The current implementation is the `ecarsi` Python package. Start with
[installation](INSTALL.md), then run:

```bash
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study
```

## Why iterate?

Feature selection and embeddings depend on the cells being analyzed. Removing
noisy populations can change which sources of variation dominate, making
previously obscured populations easier to examine in the next round.

ECA-RSI examines the data at two scales. Cross-sample integration reveals
recurring populations and quality patterns across samples. Recomputing features
within a lineage exposes finer populations that may be hidden in the global
view. The next round rebuilds the global analysis from the surviving cells'
counts. This motivates iterative review; a stable cell count alone does not
establish biological accuracy.

## How the packages fit together

| Component | Responsibility in this workflow |
| --- | --- |
| [ECA-PP](https://github.com/chansigit/eca-pp) | Prepare counts, gene names, species information, QC measurements, and metadata evidence. Runs before ECA-RSI. |
| ECA-RSI (`ecarsi`) | Organize analysis units, identify sample columns, decide sample inclusion, drive rounds, and assemble releases and browser pages. |
| [OSP](https://github.com/chansigit/osp) (`osp-sc`) | Run QC, doublet detection, contamination estimation, clustering, and annotation proposals within each sample. |
| [MSP](https://github.com/chansigit/msp) (`msp-sc`) | Recompute the shared feature space, integrate samples, inspect populations, and apply annotations and removals. |
| [ZMIP](https://github.com/chansigit/zmip) | Plan lineages, re-embed selected lineages, refine labels, record removals and reassignments, and merge results. |
| [agent-harness-bridge](https://github.com/chansigit/agent-harness-bridge) | Provide the shared agent/tool interface, runtime adapters, and failure recovery. |
| [eca-grain](https://github.com/chansigit/eca-grain) (`eca-grain`) | After release: aggregate a unit into audited grains (metacell-style, one pass, every cell on the ledger) for downstream work such as GRN inference. Does not modify the release. |

The analysis packages implement computation and apply decisions. Agents inspect
evidence and submit structured decisions through tools, with checks in the
calling package. MSP also uses
[standissect-lite](https://github.com/chansigit/standissect-lite) to identify
smaller fragments within populations.

## Prepare the input

The `run` and `organize` commands require ECA-PP products in this layout:

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

The input can also be a single source directory containing `standardize/`.
Source directory names must be unique. ECA-PP's step-local `.history/` archives
and any ECA-RSI run root (a directory holding `organize/manifest.json`, such as a
finished run mirrored next to `standardize/`) are ignored; other unexpected H5AD
files cause validation to fail. Keep ECA-RSI outputs outside the input tree and the source repository.

Organize checks upstream status and exit codes, opens each accepted H5AD, and
validates cell/gene IDs, dimensions, and finite nonnegative values in the
required `layers["counts"]`. Failed or inconsistent results block processing.
Rejected sources remain in the source inventory even without an H5AD;
nonblocking `needs_review` results can proceed with their reasons preserved.
Upstream results and metadata evidence are saved as snapshots for later review.

ECA-PP's `identify_columns/result.json` and derived TSV evidence are optional.
RSI aligns that evidence to the original cell IDs and identifies experiments
within each source. Two sources both using `sample=S1` remain separate OSP
inputs. A technical batch column is not automatically an experimental sample
column; explicit sample mappings are supported. See
[FRONT_INTEGRATION.md](FRONT_INTEGRATION.md) for mapping formats.

A sample map can also declare two cell policies that the host applies
deterministically and never infers (`ecarsi/policies.py`):

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

`exclude_cells` rules (`where`: exact string match, AND across columns;
`blank`: missing-family value in every listed column) drop cells before any
OSP subset is cut. An unknown column is an error; a rule matching no cell is
a recorded warning. Every excluded cell is listed in
`persample/excluded_cells.csv` with its reason and appears in the cell ledger
as `removed:persample-policy:<reason>`; the release's `needs_review` lists
each rule under "Cells excluded before OSP by policy". `batch_key` names the
obs column Harmony corrects by instead of the experiment (for plate = mouse x
FACS gate designs, the mouse); the host requires it to be constant within
every experiment (blank cells ignored, then filled with their experiment's
value in the OSP subset) and to take at least two values. `MSP_BATCH_COL`
still wins; a value contradicting the map is an error. Without a map, the
sample-column agent may propose exclusion rules, validated exactly like user
rules and recorded as `proposed_by: agent`; a batch-key *recommendation* from
the study design goes to `needs_review` only.

## Run the workflow

Use Python 3.10 or newer with ECA-RSI, its three kernels, and the shared bridge
installed. Install the 0.2.9 package combination from PyPI:

```bash
python -m pip install 'ecarsi[kernels]==0.2.9'
```

Follow [INSTALL.md](https://github.com/chansigit/eca-rsi/blob/main/INSTALL.md)
for environment checks and source installation. Installing `ecarsi` alone does
not install the kernels by default.

The default agent backend is OpenAI Agents SDK driving Doubao through
Volcengine Ark, with model `doubao-seed-2-1-turbo-260628`. Set `ARK_API_KEY` in
your environment before running. Other configured backends can be selected
with `--harness deepseek` or `--harness claude`. The OpenAI harness also supports
`--harness openai@openrouter` and `--harness openai@vllm`; configure the endpoint,
API key, and model as described in [INSTALL.md](INSTALL.md).

```bash
# Automatic stopping for every analysis unit.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study

# CLI values override HARNESS and MODEL environment variables.
eca-rsi --harness openai --model doubao-seed-2-1-turbo-260628 \
  run /path/to/eca-pp-output /path/to/eca-runs/study

# A fixed total of two rounds, retaining intermediate H5ADs.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study \
  --rounds 2 --no-prune

# Run on fast scratch, keep a browsable copy on long-term storage.
eca-rsi run /path/to/eca-pp-output $SCRATCH/eca-runs/study --mirror $OAK/eca-results/study
```

`python -m ecarsi` is equivalent to `eca-rsi`. The repository also provides
`./run-eca-rsi.sh <input> <root>`; set `ECA_RSI_PYTHON` to select its interpreter.
Use `eca-rsi --help` and `eca-rsi run --help` for available commands.

### Compatible packages

The 0.2.9 combination uses bridge **0.2.13**, OSP **0.1.6**, MSP **0.5.0**,
and ZMIP **0.3.8**. Dependency ranges are in `pyproject.toml`; the explicit
installation pins are in [INSTALL.md](INSTALL.md).

MSP defaults to CPU computation. `MSP_COMPUTE_ENDPOINT=dask` with
`MSP_DASK_SCHEDULER` sends Harmony, graph/clustering, and differential-expression
work to an existing Dask pool. ZMIP uses these same MSP calls for its lineages.
`MSP_COMPUTE_GPU=1` selects the RAPIDS implementations and requires a GPU worker
and compatible GPU environment. Pool startup is manual; automatic scaling,
driver placement, and OSP offloading are not implemented. See
[container/README.md](container/README.md).

To validate the input and per-sample stages before starting iterative analysis:

```bash
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/new-study --stop-after persample
```

For explicit experiment mappings and OSP options, run `organize` and
`persample` separately. The front and downstream integration records are in
[FRONT_INTEGRATION.md](FRONT_INTEGRATION.md) and
[DOWNSTREAM_INTEGRATION.md](DOWNSTREAM_INTEGRATION.md).
`FRONT_COMPATIBILITY.json` records the earlier front-only validation snapshot;
it is not the current full-workflow dependency list.

### Processing stages

1. **Organize.** Profile upstream files and propose analysis units using their
   metadata, then merge or split them in code. A conservation check requires
   each source cell to belong to exactly one analysis unit. Cross-file barcode
   overlap produces warnings; it does not establish expression identity or
   automatically deduplicate cells.
2. **Per sample, once.** Identify the experimental-run column and run OSP on
   each sample. The driver sizes concurrency from available CPUs and memory.
   Annotation is enabled by default and required for cross-sample review.
3. **First round.** Decide which samples enter integration, then run MSP
   integration, inspection, and annotation, followed by ZMIP lineage refinement.
   With one included sample, MSP skips Harmony and sample-composition evidence.
   Obs columns that are constant within every sample but differ across samples
   (e.g. FACS `subtissue`, `mouse.id`) are passed to the MSP/ZMIP agents as
   `--design-context`, so a sample-confined cluster is judged against the study
   design rather than as a batch artefact; preview with `python -m ecarsi.design <unit>`.
4. **Later rounds.** Take the previous ZMIP survivors, preserve prior labels
   under `rNN_*` columns, and rerun MSP from counts followed by ZMIP. OSP and
   the first-round sample-inclusion decision are not repeated.
5. **Release.** Record the stopping reason, collect review items, write the
   final dataset and cell ledger, and update browser pages.

OSP filters cells using its configured QC rules. Its annotation-stage
keep/flag/drop proposals remain evidence for subsequent review. MSP annotation
applies the union of preannotation candidates, inspection drop proposals, and
annotation removals. ZMIP applies local removals and label refinements; lineages
below its zoom threshold (default 800 cells) retain existing annotations.
ZMIP's output inherits MSP's global embedding; global re-embedding happens in
the next round.

Completion requires successful kernel execution and validated outputs,
including readable H5ADs, required labels, and cell conservation against removal
and reassignment ledgers. Empty placeholder files do not mark a stage complete.
Only OSP failures explicitly marked retryable receive the driver's one retry.
Stress-related expression remains evidence for review; there is no blanket
stress-population or mitochondrial top-DEG deletion switch in this release.

### Stopping rules

In automatic mode, round 1 continues. From round 2, a unit releases when:

- the current round removed **less than 1%** of its entering cells, **or fewer
  than 100 cells**; or
- the last three rounds each removed **less than 2%**;

and, on top of either path, the current round removed **fewer than 1000
cells** in absolute terms. Relative rules alone let a 400k-cell unit release
while still dropping thousands of cells per round; the floor keeps such units
going, and the round's `reason` names it (`removed 0.81% but 1,989 cells >=
1,000 floor`). Tune it per unit with `max_removed` in `loop_control.json`.

The entering count is MSP's `integrated.h5ad` and the outgoing count is ZMIP's
`annotated_zmip.h5ad`; these round statistics exclude earlier OSP filtering
and whole-sample exclusions. The cell ledger covers the preceding stages too.
Label wording changes are not a stopping criterion. Unresolved biological
questions accumulate in `needs_review` rather than prompting for approval.
Execution failures or missing required outputs can still stop a unit.

`--cap` sets the automatic-mode round limit (default 10); reaching it without
convergence produces a forced release with a review flag. `--rounds N` overrides
automatic stopping and releases after the specified total round count, including
`--rounds 1`. Check the recorded reason before interpreting a release as converged.

## Read the results

The unit's `index.html` is RSI's report across all rounds. MSP and ZMIP
`report.html` files describe individual analysis stages; ZMIP also produces
reports for each processed lineage. A standalone MSP/ZMIP run does not produce
an RSI final release.

Each analysis unit has its own release:

```text
<root>/
  index.html
  organize/manifest.json
  units/<unit>/
    index.html
    progress.log
    input/{organized.h5ad,manifest.json}
    persample/{manifest.json,<sample>/...}
    rounds/roundNN/
      crosssample/       # MSP outputs and report
      zoomin/            # ZMIP plan, lineage outputs, and reports
      ledger/            # cell ledger and Sankey plots through this round
      stats.txt
      decision.txt
    release/
      final.h5ad
      summary.md
      summary.json
      needs_review.md
      needs_review.json
      cell_ledger.csv
      sankey_coarse.png
      umap.json
```

`release/final.h5ad` contains surviving cells; the final broad and fine labels
are `obs["zmip_ann_coarse"]` and `obs["zmip_ann_fine"]`. Read `summary.md` for
round counts and stopping reasons, and `needs_review.md` for uncertain labels,
policy-excluded cells, excluded samples, reassignments, and other review
items. MSP requires an explicit review for adjacent coarse-label pairs; unresolved
boundaries are retained and listed here. ZMIP requires a written explanation when
a UMAP island is split across lineages. Neither missing DEGs nor a fixed graph
mixing percentage proves that labels should merge. The ledger and stage-specific removal CSVs (`persample/excluded_cells.csv`,
OSP `qc_removed.csv`, MSP `annotation_removed.csv`, ZMIP `zmip_removed.csv`)
record the cell-level history. Cost summaries
include only costs reported and captured by the runtime; missing cost records
do not mean a run was free or constitute a complete bill.

Report labels and explanatory text default to English, independently of the
language used to discuss or launch the analysis. Other prose languages require
an explicit configuration override.

Open a unit's `index.html` directly in a browser to view its saved report.
The final UMAP includes its plotting data in the HTML; zoom, hover and legend
filtering work offline with JavaScript enabled. Point size adapts to the plotted
cell count, panel size, and zoom. Keep the run directory together
for links to other reports and files. To update older saved pages, run
`python -m ecarsi.index /path/to/root-or-unit` (no analysis is rerun).

To browse live progress from an unfinished run or share results over HTTP:

```bash
eca-rsi serve scan-add /path/to/eca-runs/study
eca-rsi serve --port 8899
```

Open `http://127.0.0.1:8899/` on the serving machine. The server reads its dataset
registry from `~/.config/ecarsi/registry.json` by default and picks up registry
changes. `eca-rsi run ... --serve 8899` starts it after processing. Optional
`--ngrok`, `--domain`, and `--auth USER:PASS` support remote access; see
[INSTALL.md](INSTALL.md).

When the run directory lives on fast, purged scratch and the server reads a
long-term directory, pass `--mirror DIR` (to `run`, or to `organize`,
`persample`, `loop`; it is remembered in `<root>/mirror.json`, so resumed steps
keep mirroring). After every landing-page update the light files of the run
root — pages, `progress.log`, manifests, `stats.txt` / `decision.txt`, markdown,
reports, figures, small tables — are copied incrementally to DIR; at release
(after cleanup) the whole root is copied, including `final.h5ad`,
`input/organized.h5ad`, and ledgers, and files that cleanup removed are deleted
from DIR's copy of that unit only. Every page's footer shows `run state updated
<time>` (the newest state file), so a viewer of DIR knows how fresh it is; a
served DIR is also labelled a mirror copy of its source. Mirroring never reads
DIR and never fails a step: a failed copy is a warning in `progress.log`.

## Resume and storage

Repeat the same `eca-rsi run` command after an interruption to reuse validated
outputs. Organize and per-sample manifests track input, configuration, and
adapter/runtime identities. MSP and ZMIP stages also check their input content,
computation settings, runtime sources, and completed output hashes. RSI invokes
ZMIP's own resume checks even when lineage outputs already exist. Completed
rounds and releases have integrity receipts; pruned historical releases can
be checked without requiring deleted intermediate matrices.

MSP and ZMIP also save accepted agent submissions after each cluster. A restart
checks the original data, evidence and code, then revalidates the saved decisions
and continues pending clusters. ZMIP preserves refined cluster assignments and
reuses the completed integration of an unfinished lineage.

To pause a unit, set `"pause": true` in `<unit>/loop_control.json`. New sample
and lineage launches stop; running work finishes at an MSP step or lineage
boundary. The command exits with code 3 and does not publish a release. Set
`pause` to false (or remove it), then repeat the command to resume. Alternatively,
`"pause_after_stage": "crosssample"` or `"zoomin"` pauses after that stage;
remove the setting before resuming. `SIGTERM` requests the same cooperative
pause. For Slurm batch wrappers, request advance notice with
`#SBATCH --signal=B:TERM@600` and have the shell trap touch the shared
`ECA_RSI_PAUSE_FILE` while it waits for the pipeline. A hard kill can still
interrupt the current unsubmitted work; already accepted submissions are on disk.

Use a new output root when inputs or analysis code change. Legacy outputs
without the required identities or receipts remain browsable, but are not
accepted as verified completion for upgraded computation.

Harness/model changes are recorded in progress and review records and do not
invalidate completed computation. The bridge version and source are provenance;
scientific package versions, source, inputs, and computation settings remain
part of the computation identity.

For deliberate development across source changes, `ECA_RSI_DEVELOPER_MODE=1`
relaxes RSI's runtime comparison only. Reused per-sample outputs still require
their original validated receipts, and skipped runtime checks remain recorded
as `runtime_check: skipped`. Input, analysis settings, output integrity, and
kernel-level resume checks still apply. Prefer a new root for reproducible runs.

`--force-reopen` continues beyond an existing release; with `--rounds N`, choose
a total larger than the completed round count. It does not restore pruned
matrices or forward ZMIP's `--force` option.

**Release normally triggers cleanup of intermediate H5ADs.** Use `--no-prune`
on `run` or `loop` to retain them. Cleanup keeps `input/organized.h5ad`, the
release, reports, tables, and figures. Removed H5ADs leave `.pruned` markers;
those carrying labels also leave `.obs.parquet` or `.obs.csv.gz` tables for the
ledger. `release/pruned.json` records the cleanup. This preserves the decision
history, but not every intermediate expression matrix.

```bash
eca-rsi prune /path/to/eca-runs/study --dry-run
```

## Validation scope

The 0.2.9 combination has offline tests for provider selection, computation
endpoints, resume receipts, cell conservation, and release review rendering.
A replay of 19 saved chondroatlas rounds checks the new boundary-review contract
without changing historical results or calling a model. Detailed release checks
are recorded in [TAKEOVER_VALIDATION.md](TAKEOVER_VALIDATION.md).

The earlier 0.1.0 validation also checked installed-wheel resources and offline
UMAP rendering, legend selection, and zoom.

Real-data validation includes a two-round RSI run on **Clayton**, ending with
850 cells and a matching cell ledger, and a separate full-size **19Liu MSP/ZMIP**
run, from 81,079 to 75,394 cells. Clayton used historical ECA-PP 0.2 inputs;
19Liu was a downstream kernel validation, not a full RSI run. Subsequent kernel
fixes received targeted validation rather than a complete repeat of all model
decisions. These checks establish engineering behavior, not independently
validated biological accuracy. Details and remaining review items are in
[DOWNSTREAM_INTEGRATION.md](DOWNSTREAM_INTEGRATION.md).

## Development and history

See [CLAUDE.md](CLAUDE.md) for source layout, operating conventions, and targeted
checks. See [CHANGELOG.md](CHANGELOG.md) for release changes and
[TODO.md](TODO.md) for deferred policy discussions. The [architecture diagram](diagrams/architecture.html) illustrates the
main package flow; consult this README and the source for current runtime and
resume behavior.

`run.sh` and `steps/*.md` belong to the previous six-step prompt loop, preserved
on branch `primitive`. That generation used agents to write analysis scripts
through Explore → Compute → Annotate → QC → Apply → Stop. Its commands,
governance prompts, and timing examples do not describe the current `ecarsi`
workflow. `attic-v01/` is an older archive.

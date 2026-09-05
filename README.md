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
Source directory names must be unique. Every H5AD discovered under the input
must be a recognized `standardize/standardized.h5ad` with its accompanying
`result.json`; extra H5AD files cause the input check to fail. Keep ECA-RSI
outputs outside this input tree and outside the source repository.

Review upstream `result.json` files before starting. The discovery check
recognizes the file layout; it does not replace upstream quality assessment.
ECA-PP's `identify_columns/result.json` is optional. ECA-RSI identifies the
experimental-run sample column separately, and that choice takes precedence
over the upstream batch designation during integration.

## Run the workflow

Use Python 3.10 or newer with ECA-RSI, its three kernels, and the shared bridge
installed. Follow [INSTALL.md](INSTALL.md) for installation from source and
environment checks. Installing `ecarsi` alone does not install the kernels by
default.

The default agent backend is OpenAI Agents SDK driving Doubao through
Volcengine Ark, with model `doubao-seed-2-1-turbo-260628`. Set `ARK_API_KEY` in
your environment before running. Other configured backends can be selected
with `--harness deepseek` or `--harness claude`.

```bash
# Automatic stopping for every analysis unit.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study

# CLI values override HARNESS and MODEL environment variables.
eca-rsi --harness openai --model doubao-seed-2-1-turbo-260628 \
  run /path/to/eca-pp-output /path/to/eca-runs/study

# A fixed total of two rounds, retaining intermediate H5ADs.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study \
  --rounds 2 --no-prune
```

`python -m ecarsi` is equivalent to `eca-rsi`. The repository also provides
`./run-eca-rsi.sh <input> <root>`; set `ECA_RSI_PYTHON` to select its interpreter.
Use `eca-rsi --help` and `eca-rsi run --help` for available commands.

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

### Stopping rules

In automatic mode, round 1 continues. From round 2, a unit releases when:

- the current round removed **less than 1%** of its entering cells, **or fewer
  than 100 cells**; or
- the last three rounds each removed **less than 2%**.

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
excluded samples, reassignments, and other review items. The ledger and
stage-specific removal CSVs record the cell-level history. Cost summaries
include only costs reported and captured by the runtime; missing cost records
do not mean a run was free or constitute a complete bill.

To browse results, including progress from an unfinished run:

```bash
eca-rsi serve scan-add /path/to/eca-runs/study
eca-rsi serve --port 8899
```

Open `http://127.0.0.1:8899/` on the serving machine. The server reads its dataset
registry from `~/.config/ecarsi/registry.json` by default and picks up registry
changes. `eca-rsi run ... --serve 8899` starts it after processing. Optional
`--ngrok`, `--domain`, and `--auth USER:PASS` support remote access; see
[INSTALL.md](INSTALL.md).

## Resume and storage

Repeat the same `eca-rsi run` command after an interruption to reuse recorded
decisions and completed outputs. Keep the input, installed sources, and analysis
settings fixed. ECA-RSI's outer wrappers largely check required files for
existence; they do not provide end-to-end content validation. In particular,
skipping a completed ZMIP directory bypasses the newer kernel's own input,
configuration, and runtime identity checks. Use a new output root when changing
inputs or analysis code.

Modern manifests record the harness and model. Checked mismatches are rejected
unless `--allow-agent-change` is explicitly set; older manifests can only emit
a warning. This option permits a mixed run and does not recompute finished
stages. `--force-reopen` continues beyond an existing release; with `--rounds N`,
choose a total larger than the completed round count. It is not a cache reset
or a forwarded ZMIP `--force` option.

**Release normally triggers cleanup of intermediate H5ADs.** Use `--no-prune`
on `run` or `loop` to retain them. Cleanup keeps `input/organized.h5ad`, the
release, reports, tables, and figures. Removed H5ADs leave `.pruned` markers;
those carrying labels also leave `.obs.parquet` or `.obs.csv.gz` tables for the
ledger. `release/pruned.json` records the cleanup. This preserves the decision
history, but not every intermediate expression matrix.

```bash
eca-rsi prune /path/to/eca-runs/study --dry-run
```

## Development and history

See [CLAUDE.md](CLAUDE.md) for source layout, operating conventions, and targeted
checks. The [architecture diagram](diagrams/architecture.html) illustrates the
main package flow; consult this README and the source for current runtime and
resume behavior.

`run.sh` and `steps/*.md` belong to the previous six-step prompt loop, preserved
on branch `primitive`. That generation used agents to write analysis scripts
through Explore → Compute → Annotate → QC → Apply → Stop. Its commands,
governance prompts, and timing examples do not describe the current `ecarsi`
workflow. `attic-v01/` is an older archive.

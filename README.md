# ECA-RSI: Recursive Self-Improvement for an Ensemble Cell Atlas

**This workflow provides iterative quality review and cell-type annotation. It converts standardized inputs into a dataset with cell-level decision records.**

ECA-RSI coordinates sample-level QC, cross-sample integration, and lineage-level refinement. The workflow starts from [ECA-PP](https://github.com/chansigit/eca-pp) outputs. It runs dedicated analysis packages. It repeats integration and refinement on the surviving cells until the process meets a numerical stopping rule. Each analysis unit receives an annotated H5AD, reports, a cell ledger, and unresolved questions for review.

The `ecarsi` Python package (0.3.2) provides the implementation. It ships inside two Apptainer images. It installs nothing on the host. Start with [INSTALL.md](INSTALL.md).

## Two paths

| Path | What it is | Status |
| --- | --- | --- |
| **Control-plane path** | This path processes many datasets in batches. It uses Temporal workflows, a HyperQueue warm pool on Slurm nodes, and a model-turn service. Every request pins its program files by content. | Supported. Deploy per [INSTALL.md](INSTALL.md). See the design in [docs/control-plane/](docs/control-plane/ARCHITECTURE.md). |
| Local path | This path processes one dataset on one machine. The command `eca-rsi run <eca-pp output> <root>` starts the kernels as subprocesses. | Not maintained. The CLI still exists. This README documents the semantics that both paths share. |

Both paths use the same kernels, the same round policy, the same
`loop_control.json` manual controls, and the same release layout.

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

The input can also be a single source directory that contains `standardize/`. Source directory names must be unique. The system ignores ECA-PP step-local `.history/` archives. The system also ignores any ECA-RSI run root, which is a directory that holds `organize/manifest.json`. For example, this includes a finished run mirrored next to `standardize/`. Other unexpected H5AD files cause validation to fail. Keep ECA-RSI outputs outside the input tree and the source repository.

The `organize` command checks upstream status and exit codes. It opens each accepted H5AD. It validates cell/gene IDs, dimensions, and finite nonnegative values in the required `layers["counts"]`. Failed or inconsistent results block processing. Rejected sources remain in the source inventory even without an H5AD. Nonblocking `needs_review` results can proceed. The system preserves their reasons. The system saves upstream results and metadata evidence as snapshots for later review.

ECA-PP's `identify_columns/result.json` and derived TSV evidence are optional. RSI aligns that evidence to the original cell IDs. It identifies experiments within each source. Two sources that both use `sample=S1` remain separate OSP inputs. A technical batch column is not automatically an experimental sample column. The system supports explicit sample mappings. See [docs/front-integration.md](docs/front-integration.md) for mapping formats.

A sample map can also declare two cell policies (`ecarsi/policies.py`). The host applies these policies deterministically and never infers them:

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

`exclude_cells` rules drop cells before the host cuts any OSP subset. A `where` rule matches exact strings and applies an AND condition across columns. A `blank` rule checks for a missing-family value in every listed column. An unknown column is an error. A rule that matches no cell produces a recorded warning. Every excluded cell is listed in `persample/excluded_cells.csv` with its reason. Each excluded cell appears in the cell ledger as `removed:persample-policy:<reason>`. The release's `needs_review` lists each rule under "Cells excluded before OSP by policy". The `batch_key` names the obs column that Harmony corrects by instead of the experiment. For plate = mouse x FACS gate designs, this column is the mouse. The host requires this key to be constant within every experiment. The host ignores blank cells, and then fills them with their experiment's value in the OSP subset. The key must take at least two values. `MSP_BATCH_COL` still wins. A value that contradicts the map is an error. Without a map, the sample-column agent may propose exclusion rules. The system validates these rules exactly like user rules and records them as `proposed_by: agent`. A batch-key *recommendation* from the study design goes to `needs_review` only.

## Run the workflow

On the control-plane path, submit a dataset spec with `start-dataset`.
See `examples/dataset-v2.json`.
[INSTALL.md](INSTALL.md#d-submit-a-dataset) provides the command.
The dataset spec contains the same options that the CLI exposes below.
These options include the agent backend, budgets, and a fixed round count or the automatic stopping rule.

The default agent backend uses the OpenAI Agents SDK to drive Doubao through Volcengine Ark, with model `doubao-seed-2-1-turbo-260628`.
Set `ARK_API_KEY` before you run the workflow.
`--harness` accepts `openai` (default), `openai@ark`, `openai@openrouter`, `openai@vllm`, `claude`, and `deepseek`.
Configure endpoints, keys, and models as described in [INSTALL.md](INSTALL.md#c-run-time-configuration).

The local CLI is not maintained; see [Two paths](#two-paths).
The CLI shows the shared options in their shortest form:

```bash
# Automatic stopping for every analysis unit.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study

# CLI values override HARNESS and MODEL environment variables.
eca-rsi --harness openai --model doubao-seed-2-1-turbo-260628 \
  run /path/to/eca-pp-output /path/to/eca-runs/study

# A fixed total of two rounds, retaining intermediate H5ADs.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/study --rounds 2 --no-prune

# Validate the input and per-sample stages only.
eca-rsi run /path/to/eca-pp-output /path/to/eca-runs/new-study --stop-after persample

# Run on fast scratch, keep a browsable copy on long-term storage.
eca-rsi run /path/to/eca-pp-output $SCRATCH/eca-runs/study --mirror $OAK/eca-results/study
```

`python -m ecarsi` is equivalent to `eca-rsi`.
Use `eca-rsi --help` and `eca-rsi run --help` to view the full option list.
For explicit experiment mappings and OSP options, run `organize` and `persample` separately.
Mapping formats are in [docs/front-integration.md](docs/front-integration.md).

### Processing stages

1. **Organize.** Profile upstream files and propose analysis units by using their metadata.
   Then merge or split the analysis units in code.
   A conservation check requires each source cell to belong to exactly one analysis unit.
   Cross-file barcode overlap produces warnings.
   Overlap does not establish expression identity or automatically deduplicate cells.
2. **Per sample, once.** Identify the experimental-run column and run OSP on each sample.
   The driver sizes concurrency from available CPUs and memory.
   The driver enables annotation by default.
   Cross-sample review requires annotation.
3. **First round.** Decide which samples enter integration.
   Then run MSP integration, inspection, and annotation.
   Next, run ZMIP lineage refinement.
   With one included sample, MSP skips Harmony and sample-composition evidence.
   Obs columns can be constant within every sample but differ across samples (e.g. FACS `subtissue`, `mouse.id`).
   The driver passes these columns to the MSP/ZMIP agents as `--design-context`.
   Therefore, agents judge a sample-confined cluster against the study design rather than as a batch artefact.
   Preview the design context with `python -m ecarsi.design <unit>`.
4. **Later rounds.** Take the previous ZMIP survivors.
   Preserve prior labels under `rNN_*` columns.
   Rerun MSP from counts, followed by ZMIP.
   The driver does not repeat OSP or the first-round sample-inclusion decision.
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
Only OSP failures explicitly marked retryable receive the driver's one retry.
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

`--cap` sets the automatic-mode round limit (default 15).
Reaching the limit without convergence produces a forced release with a review flag.
`--rounds N` overrides automatic stopping.
The workflow releases after the specified total round count, including `--rounds 1`.
Check the recorded reason before interpreting a release as converged.

## Read the results

The unit `index.html` file is the RSI report across all rounds. MSP and ZMIP `report.html` files describe individual analysis stages. ZMIP also produces reports for each processed lineage. A standalone MSP/ZMIP run does not produce an RSI final release.

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

`release/final.h5ad` contains surviving cells. The final broad and fine labels are `obs["zmip_ann_coarse"]` and `obs["zmip_ann_fine"]`. Read `summary.md` for round counts and stopping reasons. Read `needs_review.md` for uncertain labels, policy-excluded cells, excluded samples, reassignments, and other review items. MSP requires an explicit review for adjacent coarse-label pairs. The system retains unresolved boundaries and lists them here. ZMIP requires a written explanation when a UMAP island splits across lineages. Neither missing DEGs nor a fixed graph mixing percentage proves that labels should merge. The ledger and stage-specific removal CSV files (`persample/excluded_cells.csv`, OSP `qc_removed.csv`, MSP `annotation_removed.csv`, ZMIP `zmip_removed.csv`) record cell-level history. Cost summaries include only costs that the runtime reports and captures. Missing cost records do not mean that a run was free. These missing records also do not constitute a complete bill.

Report labels and explanatory text default to English. This default does not depend on the language used to discuss or launch the analysis. Other prose languages require an explicit configuration override.

Open a unit's `index.html` directly in a browser to view its saved report. The final UMAP includes its plotting data in the HTML. Zoom, hover, and legend filtering work offline when you enable JavaScript. Point size adapts to the plotted cell count, panel size, and zoom. Keep the run directory together for links to other reports and files. To update older saved pages, run `python -m ecarsi.index /path/to/root-or-unit`. This command does not rerun any analysis.

To browse live progress from an unfinished run or share results over HTTP:

```bash
eca-rsi serve scan-add /path/to/eca-runs/study
eca-rsi serve --port 8899
```

Open `http://127.0.0.1:8899/` on the serving machine. By default, the server reads its dataset registry from `~/.config/ecarsi/registry.json`. The server automatically picks up registry changes. `eca-rsi run ... --serve 8899` starts the server after processing. Optional `--ngrok`, `--domain`, and `--auth USER:PASS` support remote access. See [INSTALL.md](INSTALL.md).

Pass `--mirror DIR` when the run directory lives on fast, purged scratch and the server reads a long-term directory. You can pass `--mirror DIR` to `run`, `organize`, `persample`, or `loop`. The system stores this setting in `<root>/mirror.json`, so resumed steps keep mirroring. After every landing-page update, the system incrementally copies light files from the run root to DIR. These files include pages, `progress.log`, manifests, `stats.txt` / `decision.txt`, markdown, reports, figures, and small tables. At release after cleanup, the system copies the whole root. This copy includes `final.h5ad`, `input/organized.h5ad`, and ledgers. The system deletes files that cleanup removed from DIR's copy of that unit only. Every page footer shows `run state updated <time>` from the newest state file. This timestamp shows a viewer of DIR how fresh the mirror is. The system also labels a served DIR as a mirror copy of its source. Mirroring never reads DIR. Mirroring never fails a step. A failed copy only records a warning in `progress.log`.

## Resume and storage

Repeat the same `eca-rsi run` command after an interruption to reuse validated outputs. Organize and per-sample manifests track input, configuration, and adapter/runtime identities. MSP and ZMIP stages also check their input content, computation settings, runtime sources, and completed output hashes. RSI invokes ZMIP's own resume checks even when lineage outputs already exist. Completed rounds and releases have integrity receipts. You can check pruned historical releases without deleted intermediate matrices.

MSP and ZMIP save accepted agent submissions after each cluster. A restart checks the original data, evidence, and code. The restart then revalidates saved decisions and continues pending clusters. ZMIP preserves refined cluster assignments. ZMIP also reuses the completed integration of an unfinished lineage.

To pause a unit, set `"pause": true` in `<unit>/loop_control.json`. New sample and lineage launches stop. Running work finishes at an MSP step or lineage boundary. The command exits with code 3 and does not publish a release. Set `pause` to false or remove it. Then repeat the command to resume. Alternatively, set `"pause_after_stage": "crosssample"` or `"zoomin"` to pause after that stage. Remove the setting before you resume. `SIGTERM` requests the same cooperative pause. For Slurm batch wrappers, request advance notice with `#SBATCH --signal=B:TERM@600`. Have the shell trap touch the shared `ECA_RSI_PAUSE_FILE` while it waits for the pipeline. A hard kill can still interrupt current unsubmitted work. Already accepted submissions are on disk.

Use a new output root when inputs or analysis code change. Legacy outputs without the required identities or receipts remain browsable. The system does not accept them as verified completion for upgraded computation.

The system records harness/model changes in progress and review records. These changes do not invalidate completed computation. The bridge version and source are provenance. Scientific package versions, source, inputs, and computation settings remain part of the computation identity.

For deliberate development across source changes, `ECA_RSI_DEVELOPER_MODE=1` relaxes RSI's runtime comparison only. Reused per-sample outputs still require their original validated receipts. The system records skipped runtime checks as `runtime_check: skipped`. Input, analysis settings, output integrity, and kernel-level resume checks still apply. Prefer a new root for reproducible runs.

`--force-reopen` continues beyond an existing release. With `--rounds N`, choose a total larger than the completed round count. It does not restore pruned matrices. It also does not forward ZMIP's `--force` option.

**Release normally triggers cleanup of intermediate H5ADs.** Use `--no-prune` on `run` or `loop` to retain them. Cleanup keeps `input/organized.h5ad`, the release, reports, tables, and figures. Removed H5ADs leave `.pruned` markers. Those carrying labels also leave `.obs.parquet` or `.obs.csv.gz` tables for the ledger. `release/pruned.json` records the cleanup. This preserves the decision history, but not every intermediate expression matrix.

```bash
eca-rsi prune /path/to/eca-runs/study --dry-run
```

## Validation

The test suite contains 559 tests. It runs inside the compute image. See [INSTALL.md](INSTALL.md#b3-run-the-tests).

The latest end-to-end regression ran on 2026-10-02. The run used dataset 11_Shietal on the `20261001-3` image pair. The run included two rounds, 9,163 to 4,146 cells, and 12 agent sessions. The run completed with no failures.

Older validation records are in [docs/history/](docs/history/). These records include release checks, pause and recovery, the Clayton and 19Liu runs, and the fixed-task model comparison in [eval/RESULTS.md](eval/RESULTS.md).

These checks establish engineering behavior. They do not establish independently validated biological accuracy.

## Development and history

See [CLAUDE.md](CLAUDE.md) for source layout, conventions, and targeted checks.
See [CHANGELOG.md](CHANGELOG.md) for release changes. The
[architecture diagram](docs/diagrams/architecture.html) shows the main package
flow. [docs/control-plane/](docs/control-plane/ARCHITECTURE.md) documents the
control-plane path.

History, oldest first:

- The project keeps a v0.1 archive (`attic-v01/`) outside the repository. This archive was never in git.
- The six-step prompt loop (`run.sh` and `steps/*.md`) let agents write their own analysis scripts through Explore → Compute → Annotate → QC → Apply → Stop. Branch `primitive` preserves this loop. The path `docs/history/primitive/` also preserves it for reference. Its prompts and timings do not describe `ecarsi`.
- `ecarsi` replaced this loop with deterministic kernels and narrow agent decisions. The local path ran one dataset per machine. The project no longer maintains the local path.
- The control-plane path was formerly the `gen2` branch. The project merged this branch in 0.3.1. The control-plane path added Temporal workflows, the HyperQueue warm pool, and the model-turn service. The project removed the earlier Dask pool (`ecarsi.pool`) in 0.3.2. Since then, the project added HyperQueue native priorities with a feasibility gate, DEG batching, resident model runners, and deployment from two images.

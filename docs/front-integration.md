# Front half: ECA-PP input → organize → persample → OSP

This document describes input rules, sample-map policies, and resume semantics for the front half. The MSP and ZMIP upgrade record is in [history/DOWNSTREAM_INTEGRATION.md](history/DOWNSTREAM_INTEGRATION.md). The validated OSP version is 0.1.7. `pyproject.toml` accepts `osp-sc>=0.1.3,<0.2`.

The commands below apply to the local path. The control-plane path runs the same programs as pool tasks ([control-plane/ORGANIZE_V2.md](control-plane/ORGANIZE_V2.md), [control-plane/PERSAMPLE_V2.md](control-plane/PERSAMPLE_V2.md)). Both paths follow the same rules.
## Input and organize

```bash
eca-rsi organize /path/to/eca-pp-output /path/to/new-run
eca-rsi organize /path/to/eca-pp-output /path/to/new-run --plan-json plan.json   # skip the planning model
```

`plan.json` follows `ecarsi.plan.PLAN_SCHEMA`. Each unit must have exactly one resolved species. Assign every cell of an accepted source exactly once. The main flow validates the model's submission before it writes results.

Input rules:

- Input processing reads Schema 2, including 0.2.x and 0.5.x results on disk. It rejects unknown schemas.
- Input processing accepts `ok/0` and `needs_review/0`. It keeps review reasons for each source. Starting with ECA-PP `0576683`, input processing trusts the expanded `.raw` directly. It no longer generates the HVG counts cross-check or its review reason. Existing `counts_check` records stay unchanged.
- Input processing lists `rejected/2` sources without output in `organize/source_inventory.json`. It excludes these sources.
- `error`, `blocked`, contradictory states, missing files, a missing counts layer or an invalid matrix stop the whole input set.
- Input processing skips only `.history` inside an ECA-PP step directory. Any other undeclared H5AD causes an error.
- `input/upstream/<source>/` keeps the full result JSON, the derived TSV and the full source obs. Input processing aligns the TSV by original cell ID before any renaming. It rejects duplicate, missing or extra IDs.
- `organized.h5ad` keeps the original metadata. It adds `source_unit` and `eca_source_cell_id`. It optionally adds `eca_pp_batch` and `eca_pp_cell_type`. Input processing reserves these column names. An input that already contains them causes an error. Expression lives only in `layers["counts"]`. Integers wider than 4 bytes become int32. `X` is an empty CSR placeholder (`uns["X_placeholder"]`). Input processing does not keep the upstream normalized `X`. `validate_matrix` does not accept `X` as counts. OSP and MSP rebuild `X` from counts.

`organize/manifest.json` first records the plan and the `running` state. It then records each unit's output fingerprint. It records `complete` only when every unit is done. An interrupted run can finish the remaining units if the input and adapter code remain unchanged. Use a new directory if the input, plan or code changes. The driver computes content SHA-256 once per driver entry, not per sample subprocess. Each unit keeps the full source obs to check whether an experiment pool was split by organ.
## Experiment mapping

```bash
eca-rsi persample /path/to/new-run/units/UNIT --sample-column sample      # `sample` is the physical experiment column; equal values group only within one source
eca-rsi persample /path/to/new-run/units/UNIT --single-sample             # one source is one complete experiment
eca-rsi persample /path/to/new-run/units/UNIT --sample-map samples.json --plan-only   # save and check the map, do not run OSP
```

Without a mapping argument, a narrow decision model receives the obs profile for each source. It also receives upstream classification, candidates, nesting, correction and warning evidence for each source. A batch column alone does not identify the experiment column. Neither `batch=null` nor `correction=unnecessary` implies a single experiment. A `null` choice requires `confirmed_single=true` and a reason. Unknown grouping stops the run and triggers a request for an explicit map. There is no hard limit of 200 experiments. Empty strings and common missing placeholders cannot become samples.

An explicit map takes precedence. Include every source of the unit in `sources`. Add an explicit `merges` entry to pool across sources:

```json
{
  "sources": {
    "source-A": {"sample_column": "library", "rationale": "original library id"},
    "source-B": {"sample_column": "eca_pp_batch", "rationale": "TSV values verified to be the original library ids"}
  },
  "merges": [
    {
      "sample_id": "library-7",
      "evidence": "L7 in A and run7 in B are the same GEM well split into two cell files",
      "members": [
        {"source": "source-A", "value": "L7"},
        {"source": "source-B", "value": "run7"}
      ]
    }
  ]
}
```

A merged experiment id may contain letters, digits, dots, underscores and hyphens. A source group can take part in only one merge. The run rejects duplicate original cell ids within one experiment. Without `merges`, `S1` in two sources remains two experiments. The run writes the full map to `persample/sample_mapping.csv.gz`. The map includes the current cell id, original cell id, source, original group value and `eca_sample_id`. Only the OSP subset receives `eca_sample_id`. The original `sample` column remains unchanged.

If the full source obs shows that an experiment has cells in another tissue unit, the run refuses QC on the partial pool. eca-rsi does not implement cross-organ experiment-level QC.
## Cell policies: `exclude_cells` and `batch_key`

The map file has two more top-level keys. Both keys are declarative. The host applies them deterministically through `ecarsi/policies.py`. The host accounts for every cell. Unknown top-level keys cause errors.

Tabula Muris FACS example:

```json
{
  "sources": {"Lung": {"sample_column": "plate.barcode", "rationale": "Smart-seq2 plate = library"}},
  "exclude_cells": [
    {"blank": ["mouse.id", "subtissue", "cell_ontology_class"],
     "reason": "upstream_qc_blank",
     "rationale": "wells the authors dropped in QC keep only empty metadata (eca-pp rewrites it as 'missing'); 91 % are removed again downstream"}
  ],
  "batch_key": "mouse.id"
}
```

`exclude_cells` is a list of rules. The host applies these rules in order after organize and before it creates any OSP subset. The host assigns each excluded cell to the first matching rule.

- `{"where": {"<column>": ["value", ...]}, "reason", "rationale"}`: Match exact strings after trimming. Combine several columns with AND. The literal `"missing"` matches `"missing"`.
- `{"blank": ["col1", "col2", ...], "reason", "rationale"}`: Every listed column must contain a value from the missing family. This family contains empty, NA, nan, none, null, and missing, as in `upstream.normalize`.
- `reason` is a slug that uses `[a-z0-9_-]`. Limit it to 40 characters. Keep it unique in the list. Provide a non-empty `rationale`. An unknown column causes an error. A rule that matches zero cells generates a warning because one map may serve several organs. The host writes this warning to the manifest and needs_review. A source that becomes empty causes an error.
- Excluded cells remain in `persample/sample_mapping.csv.gz`. The host sets their `excluded_reason` field. Their `eca_sample_id` field stays empty. The host also lists excluded cells in `persample/excluded_cells.csv` with cell, source_unit, source_cell_id, reason, and proposed_by. The ledger records them as `osp_status = removed:persample-policy:<reason>`. Conservation requires OSP survivors + OSP QC removals + policy removals = organized input. The release's needs_review lists excluded cells per rule under `policy_excluded`.
- Rules form part of the mapping identity. The `excluded_reason` column changes `mapping_identity`. The map file itself enters `explicit_mapping`. Use a new output directory when you change a rule. The host checks the sample column for NA values after exclusion.

`batch_key` names the Harmony correction column. The default is `eca_sample_id`. The host checks that this column exists in the obs of `organized.h5ad`. The host checks that the column is constant within every OSP experiment. It ignores missing values during this check. It fills missing values per experiment. Two non-NA values in one experiment cause an error. An all-NA experiment causes an error. The host also checks that the column has at least two values in the unit.

The host writes the per-sample constant into the OSP subset. It also records this constant in `sample_mapping.batch_key` (`column`, `of_sample`, `n_filled`). Cross-sample passes the correction column to MSP as `--batch-col`. The round manifest records `integration_policy.selection = "sample_map"`. An explicit `MSP_BATCH_COL` takes precedence (`explicit`). A conflict with the map causes an error.

Without a map file, the sample-column agent may attach an `exclude_cells` proposal with the same structure. The host checks the proposal against the source obs. The column must exist. At least one cell must match. The proposal must exclude at most half of the source: `policies.AGENT_EXCLUDE_MAX_FRAC`. If a check fails, the host requests a resubmission. The host applies valid proposals with `proposed_by: "agent"`.

The study design (`ecarsi.design`) contains columns that are constant per sample. If no `batch_key` declaration exists and the study design has two or more columns, the host makes one small agent call. This call **recommends** a `batch_key`. The host records the recommendation in `persample/needs_review` and the manifest's `batch_key_recommendation`. The host never applies the recommendation. A failure of this call does not affect per-sample.
## OSP configuration and status

```bash
eca-rsi persample /path/to/new-run/units/UNIT --sample-column sample --resolution 0.8 --language Chinese --effort high
eca-rsi persample /path/to/another-run/units/UNIT --sample-column sample --no-scrublet --no-decontx --no-annotate   # debugging; QC and annotation are on by default
```

Single and multiple experiments call the OSP public Python API through the `ecarsi.osp_worker` subprocess. Both pass Scrublet, DecontX, resolution, species, tissue, language, effort and model explicitly. The default resolution is 1.0. `OSP_PYTHON` selects the kernel interpreter. ECA-RSI does not copy OSP's QC, clustering or annotation code. ECA-RSI does not change the shared bridge's budgets or retries.

Each sample's `request.json` and `run_state.json` record the input and configuration identity, interpreter and package versions. They also record the source commit and content digest, attempt, stage, exit code, failure class, validation results and output fingerprints. The driver and each sample directory hold a process lock. A sample is complete when the run succeeds with exit 0 and passes these checks:

- The sample has a readable `clustered.h5ad`. It has a valid HTML report. It has a QC summary and `qc_removed.csv`. A proposal exists when annotation is on.
- Input cells = survivors ∪ removed cells. Survivors and removed cells are disjoint. Neither set contains duplicates or foreign ids. The summary counts match.
- The proposal's `cluster_key` exists. The proposal's cluster coverage, coarse and fine labels and QC actions agree with the H5AD.

ECA-RSI does not recompute deterministic errors. ECA-RSI retries explicit transient connection or timeout errors once. Unclassified errors stay failed. ECA-RSI does not use text to guess retryability. ECA-RSI records zero QC survivors and fewer than three cells separately. Both conditions leave the unit incomplete. In both cases, ECA-RSI creates no placeholder H5AD and does not silently exclude the unit. An annotation failure keeps the validated compute snapshot. A resume with the same identity runs only the annotation. Success deletes the subset and the snapshot.

A resume with the same configuration validates and skips successful samples. If the input, map, compute parameter, model, interpreter or source changes, use a new output directory. You can browse old outputs and `.pruned` markers. They do not provide evidence of success. You can move the directory. Old absolute paths in result files provide provenance only.

Upstream review and warnings go to `persample/needs_review.{json,md}`. OSP degradation and failure messages also go to that location. These items appear on the unit page and in the release review.
## Checks

```bash
LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_front_integration.py tests/test_osp_worker.py tests/test_agent_selection.py
```

These tests do not need MSP or ZMIP. [history/FRONT_VALIDATION.md](history/FRONT_VALIDATION.md) contains the September 2026 validation record. [history/FRONT_COMPATIBILITY.json](history/FRONT_COMPATIBILITY.json) lists the source revisions tested during that validation.
## Bridge compatibility layer

`ecarsi.harness` provides a compatibility layer for old import paths. It preserves the object identity of the 14 public interfaces and the old constants from before the split. New code imports from `harness_bridge` directly. The tests pin the old interface set. They allow additions. Removed interfaces or mismatched objects cause test failures.

The CLI calls `configure_logging("ecarsi", stream=sys.stderr)` at entry. The OSP worker configures logging for `ecarsi`, `osp` and the bridge. Library functions do not reconfigure logging. The bridge's `ensure_logging` respects existing handlers. The source dependency is `agent-harness-bridge[all]>=0.2.14,<0.3`.

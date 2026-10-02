# Front half: ECA-PP input → organize → persample → OSP

This document states the input rules, the sample-map policies and the resume semantics of the front half. The MSP and ZMIP upgrade record is in [history/DOWNSTREAM_INTEGRATION.md](history/DOWNSTREAM_INTEGRATION.md). The validated OSP version is 0.1.7; `pyproject.toml` accepts `osp-sc>=0.1.3,<0.2`.

The commands below are the local path's. The control-plane path runs the same programs as pool tasks ([control-plane/ORGANIZE_V2.md](control-plane/ORGANIZE_V2.md), [control-plane/PERSAMPLE_V2.md](control-plane/PERSAMPLE_V2.md)); the rules are identical.

## Input and organize

```bash
eca-rsi organize /path/to/eca-pp-output /path/to/new-run
eca-rsi organize /path/to/eca-pp-output /path/to/new-run --plan-json plan.json   # skip the planning model
```

`plan.json` follows `ecarsi.plan.PLAN_SCHEMA`. Each unit must have exactly one resolved species. Every cell of an accepted source must be assigned exactly once. The main flow validates the model's submission before it writes results.

Input rules:

- Schema 2 is read, including 0.2.x and 0.5.x results on disk. Unknown schemas are rejected.
- `ok/0` and `needs_review/0` are accepted. Review reasons are kept per source. From ECA-PP `0576683` the expanded `.raw` is trusted directly; the HVG counts cross-check and its review reason are no longer generated. Existing `counts_check` records stay as they are.
- `rejected/2` without output is listed in `organize/source_inventory.json` and excluded.
- `error`, `blocked`, contradictory states, missing files, a missing counts layer or an invalid matrix stop the whole input set.
- Only `.history` inside an ECA-PP step directory is skipped. Any other undeclared H5AD is an error.
- `input/upstream/<source>/` keeps the full result JSON, the derived TSV and the full source obs. The TSV is aligned by original cell id before any renaming; duplicate, missing or extra ids are rejected.
- `organized.h5ad` keeps the original metadata and adds `source_unit`, `eca_source_cell_id`, and optionally `eca_pp_batch` and `eca_pp_cell_type`. These column names are reserved; an input that already has them is an error. Expression lives only in `layers["counts"]` (integers wider than 4 bytes become int32). `X` is an empty CSR placeholder (`uns["X_placeholder"]`). The upstream normalized `X` is not kept: `validate_matrix` does not accept `X` as counts, and OSP and MSP rebuild `X` from counts.

`organize/manifest.json` first records the plan and the `running` state, then each unit's output fingerprint, and `complete` only when every unit is done. An interrupted run with the same input and adapter code can finish the remaining units. A changed input, plan or code needs a new directory. Content SHA-256 is computed once per driver entry, not per sample subprocess. Each unit keeps the full source obs to check whether an experiment pool was split by organ.

## Experiment mapping

```bash
eca-rsi persample /path/to/new-run/units/UNIT --sample-column sample      # `sample` is the physical experiment column; equal values group only within one source
eca-rsi persample /path/to/new-run/units/UNIT --single-sample             # one source is one complete experiment
eca-rsi persample /path/to/new-run/units/UNIT --sample-map samples.json --plan-only   # save and check the map, do not run OSP
```

Without a mapping argument, a narrow decision model receives, per source, the obs profile and the upstream classification, candidates, nesting, correction and warning evidence. A batch column is not taken as the experiment column by itself. `batch=null` or `correction=unnecessary` does not imply a single experiment. A `null` choice needs `confirmed_single=true` with a reason. Unknown grouping stops the run and asks for an explicit map. There is no hard limit of 200 experiments. Empty strings and common missing placeholders cannot become samples.

An explicit map wins. `sources` must cover every source of the unit. Pooling across sources needs an explicit `merges` entry:

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

A merged experiment id may use letters, digits, dots, underscores and hyphens. A source group can take part in one merge only. Duplicate original cell ids inside one experiment are rejected. Without `merges`, `S1` in two sources stays two experiments. The full map is written to `persample/sample_mapping.csv.gz` with current cell id, original id, source, original group value and `eca_sample_id`. Only the OSP subset gets `eca_sample_id`; the original `sample` column is untouched.

If the full source obs shows that an experiment has cells in another tissue unit, QC on the partial pool is refused. Cross-organ experiment-level QC is not implemented.

## Cell policies: `exclude_cells` and `batch_key`

The map file has two more top-level keys. Both are declarative: the host applies them deterministically and accounts for every cell (`ecarsi/policies.py`; unknown top-level keys are errors). Tabula Muris FACS example:

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

`exclude_cells` is a list of rules. They run after organize and before any OSP subset is cut, in order. A cell is charged to the first rule that matches it.

- `{"where": {"<column>": ["value", ...]}, "reason", "rationale"}`: exact string match after trimming; several columns are combined with AND; the literal `"missing"` matches `"missing"`.
- `{"blank": ["col1", "col2", ...], "reason", "rationale"}`: every listed column is in the missing family (empty, NA, nan, none, null, missing; same as `upstream.normalize`).
- `reason` is a slug (`[a-z0-9_-]`, at most 40 characters, unique in the list). `rationale` is non-empty. An unknown column is an error. A rule that matches zero cells is a warning, written to the manifest and needs_review, because one map may serve several organs. A source that ends up empty is an error.
- Excluded cells stay in `persample/sample_mapping.csv.gz` (`excluded_reason` set, `eca_sample_id` empty) and are listed in `persample/excluded_cells.csv` (cell, source_unit, source_cell_id, reason, proposed_by). The ledger records them as `osp_status = removed:persample-policy:<reason>`. Conservation requires OSP survivors + OSP QC removals + policy removals = organized input. The release's needs_review lists them per rule under `policy_excluded`.
- Rules are part of the mapping identity: the `excluded_reason` column changes `mapping_identity`, and the map file itself enters `explicit_mapping`. A changed rule needs a new output directory. The sample column's NA check runs after exclusion.

`batch_key` names the Harmony correction column (default `eca_sample_id`). The host checks that it is an obs column of `organized.h5ad`, constant within every OSP experiment (missing values are ignored and filled per experiment; two non-NA values in one experiment is an error; an all-NA experiment is an error), and that it has at least two values in the unit. The per-sample constant is written into the OSP subset and recorded in `sample_mapping.batch_key` (`column`, `of_sample`, `n_filled`). Cross-sample passes it to MSP as `--batch-col`; the round manifest records `integration_policy.selection = "sample_map"`. An explicit `MSP_BATCH_COL` still wins (`explicit`); a conflict with the map is an error.

Without a map file, the sample-column agent may attach an `exclude_cells` proposal of the same shape. The host checks it against the source obs (column exists, at least one cell matches, at most half of the source: `policies.AGENT_EXCLUDE_MAX_FRAC`), asks for a resubmission if it fails, and applies it with `proposed_by: "agent"`. If no `batch_key` is declared and the study design (`ecarsi.design`: columns constant per sample) has two or more columns, the host makes one small agent call that **recommends** a `batch_key`. The recommendation goes to `persample/needs_review` and the manifest's `batch_key_recommendation`; it is never applied. A failure of that call does not affect per-sample.

## OSP configuration and status

```bash
eca-rsi persample /path/to/new-run/units/UNIT --sample-column sample --resolution 0.8 --language Chinese --effort high
eca-rsi persample /path/to/another-run/units/UNIT --sample-column sample --no-scrublet --no-decontx --no-annotate   # debugging; QC and annotation are on by default
```

Single and multiple experiments both call the OSP public Python API through the `ecarsi.osp_worker` subprocess, with Scrublet, DecontX, resolution, species, tissue, language, effort and model passed explicitly. The default resolution is 1.0. `OSP_PYTHON` selects the kernel interpreter. ECA-RSI does not copy OSP's QC, clustering or annotation code, and does not change the shared bridge's budgets or retries.

Each sample's `request.json` and `run_state.json` record the input and configuration identity, the interpreter, package versions, source commit and content digest, attempt, stage, exit code, failure class, validation results and output fingerprints. The driver and each sample directory hold a process lock. A sample is complete when the run succeeded with exit 0 and these checks pass:

- a readable `clustered.h5ad`, a valid HTML report, a QC summary and `qc_removed.csv`; a proposal when annotation is on;
- input cells = survivors ∪ removed cells, disjoint, with no duplicates or foreign ids, and matching summary counts;
- the proposal's `cluster_key` exists, and its cluster coverage, coarse and fine labels and QC actions agree with the H5AD.

Deterministic errors are not recomputed. Explicit transient connection or timeout errors are retried once. Unclassified errors stay failed; text is not used to guess retryability. Zero QC survivors and fewer than three cells are recorded separately; both leave the unit incomplete, with no placeholder H5AD and no silent exclusion. An annotation failure keeps the validated compute snapshot; a resume with the same identity runs only the annotation. Success deletes the subset and the snapshot.

A resume with the same configuration validates and skips successful samples. A changed input, map, compute parameter, model, interpreter or source needs a new output directory. Old outputs and `.pruned` markers can be browsed but are not evidence of success. The directory can be moved; old absolute paths in result files are provenance only.

Upstream review and warnings, OSP degradation and failure messages go to `persample/needs_review.{json,md}` and appear on the unit page and in the release review.

## Checks

```bash
LC_ALL=C LANG=C PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_front_integration.py tests/test_osp_worker.py tests/test_agent_selection.py
```

These tests do not need MSP or ZMIP. The September 2026 validation record is in [history/FRONT_VALIDATION.md](history/FRONT_VALIDATION.md); the source revisions tested then are in [history/FRONT_COMPATIBILITY.json](history/FRONT_COMPATIBILITY.json).

## Bridge compatibility layer

`ecarsi.harness` keeps the object identity of the 14 public interfaces and the old constants from before the split, as a compatibility layer for old import paths. New code imports from `harness_bridge` directly. The tests pin the old interface set and allow additions; a removed interface or a mismatched object fails.

The CLI calls `configure_logging("ecarsi", stream=sys.stderr)` at entry. The OSP worker configures `ecarsi`, `osp` and the bridge's logging. Library functions do not reconfigure logging; the bridge's `ensure_logging` respects existing handlers. The source dependency is `agent-harness-bridge[all]>=0.2.14,<0.3`.

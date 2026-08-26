# Step: explore — probe the state, plan the round

Decide what this round should work on. You plan; you do not label, clean, or
modify anything.

## Round 1 (no checkpoint yet): survey the input folder from scratch

**First, look for upstream provenance.** The inputs may have been prepared by
**eca-pp / ecasteps** (github.com/chansigit/eca-pp), which leaves its results
next to its outputs — typically a `result.json` beside a `standardized.h5ad`,
often one output folder per sample, sometimes a `batch.tsv`. The layout is
not fixed: search near the input files and read what you find. If present:

- `result.json` from **ecasteps-standardize** settles questions you would
  otherwise probe for: `status` (`ok` — usable; `rejected` — exclude the
  sample, record its reason; `needs_review` — usable but carry the concern
  forward as a flag), `species` (resolved, with source and confidence),
  `metrics.counts_source`, and gene-harmonization statistics. A
  `standardized.h5ad` with status ok guarantees: `layers["counts"]` = integer
  raw counts, `X` = log1p(normalize_total(counts, 1e4)), `var_names` =
  canonical gene symbols (originals in `var["original_feature_name"]`), and
  authoritative QC columns in obs (`pct_counts_mt`, `pct_counts_hb`,
  `total_counts`, `n_genes_by_counts`). Obs columns ending `__original` are
  superseded copies the upstream renamed aside — never treat them as
  independent metadata.
- `result.json` from **ecasteps-identify-columns** carries a verified verdict
  under `columns`: the **batch column** (an obs column name, or a `batch.tsv`
  of derived per-cell values to merge into obs, or null = no batch structure)
  with whether correction is even needed (`correction: recommended` vs
  `unnecessary`), and the **cell-type column** if one exists — use it as
  prior labels (evidence, not truth). These verdicts were validated by
  integration trials; take them as your sample/batch key and prior-label
  source instead of guessing from column names.

Cite these files as evidence like any probe output. Trust them for what they
assert, and spend your own probing on what they do not cover — how *multiple*
standardized files relate to each other (below) is exactly the question the
upstream, which sees one file at a time, structurally cannot answer.

**Then probe what provenance does not settle** — and everything, if there is
no provenance at all. The input files are not self-describing — assume
nothing about how they relate. Probe (with your own short scripts, saved to
the round dir):

- Each file: shape, obs columns + example values, whether X is raw counts or
  normalized, species, gene-name style.
- Between files: shared genes and **shared barcodes**. Shared barcodes with
  identical expression = the same physical cells in two files (e.g. one file
  is a compartment extracted from another) — merging those would double-count
  cells, so one of them must be excluded. Shared barcode *strings* with
  different expression are just independent runs reusing names.
- Decide which files form the working dataset (and which are excluded, with
  the reason), which obs column is the sample/batch key, which columns are
  biology to preserve (condition, tissue...). If the folder spans strata that
  must not be co-embedded (organs, species), plan per-stratum work.

## Round ≥ 2: read the record, then plan

- Read the previous round's reports (`explore/compute/annotate/qc/apply/stop.md`)
  and its open items. Verify the checkpoint's current cell count matches what
  apply reported; a mismatch means something edited the data outside the loop
  — report it and plan nothing else.
- Carry unresolved items forward; drop what got resolved; add what the
  previous round's findings make urgent.

## Report (plan.md is your report — write it as `explore.md`)

- What you found (round 1: the file map and merge/exclude verdicts with
  evidence; later: what changed last round).
- This round's scope: whole dataset, or a named subset (say exactly which
  cells, by which obs column and values).
- 3–6 concrete goals, each naming its target. Fewer, sharper goals beat a
  list of everything.

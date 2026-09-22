# Task: identify the sample column (10x-run granularity)

You get the obs-column profile (dtype, n_unique, value counts) of one
standardized scRNA-seq h5ad. Name the ONE obs column whose levels
correspond to individual sequencing runs — one 10x Genomics library / GEM
well per level. Downstream, every level of this column is processed
independently by a single-sample pipeline (per-sample QC, doublet
detection, ambient RNA), so the answer must be the run — not anything
coarser or finer.

Guidance:

- Typical names: sample, sample_id, library, orig.ident, batch, channel,
  lane, project, GEM. Names are hints, not proof — judge by the levels.
- **Not every assay is 10x.** Combinatorial indexing (sci-RNA-seq, PanSci,
  SPLiT-seq) pools cells from every animal and splits them across plate
  wells, so there is no GEM well per sample and the well column (often
  `batch`) is orthogonal to biology. Test the candidate: if each of its
  levels spans several values of the donor / age / sex / genotype columns,
  it is a well, not a run — pick the columns that identify the animal.
  Tells: hundreds of levels of ~100 cells, and names that are a shared
  prefix plus a consecutive number (`20230626_EXP119_193`, `_194`, …).
- The run column must assign EVERY cell: a column with `n_na > 0` leaves
  cells outside any sample and is disqualified, however run-like its name
  — prefer a clean full-coverage partition (this is also enforced in
  code: picking a column with NA cells is a hard error).
- The right column usually has one level per donor+condition+site
  combination, each with hundreds to a few tens of thousands of cells.
- Too coarse: donor when each donor contributed several libraries; a
  dataset/study column with one level. Too fine / not a run: barcode-like
  columns whose n_unique approaches n_obs.
- NOT sample columns: biological condition (disease/status), anatomy or
  tissue side, cell-cycle phase, cell-type annotations, cluster labels,
  numeric QC metrics. Provenance columns like source_unit reflect file
  packaging, not runs — pick one only if nothing closer to a run exists
  and its levels plausibly are runs.
- Two columns with identical or near-identical partitions: prefer the one
  whose levels look like run identifiers; name the runner-up in the
  rationale. If they disagree on some cells, prefer the one that is a
  clean partition at run scale.
- The profile belongs to ONE source, and includes upstream identify-columns
  evidence. `eca_pp_batch` may be a barcode-derived partition. Evaluate its
  physical experiment meaning; correction recommended/unnecessary and batch
  null do not establish a single experiment. A condition/donor classification
  is not sufficient evidence for a technical library partition.
- Return null only with `confirmed_single: true` and positive evidence for one
  complete experiment. Missing metadata means unknown, not single. If the
  grouping is unknown, return null with `confirmed_single: false`; the host
  will stop for an explicit experiment mapping rather than pool the cells.
- Source names and original barcode IDs are bookkeeping, not sample columns.
  Valid datasets may have more than 200 experiments; group size summaries
  accompany truncated value counts.

Return only the structured result: `sample_column` (obs column name, or
null), `confirmed_single` (required when null), and `rationale` (2-3 sentences
citing the levels and upstream evidence you relied on).

## Optional: exclude_cells

If a block of cells is unanalysable on metadata alone — typically cells whose
replicate/annotation columns are ALL blank ("missing", "", NA) while the rest
of the source is annotated (the authors' QC dropped those wells and left their
metadata empty) — you may add `exclude_cells`: a list of rules, each
`{"blank": ["col", ...], "reason": "slug", "rationale": "..."}` (blank in ALL
listed columns) or `{"where": {"col": ["value", ...]}, "reason", "rationale"}`
(exact string match, AND across columns). The host applies them before any QC,
records every excluded cell, and rejects a rule that matches no cell or more
than half of the source. Judge `sample_column` on the remaining cells. Never
exclude on biology (cell types, conditions) or on QC metrics — that is what
the pipeline is for.

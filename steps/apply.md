# Step: apply — execute this round's decisions on the checkpoint

Read this round's `annotate.md` and `qc.md` and carry out **every** decision
in them. This is the step where records and reality must meet: a decision
that is written down but not executed leaves an audit trail claiming
something that never happened — the worst failure mode this loop has. If a
decision cannot be executed as written (ambiguous target, missing column),
do NOT improvise a version of it: list it under "not executed" with the
reason, so the stop step counts it as unresolved.

What executing means:

- Labels → write `label_l1`, `label_l2`, `cl_id` for the named clusters, and
  mirror them into `roundNN_label_l1` / `roundNN_label_l2` so the label
  history stays on the checkpoint and cross-round drift is checkable.
- Cell/cluster-level flags → boolean obs columns (`flag_<name>`); clear the
  ones qc resolved. Dataset-level concerns (e.g. the removal budget) are not
  obs columns — they live in the reports and must reach the release summary.
- Removals → actually drop the cells from `checkpoint.h5ad`, and append one
  tab-separated line per cell to `removed_cells.tsv` **in the workspace root**
  (one cumulative file, not per-round): barcode, round, cluster at removal,
  reason — so the history stays reconstructable.
- Merges → relabel the source cluster with the target's labels.
- Splits/postpones → nothing to execute; they carry forward as open items.

Never write `checkpoint.h5ad` in place: write `checkpoint.tmp.h5ad`, then
rename it over the old file — the checkpoint is the loop's only state, and a
crash halfway through an in-place write destroys it; a rename cannot.

Save the script that does all of this into the round dir, run it, and verify
before writing your report: reload the checkpoint and confirm the new cell
count and label counts match what you just did, and confirm the lines you
appended to `removed_cells.tsv` are actually there and equal the number of
cells dropped. Never report a file as written without checking it exists —
that exact failure has happened.

## Report (`apply.md`)

Counts before → after (cells, labelled cells, per-action tallies), the
removed-barcode total appended this round, and the "not executed" list
(ideally empty, with reasons when not).

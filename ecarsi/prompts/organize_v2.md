# Experiment mapping required by Organize

In addition to the analysis-unit plan, return `sample_mapping`: an object keyed
by **every source profile name**. Each value is an experiment decision with
`sample_column`, `rationale`, and `confirmed_single` when appropriate.

Choose a metadata column that identifies the original complete experimental
sample or library for that source. A shared donor alone does not make multiple
libraries or sample IDs one QC unit. The value must be an existing obs
column, not a cell type, batch-processing artifact, barcode, or guessed sample.
If evidence proves one complete experiment, set `sample_column` to null and
`confirmed_single` to true with concrete evidence in `rationale`. If provenance
is unclear, explain uncertainty in `notes`; do not claim a single experiment.
If `sample_id`, `library_plate`, or another explicit sample/library column has
multiple values, select a defensible partition rather than `confirmed_single`.
The host will reject an unconfirmed mapping or one that splits any experiment
between analysis units. Each source must be covered exactly once.

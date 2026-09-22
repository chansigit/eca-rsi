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

Not every assay is 10x. Combinatorial indexing (sci-RNA-seq, PanSci, SPLiT-seq)
pools cells from every animal and then distributes them across plate wells, so
the well or lane column — frequently named `batch` — is a processing artifact
that carries no biology at all. **Test any candidate before choosing it: if its
levels each span several values of the donor / age / sex / genotype columns, it
is a well, not a sample.** A real sample column partitions those covariates; a
well column is orthogonal to them. Two more tells: hundreds of levels holding a
hundred cells each, and level names that are a shared prefix plus a consecutive
number (`20230626_EXP119_193`, `_194`, `_195`). Treating wells as samples runs
the single-sample pipeline on a random slice of a pooled library, where QC
thresholds and doublet detection are population statistics computed on nothing.

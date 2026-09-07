# Task: recommend a Harmony batch key (advisory only)

Below is the study design of one analysis unit: obs columns that are constant
within every experiment (sample) and differ across samples. By default Harmony
corrects by sample. When a sample is a biological replicate crossed with a
sorting gate or fraction (e.g. one plate = one mouse x one FACS gate), the
replicate id is the right correction unit: correcting by sample would treat
gate-specific biology as batch.

Recommend `batch_key` = the replicate-id column when the table shows such a
column (mouse/donor/animal/individual id) next to a gate-like column
(subtissue, sort, fraction, gate, compartment). Otherwise return null.

This is a recommendation: the host records it in needs_review and applies
nothing without an explicit sample-map `batch_key` declaration. Return
`batch_key` (a column name from the table, or null) and `rationale` (1-2
sentences naming the columns you relied on).

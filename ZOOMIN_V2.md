# Zoom-in on Temporal and Warm Pool

The v2 workflow reuses ZMIP's lineage planning, marker scoring and merge checks, and MSP's numerical kernels. Model requests go through Agent Bridge; registered tools and numerical work execute on Pool workers. It is implemented on the feature branch and undergoing real-data acceptance.

The sequence is planning evidence → lineage plan → shared lineage markers and bounded subset preparation → independent lineage computation/DEG/annotation → validated global merge. Numerical admission is released before each lineage's model session, so another lineage can compute while the first waits. Every operation uses explicit resources and immutable input references; successful receipts survive Coordinator restarts.

Lineage computation resets from counts and runs normalization, HVG, PCA, Harmony, neighbors, Leiden, UMAP and QC together. `compute_backend` selects `cpu`, `rapids`, or `auto`; auto requests a preferred GPU once `gpu_min_cells` is reached, subject to `gpu_memory_mb` and the actual Pool grant. PCA/Harmony/neighbors/UMAP have RAPIDS implementations; Leiden, foreign scoring and the remaining CPU routines still use CPU. GPU execution must be verified from the grant and runtime record, not inferred from the node name.

Fine identity uses Leiden 1.0; quality uses Leiden 2.0. The host records exact cell intersections and does not assume nested partitions. One agent per lineage saves type proposals before quality, queries the lineage's immutable SQLite DEG, and submits complete quality intersections. Confirmed dissociation/dying subclusters default to removal; a soft-budget second review checks the same policy and scope. Reassignment retains cells and changes labels without re-embedding them in the destination lineage during this pass.

A requested subcluster refinement runs as one budgeted tool operation. It generates a new evidence version and matching DEG, retaining the same agent session. Quality refinement preserves accepted types; type refinement invalidates affected types and merge partners. Normal lineage DEG comparisons are independent Pool tasks. Small targeted refinement comparisons stay together in their tool operation.

Each lineage produces `annotated.h5ad`, `annotation_removed.csv`, `annotation_reassigned.csv` and `cell_exclusions.csv.gz`. Global merge checks every planned lineage and exact input/survivor/exclusion coverage, retains skipped lineages, and produces `annotated_zmip.h5ad` with a publication manifest. Every exclusion carries original cell identity, the two cluster IDs, reasons and evidence; reassignment is not counted as exclusion.

## Commands

Use the configured control environment:

```bash
python -m ecarsi.work_coordinator --temporal HOST:7233 start-zoomin spec.json
python -m ecarsi.work_coordinator --temporal HOST:7233 status-zoomin RUN_ID
python -m ecarsi.work_coordinator --temporal HOST:7233 resume-zoomin RUN_ID
```

The spec requires `run_id`, `dataset_id`, an SHA-256 `input` reference to the completed cross-sample publication, a fresh absolute `output_root`, `pool_root`, `bridge_root`, `max_in_flight_lineages`, and `max_in_flight_deg`. Set `prepare_budget`, `subset_budget`, `compute_budget`, `deg_budget`, `tool_budget`, and `merge_budget`, each with positive integer `cpus`, `memory_mb`, and `timeout_seconds`.

`config` requires `batch_col`, `species`, `tissue`, `min_cells`, `n_top_genes`, `n_pcs`, `n_neighbors`, `compute_backend`, `gpu_min_cells`, `gpu_memory_mb`, and `max_refinements`. Species and sample key must match the accepted cross-sample run. Use the existing ZMIP minimum size of 800 unless the experiment explicitly calls for a different value; small acceptance subsets may use a documented lower limit.

Resume accepts only a failed workflow whose saved spec and input are unchanged and whose external outcomes have been reconciled. It reuses accepted Pool/Bridge results. Unknown executions are not blindly repeated. Process restart recovery is separate from Temporal Service storage survival after node expiry; persistent service relocation and large-data resource calibration remain separate production gates.

## GPU execution acceptance — 2026-09-15

Real Prostate lineages ran through the Pool on `sh03-15n05`, using the allocated RTX 3090 and the unified `rsi-science-20260915-5.sif` image. Epithelial (348 cells) completed its RAPIDS numerical unit in 24.70 seconds; Immune (141 cells) completed in 21.39 seconds. Both receipts record `compute_backend=rapids`, the allocated GPU UUID and 345 MiB peak GPU memory. Their 39 independent DEG comparisons also completed. These small inputs verify execution, not large-data speedup.

Uterus exercises the CPU route while Prostate agents wait: three lineage numerical units completed, with the first two running concurrently. Both workflows are continuing through model annotation and final merge; GPU numerical success alone does not establish full zoom-in acceptance. The small acceptance runs explicitly use `min_cells=50` instead of changing the production default.

Records are under `crosssample-migration-20260915-015647` in the development run directory. Workflow IDs: `zoom-in/zoom-prostate-20260915-0340` and `zoom-in/zoom-uterus-20260915-0340`. The preceding stage's completed checks are recorded in [cross-sample acceptance](CROSSSAMPLE_V2_ACCEPTANCE.md).

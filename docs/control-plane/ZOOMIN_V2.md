# Zoom-in stage

The zoom-in workflow reuses ZMIP's lineage planning, marker scoring and merge checks, and MSP's numerical kernels. Model turns go through the model-turn service. Tools and numerical work run on pool workers.

## Steps

1. `prepare`: lineage evidence, including the connectivity islands of the UMAP.
2. Lineage plan: an agent session chooses the lineages to zoom into. The default minimum is 800 cells. The host checks island connectivity: a plan that merges separate islands into one lineage is rejected; a plan that splits one island needs `confirm_shared_islands: true`.
3. `markers`: one shared marker table for all lineages.
4. Per lineage, with at most `max_in_flight_lineages` computing at once:
   1. `subset`;
   2. `compute`: reset from counts, then normalization, HVG, PCA, Harmony, neighbors, Leiden at 1.0 and 2.0, UMAP and QC;
   3. DEG, eight comparisons per request (`deg-batch`), bounded by `max_in_flight_deg`;
   4. `assemble`;
   5. lineage annotation: one agent session submits types (`submit_types`, Leiden 1.0) and then quality (`submit_quality`, Leiden 2.0). Numerical admission is released before the session starts, so another lineage can compute while this one waits;
   6. `apply`: removals and reassignments for the lineage, plus its report.
5. `merge`: merge every lineage back, check exact input, survivor and exclusion coverage, keep skipped lineages, and publish `annotated_zmip.h5ad` with a manifest.

Fine identity uses Leiden 1.0; quality uses Leiden 2.0. The host records exact cell intersections and does not assume nested partitions. Confirmed dissociation or dying subclusters default to removal. Reassignment keeps the cells and changes their labels; it does not re-embed them in the destination lineage. A requested subcluster refinement runs as one budgeted tool operation and produces a new evidence version with matching DEG in the same session.

`compute_backend` selects `cpu`, `rapids` or `auto`. `auto` asks for a preferred GPU once `gpu_min_cells` is reached, subject to `gpu_memory_mb` and the actual grant. PCA, Harmony, neighbors and UMAP have RAPIDS implementations; Leiden, foreign scoring and the other CPU routines stay on CPU. Check the grant and the runtime record to confirm GPU use. Do not infer it from the node name.

A lineage whose session dies twice is skipped and keeps its cross-sample labels. The stage fails when skipped lineages hold more than 10 % of the round's cells. The workflow continues as new past 5,000 history events, carrying finished lineages.

## Outputs

Each lineage produces `annotated.h5ad`, `annotation_removed.csv`, `annotation_reassigned.csv` and `cell_exclusions.csv.gz`. Every exclusion carries the original cell identity, the two cluster ids, reasons and evidence. Reassignment is not an exclusion.

## Commands

Inside a dataset workflow the stage starts by itself. Standalone:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start-zoomin spec.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status-zoomin RUN_ID
python -m ecarsi.control --service-root <control> --task-queue <queue> resume-zoomin RUN_ID
python -m ecarsi.control --service-root <control> set-deg-limit zoom-in RUN_ID 16
```

The spec requires `run_id`, `dataset_id`, an SHA-256 `input` reference to the completed cross-sample publication, a fresh absolute `output_root`, `pool_root`, `bridge_root`, `max_in_flight_lineages` and `max_in_flight_deg`. Set `prepare_budget`, `subset_budget`, `compute_budget`, `deg_budget`, `tool_budget` and `merge_budget`, each with positive integer `cpus`, `memory_mb` and `timeout_seconds`. `config` requires `batch_col`, `species`, `tissue`, `min_cells`, `n_top_genes`, `n_pcs`, `n_neighbors`, `compute_backend`, `gpu_min_cells`, `gpu_memory_mb` and `max_refinements`. Species and sample key must match the accepted cross-sample run.

CPU comparisons share cross-sample's measured memory estimator. `set-deg-limit` survives coordinator restarts and keeps dispatched work.

Resume accepts only a failed workflow whose saved spec and input are unchanged and whose external outcomes are reconciled. It reuses accepted pool and bridge results.

## History

The 2026-09-15 GPU acceptance is in [docs/history/ZOOMIN_ACCEPTANCE_20260915.md](../history/ZOOMIN_ACCEPTANCE_20260915.md).

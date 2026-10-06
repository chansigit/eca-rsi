# Zoom-in stage

The zoom-in workflow reuses ZMIP's lineage planning, marker scoring, and merge checks. It also reuses MSP's numerical kernels. Model turns go through the model-turn service. Tools and numerical work run on pool workers.
## Steps

1. `prepare`: prepare lineage evidence, including the connectivity islands of the UMAP.
2. Lineage plan: an agent session chooses the lineages to zoom into. The default minimum is 800 cells. The host checks island connectivity. It rejects plans that merge separate islands into one lineage. Plans that split one island need `confirm_shared_islands: true`.
3. `markers`: create one shared marker table for all lineages.
4. For each lineage, allow at most `max_in_flight_lineages` lineages to compute at once:
   1. `subset`;
   2. `compute`: reset from counts. Then run normalization, HVG, PCA, Harmony, neighbors, Leiden at 1.0 and 2.0, UMAP and QC.
   3. DEG: run eight comparisons per request (`deg-batch`), fewer for a lineage above 50,000 cells. Apply the `max_in_flight_deg` limit, raised by the same factor.
   4. `assemble`;
   5. lineage annotation: one agent session submits types (`submit_types`, Leiden 1.0). It then submits quality (`submit_quality`, Leiden 2.0). Release numerical admission before the session starts. Another lineage can then compute while this one waits.
   6. `apply`: apply removals and reassignments for the lineage. Produce its report.
5. `merge`: merge every lineage back. Check exact input, survivor and exclusion coverage. Keep skipped lineages. Publish `annotated_zmip.h5ad` with a manifest.

Fine identity uses Leiden 1.0. Quality uses Leiden 2.0. The host records exact cell intersections. It does not assume nested partitions. Stress, dissociation and dying removals follow the stress policy (decision 0017): a removal of 10 or more cells stands on the stress-gene mark or the dying check, otherwise its cells stay, labelled in `retained_state` (`annotation_retained.csv` per lineage, merged into `annotated_zmip.h5ad`). Reassignment keeps the cells. It changes their labels. It does not re-embed them in the destination lineage. A requested subcluster refinement runs as one budgeted tool operation. It produces a new evidence version with matching DEG in the same session.

`compute_backend` selects `cpu`, `rapids` or `auto`. `auto` asks for a preferred GPU when the cell count reaches `gpu_min_cells`, subject to `gpu_memory_mb` and the actual grant. PCA, Harmony, neighbors and UMAP have RAPIDS implementations. Leiden, foreign scoring and the other CPU routines stay on CPU. Check the grant and the runtime record to confirm GPU use. Do not infer GPU use from the node name.

The workflow skips a lineage if its session dies twice. The lineage keeps its cross-sample labels. The stage fails when skipped lineages hold more than 10 % of the round's cells. The workflow continues as new past 5,000 history events. It carries finished lineages forward.
## Outputs

Each lineage produces `annotated.h5ad`, `annotation_removed.csv`, `annotation_reassigned.csv`, and `cell_exclusions.csv.gz`. Each exclusion records the original cell identity, the two cluster IDs, reasons, and evidence. Reassignment is not an exclusion.
## Commands

The stage starts automatically inside a dataset workflow. For standalone use, run:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start-zoomin spec.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status-zoomin RUN_ID
python -m ecarsi.control --service-root <control> set-deg-limit zoom-in RUN_ID 16
```

Include `run_id` and `dataset_id` in the spec. Include an SHA-256 `input` reference to the completed cross-sample publication. Set a fresh absolute `output_root`. Include `pool_root`, `bridge_root`, `max_in_flight_lineages` and `max_in_flight_deg`. Set `prepare_budget`, `subset_budget`, `compute_budget`, `deg_budget`, `tool_budget` and `merge_budget`. For each budget, use positive integers for `cpus`, `memory_mb` and `timeout_seconds`. In `config`, include `batch_col`, `species`, `tissue`, `min_cells`, `n_top_genes`, `n_pcs`, `n_neighbors`, `compute_backend`, `gpu_min_cells`, `gpu_memory_mb` and `max_refinements`. Match the species and sample key to the accepted cross-sample run.

CPU comparisons use the same measured memory estimator as cross-sample. `set-deg-limit` survives coordinator restarts. It preserves dispatched work.

Resume accepts only a failed workflow. Its saved spec and input must remain unchanged. Reconcile its external outcomes before resuming. Resume reuses accepted pool and bridge results.
## History

See [docs/history/ZOOMIN_ACCEPTANCE_20260915.md](../history/ZOOMIN_ACCEPTANCE_20260915.md) for the 2026-09-15 GPU acceptance.

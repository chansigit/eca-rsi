# Organize stage

One Temporal workflow per dataset runs three steps:

| Step | Runs on | Work |
| --- | --- | --- |
| `organize.prepare` | pool worker | Discover accepted ECA-PP products. Validate the source files. Save compact metadata profiles. |
| `organize.plan` | model-turn service and worker tools | The model proposes the analysis units and the source-scoped experiment mapping. `inspect_source` and `submit_plan` provide metadata. These tools also validate the proposal. |
| `organize.execute` | pool worker | Recheck source identities. Verify cell conservation. Do not split one complete experiment across units. Write the organized H5ADs and per-cell sample maps. Record output identities. |

Temporal stores references and state transitions. The pool and the model-turn service store their own requests and results. A model wait never holds a pool grant. The workflow does not request or release a Slurm allocation.

The plan session has a 30-turn budget. Rejected proposals return concrete errors for correction. A successful `submit_plan` ends the session. The model has no filesystem or code tools. One validation rule comes from a real failure. A proposal cannot call explicit multi-valued sample or library ids a single experiment.

The coordinator verifies the receipt, H5ADs, manifests, upstream snapshots, and mapping. Then, the coordinator publishes atomically to a fresh output root. Per-sample reads the confirmed map. It does not identify the sample column again. It also skips its optional batch-key suggestion for this path.

The organized matrix keeps expression only in `layers["counts"]`. `X` is an empty placeholder (`uns["X_placeholder"]`). Integer counts wider than 4 bytes become int32. The reserved obs columns are `source_unit`, `eca_source_cell_id`, `eca_pp_batch`, and `eca_pp_cell_type`. See [front-integration.md](../front-integration.md) for the input rules and the sample-map policies.

## Spec and commands

The dataset spec carries the budgets under `organize` (`prepare_cpus`, `prepare_memory_mb`, `prepare_timeout_seconds`, `execute_*`). Standalone, the spec has `run_id`, `input_root`, a fresh `output_root`, `pool_root`, `bridge_root`, and the two budgets:

```bash
python -m ecarsi.control --service-root <control> --task-queue <queue> start spec.json
python -m ecarsi.control --service-root <control> --task-queue <queue> status RUN_ID
```

Do not reuse a workflow id for a different run. Organize recovery reuses its publication validator. By design, the worker manifest and the relocated published manifest have different paths and records. Organize recovery requires both artifact identities and the accepted worker completion.

For large inputs, the shared HDF5 reader slices counts and reads obs directly. Preparation of a 126k-cell dataset stays well under a 4 GiB budget.

## Checks

```bash
LC_ALL=C LANG=C OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -q tests/test_durable_agent_bridge.py tests/test_front_integration.py tests/test_organize_v2_contract.py
```

## History

See [docs/history/ORGANIZE_ACCEPTANCE_20260914.md](../history/ORGANIZE_ACCEPTANCE_20260914.md) for the 2026-09-14 real-data runs, the concurrent eight-dataset test, and the migration status table.

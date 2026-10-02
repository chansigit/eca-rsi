# Periscope control-plane page

Periscope serves the control-plane page at `/_control/` when you start Periscope with `--control-plane <run dir>`. The page is read-only. It combines warm pool request and receipt records, bridge replies and token usage, and published stage outputs. The files `ecarsi/observatory.py` and `ecarsi/ui/observatory.html` implement the page and its API using the Python standard library.

## Timeline

The timeline reads the `workflow_id`, `dataset_id`, and `unit_id` trace fields from pool and bridge submissions. It also reads the stable worker id from pool acceptances. It displays every operation type. Overlapping tasks stack vertically inside one group for each worker or service.

- The time window supports up to 24 hours, with a dataset filter and a 2,000-task response cap. Narrow the window or the filter when the cap applies.
- The system caches completed requests. It does not reread them on every refresh.
- Scroll over the plot to zoom around the pointer (30 seconds to 24 hours). Select a preset to restore a live rolling window.
- The default `Latest activity` view finds the last group of executed tasks separated from earlier work by at least five minutes. It fits that interval with padding.
- Each pool worker and the bridge has one labeled group. The label reports the peak concurrent count in the selected window, not a configured capacity.
- `agent.turn` bars cover model turns and exclude tool waits. Legacy Organize bars span whole planning sessions.
- Queued requests have no execution bar. Queue wait appears in the task details. Records without worker ids group by host.

CPU, RAM, and GPU curves appear in a collapsed `Worker resource history` section with the same time window. Resource refreshes do not rebuild an unchanged task chart. Running bars stop at the present time. Task bars use one stable muted color for each dataset. A red inset outline marks a failure.

## Warm pool panel

The `Compute Warm Pool` panel groups worker identities by physical host. Each worker shows its advertised CPU and RAM budget, unfinished-task reservations, current CPU measurement, and five-minute average. Host RAM and visible GPU measurements appear once per host. The panel never adds these measurements to pool capacity.

Workers sample telemetry every 30 seconds. The page refreshes every 10 seconds. A recent sample shows that the worker supervisor is reporting. It does not prove HyperQueue membership. An identity with no sample for 90 seconds moves to the text history with its last observed time. A missing GPU sample means unknown, not zero.

Workers on the same host must use distinct work directories and disjoint CPU ids. The adapter enforces this requirement with host-scoped locks. The system records Slurm job ids from the joining process environment. The panel reports worker budgets, not whole Slurm allocations. Old task receipts supply historical hosts when the system did not save a worker identity. The panel reads only the records of this pool.

## Dependencies

Curved connectors join completed upstream requests to started downstream requests across the pool and bridge lanes. Workflow modules record predecessor request ids in `trace.depends_on`, for example `"depends_on": ["run-17.prepare"]`. Organize receipts use the known prepare → plan → execute order within one workflow. The system does not infer links from dataset names or submission order. Separate retries therefore stay separate.

Hover over a task to highlight its workflow. On crowded timelines, wide connectors fade until you highlight a workflow. Thin connectors keep 50 % opacity. Each `depends_on` entry identifies a predecessor request within the same workflow. Lists support fan-out, fan-in, and successive iterations. The system never infers dependencies from execution order. The system does not discard dependencies because of status or clock skew. The note shows rendered edge counts versus loaded edge counts. Tasks outside the window or the cap cannot provide visible endpoints.

## Trace fields and API

Set the same optional `trace` object on each pool or bridge submission, for example:

```json
{"workflow_id": "osp/run-17", "dataset_id": "dataset-17", "unit_id": "per-sample.compute", "depends_on": ["run-17.partition"]}
```

Without this object, an operation shows as `Unattributed`. The page never guesses a dataset from a request id.

`GET /_control/api/timeline?since=<epoch>&until=<epoch>&dataset=<text>` returns the selected task records and downsampled resource points.

Workers append host resource samples every 30 seconds to UTC-day JSONL files under `<pool-root>/workers/<worker-id>/`. These samples include allocated-core CPU usage, whole-host RAM usage, and accessible-GPU utilization and memory. The server returns at most 240 averaged points per worker. It polls the filesystem every 10 seconds. It does not start the scheduler, workers, coordinators, model requests, or dataset computation.

## Checks

- Run `node tests/test_dev_observatory_ui.cjs` to run the geometry and dependency checks.
- Run `node tests/check_dev_observatory_browser.cjs <viewer-url>` to run a Playwright browser check against existing records. Set `OBSERVATORY_ARTIFACTS` to save a screenshot. This check does not submit scientific work or model requests.
- Run `python -m pytest -q tests/test_dev_observatory.py tests/test_dev_observatory_status.py` to test the API and the `status` report.

## History

[docs/history/DEV_OBSERVATORY_OPS_20260918.md](../history/DEV_OBSERVATORY_OPS_20260918.md) records the standalone observatory process of September 2026 and its tmux and SQLite setup.

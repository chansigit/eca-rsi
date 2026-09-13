# Periscope warm pool panel

The **Warm pool** button sits below **Overview** in Periscope's home navigator.
Click it to display worker resources, active tasks and the queue in the right
content panel. Overview and dataset navigation restore the usual content.
The panel inherits Periscope's colors and light/dark preference, with subtle
highlights on resource gauges and node borders.

There is no standalone pool page or pool-specific URL hash. Old `/_pool` and
`/__pool__` page URLs return HTTP 404; old `#/__pool__` bookmarks fall back to
Overview. Open Periscope at `/` and use the button.

Point the web server at an existing pool:

```bash
eca-rsi serve --pool-scheduler /shared/pool/scheduler.json
```

`ECA_POOL_SCHEDULER` supplies the default when the flag is omitted. This only
connects the monitor: it does not start a scheduler, register workers or change
driver compute modes. Periscope can run on a different host from the pool.

Without a configured, reachable ECA-RSI pool scheduler, the button is gray and
disabled. The data endpoint `/_pool/status.json` returns HTTP 503 when the pool
is unavailable. A leftover scheduler file or an ordinary Dask scheduler does
not enable access. A live pool with zero workers remains viewable.

The browser checks availability every five seconds. Opening the panel fetches
fresh data before showing it; stopping the pool closes the panel, clears its
measurements and restores Overview. The button becomes usable when the pool
returns. Navigation away cancels pending panel updates. Data requests probe
the scheduler with a bounded timeout and `Cache-Control: no-store`; existing
Periscope authentication applies to both monitoring endpoints.

Worker and queue views refresh every five seconds. GPU and Slurm inventory are
sampled about every 30 seconds; allocation details show inventory age and stay
expanded through automatic refresh. CPU is normalized by assigned CPUs, and
RAM is worker process RSS. These are worker measurements, not a whole-host
survey. Slurm memory totals count each host/job allocation once.

Header gauges use reporting online workers: CPU is weighted by assigned CPU
count, RAM is summed RSS divided by summed worker budgets, and GPU utilization
is the mean of reported device utilizations. Unknown measurements show a dash.
Closing Periscope never shuts down the pool. Resource lifecycle remains under
the existing [pool commands](SLURM_POOL.md).

A batch can appear before its output directories exist. Set
`ECA_PERISCOPE_BATCH_STATUS` to its status JSON, whose `datasets` entries contain
`mirror`, `state`, and `n_cells`, then bind those mirror paths in the registry.
Queued entries render a waiting page without creating analysis files. The
batch runner continues to own execution and output creation; Periscope only
reads its status. A paused entry in a still-active batch without a `pause` file
is waiting to resume. Study outputs arranged as
`<collection>/<study>/rsi` beside `standardize/result.json` are grouped by
collection, as are the existing `eca-pp` trees.

CPU percentages are relative to allocated CPUs, both per worker and across
the pool. The monitor also shows equivalent CPU cores in use so a small task
is visible within a large allocation. Model calls run in drivers and can leave
compute workers idle between computation steps.

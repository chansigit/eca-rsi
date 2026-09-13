# Periscope warm pool monitor

The **Warm pool** button sits below **Overview** in Periscope's sidebar.
It opens a read-only view of workers, CPU/memory/GPU use, Slurm allocations,
remaining time, active tasks and the admission queue.

Point the web server at an existing pool:

```bash
eca-rsi serve --pool-scheduler /shared/pool/scheduler.json
```

`ECA_POOL_SCHEDULER` supplies the default when the flag is omitted. This only
connects the monitor: it does not start a scheduler, register workers or change
driver compute modes. Periscope can run on a different host from the pool.

Without a configured, reachable ECA-RSI pool scheduler, the button is disabled.
The server also refuses direct requests to `/_pool` and `/_pool/status.json`
with HTTP 503. A leftover scheduler file or an ordinary Dask scheduler does
not enable access. A live pool with zero workers remains viewable so workers
can be monitored as they join.

The browser checks availability every five seconds and re-enables the button
when the pool returns. If the pool disconnects while its page is open, the
page clears the old measurements and the navigator returns to Overview.
Direct page/data requests probe the scheduler afresh, with a bounded timeout
and `Cache-Control: no-store`; existing Periscope authentication also applies.

Worker and queue views refresh every five seconds. GPU and Slurm inventory are
sampled about every 30 seconds; allocation details show inventory age. CPU is
normalized by assigned CPUs, and RAM is worker process RSS. These are worker
measurements, not a whole-host survey. Slurm memory totals count each host/job
allocation once, even if multiple workers share it.

Closing the monitor never shuts down the pool. This page has no submission,
drain or Slurm cancellation controls. Start and stop resources with the
existing [pool commands](SLURM_POOL.md).

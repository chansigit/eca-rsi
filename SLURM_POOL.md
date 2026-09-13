# Slurm warm pool

The pool runs work on Slurm allocations you already own. You request CPU/GPU
resources, start workers and release allocations yourself. Drivers and the pool
have independent lifetimes. Periscope can continue to observe driver outputs.

Install `ecarsi[pool]` in the scheduler, worker and driver environments. OSP/MSP
must also be installed where their work runs. Use the same scientific package
versions and source on drivers and workers; restart workers after source edits.
The existing `MSP_COMPUTE_ENDPOINT=local|dask-local|dask` modes still work.

## Start a scheduler

Run on a host reachable from your allocated nodes. These commands stay in the
foreground; use your usual service/session launcher for persistent processes.

```bash
eca-rsi pool scheduler --scheduler-file /shared/pool/scheduler.json
```

This is a Dask scheduler with one shared admission queue. Use it within your
trusted account/network, with the same access controls as your existing Dask
cluster. The scheduler file must be a new path; it does not replace a running
scheduler. No job submission or cancellation is performed by these commands.

## Join an allocated node

Run the host launcher **inside your existing Slurm job/step**, where `scontrol`
is available and Slurm job cgroups constrain the process. Setting `SLURM_JOB_ID`
alone is insufficient. `--python` can be a Python interpreter or your container wrapper.
Use `taskset` within the granted affinity if several worker processes share a
node; give them disjoint CPU sets and GPU devices, and divide the memory budget.

```bash
python -m ecarsi.pool.slurm \
  --scheduler /shared/pool/scheduler.json \
  --directory /shared/pool/worker-a \
  --python /path/to/compute-python \
  --cpus 4 --memory-gb 16 --root /shared/data
```

`--cpus` selects the first CPUs from the launcher's actual affinity; omit it to
use the whole affinity. Each process accepts **one heavy task at a time** and
fixes native library thread counts to that profile. CPU locks prevent these
launchers from using the same CPU twice on a host. The scheduler also rejects
overlapping CPU/GPU inventories and memory totals above the allocation limit.
Resources used by unrelated processes or older Dask pools are still your
responsibility; assign separate capacity when running them together.

For GPUs, start inside a GPU Slurm step and add `--gpu`. The launcher checks
`CUDA_VISIBLE_DEVICES` against Slurm's GPU IDs and passes unique device UUIDs
into the worker. CPU workers hide GPUs. Nanny monitors each worker's memory
limit and terminates/restarts a worker that exceeds Dask's termination threshold.

The host refreshes Slurm end time and cgroup limits every 30 seconds. Inventory
older than 90 seconds stops new admissions. A task needs its estimated duration
plus a 60-second margin before allocation expiry. Slurm remains the final hard
resource/time limit. Draining finishes active work and rejects new work:

```bash
eca-rsi pool status --scheduler /shared/pool/scheduler.json
eca-rsi pool status --scheduler /shared/pool/scheduler.json --json
eca-rsi pool drain --scheduler /shared/pool/scheduler.json tcp://worker:port
```

Status shows task occupancy, worker CPU utilization divided by its assigned CPU
count, process RSS versus worker memory budget, Slurm memory and remaining time.
GPU utilization and VRAM use come from the host inventory refreshed every 30
seconds. These are your workers' metrics, not other users' load on the host.
JSON also includes Slurm requested/allocated TRES for resource reconciliation.
Slurm GPU device minors and CUDA visible ordinals are matched through UUIDs.

After draining and observing no active task, stop the worker launcher yourself.
This stops its processes; it does not release the Slurm allocation. New workers
can join the same scheduler at any time.

## Attach drivers

```bash
export ECA_POOL_SCHEDULER=/shared/pool/scheduler.json
export ECA_POOL_DATA_ROOT=/shared/data
export OSP_COMPUTE_ENDPOINT=pool
export MSP_COMPUTE_ENDPOINT=pool
eca-rsi run --help
```

`pool` always queues computation. `auto` keeps very small tasks local when local
resources suffice; for larger tasks it uses a compatible idle pool slot, or
local computation when the pool is busy and local resources suffice. It does
not move already submitted/running work. With no pool configured, `auto` can
run locally when resources suffice. The default remains `local` and does not
require Dask. GPU algorithm selection remains the existing explicit MSP option;
`auto` only chooses the execution location.

The shared queue scans arrival order and assigns the first compatible free
worker. Tasks that do not currently fit retain their positions while fitting
tasks can pass. There are no priorities, fair-share quotas or future capacity
reservations. Large jobs can wait longer under a continuing stream of small
jobs; reconsider this policy only if actual workloads show a problem.

Requests describe CPUs, memory, GPUs and estimated seconds. Defaults are coarse
estimates from array sizes or OSP cell counts; override them for known workloads:

```bash
export ECA_POOL_TASK_CPUS=4
export ECA_POOL_TASK_MEMORY_GB=16
export ECA_POOL_TASK_SECONDS=900
export ECA_POOL_QUEUE_TIMEOUT=3600
```

Admission timeout cancels queued/unstarted work and fails visibly. Requests are
small; arrays are transmitted only after admission. OSP uses shared paths and
checks input identity; set the same data root with worker `--root` and driver
`ECA_POOL_DATA_ROOT`. MSP can send arrays directly, including sparse arrays.
The Python adapter also accepts explicit estimates:

```python
from ecarsi.pool.client import PoolEndpoint

with PoolEndpoint(mode="pool") as compute:
    result = compute.submit(function, array,
                            needs={"cpus": 2, "memory": 8 * 2**30,
                                   "seconds": 300}).result()
```

`submit` waits for admission; `.result()` waits for execution. Separate drivers
share the same queue. A worker disconnect fails its active request; a cancelled
future or disconnected driver does not free resources while Python still runs.
The execution wrapper rejects a replay of an already-started grant.

OSP QC (including requested Scrublet/DecontX), clustering and DE run together in
the pool. Annotation stays in the driver. Each remote invocation writes a fresh
`.pool-attempts/` directory. Only the driver holding its existing writer lock
validates and publishes output and completion receipts. Failed/interrupted
attempt directories remain for diagnosis; successful attempts are removed.

The queue is in memory, with recent terminal status retained for an hour. A
scheduler restart changes its epoch and invalidates old grants. Resume through
the existing RSI/MSP driver checkpoints; there is no durable queue replay or
automatic migration of running work. Completed OSP compute checkpoints still
allow annotation-only retries. Status includes queue reasons, actual durations
and worker-process peak RSS (a cumulative high-water mark, not per-task memory).

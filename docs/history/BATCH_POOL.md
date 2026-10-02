> **Retired on branch `gen2` (ecarsi 0.3.1).** Dataset admission is the Temporal control plane's
> (`python -m ecarsi.control start-dataset`, see docs-gen2/ARCHITECTURE.md); `eca-rsi batch`, the node
> agents, the preparation queue and the driver memory leases described below exist only on `main`.

# Persistent dataset queue

`eca-rsi batch` distributes dataset drivers across the Slurm allocations you
have already supplied. The drivers submit OSP/MSP/ZMIP computation through the
existing warm pool. You can keep adding datasets while other datasets run;
neither the queue nor the node agents need restarting.

Drivers and compute workers have separate resource budgets. Reserve disjoint
CPU sets and memory for these two roles when configuring a node. Their combined
memory budgets must fit the allocation, with room for Slurm and runtime overhead.
Keeping compute capacity available prevents drivers from occupying all the
resources they subsequently need for computation. A node may run several drivers
and several compute worker processes.

`driver_cpu_slots_per_core` defaults to `1`. Set it to `2` to let two drivers
share each CPU owned by the node agent while model or I/O calls wait. Admission
still checks `max_cpu_percent` and each driver's current memory reservation.
Sharing stays inside the driver CPU slice; compute workers keep their own CPUs.
Drivers use shared CPU locks within an exclusively registered driver slice, so
restarting the node agent preserves the ability to admit another driver on that
slice. Workers require exclusive CPU locks, and the allocation ledger rejects
overlapping driver/worker reservations. Older attempts with exclusive locks
must finish or reach a checkpoint before that CPU can use the new protocol.
The setting reloads from the configuration;
reducing it stops further sharing without interrupting existing drivers.

With `driver_memory_lending`, matrix-free stages retain measured RSS plus a
margin while models wait. `driver_model_memory_margin_gb` defaults to 2 GiB;
the reservation also retains at least 25% RSS headroom and a 2 GiB floor.
Managed runtimes can request `driver_budget.work(memory_bytes=...)` for a
particular matrix operation. The controller adds the rest of the driver tree's
RSS; unprofiled operations and return to the outer driver still request the full
dataset budget. Matrix estimates use uncompressed HDF5 array shapes.
The preparation runtime also lends its driver reservation while waiting for
remote pool work, then reacquires it before continuing locally. Driver-node
headroom counts clean, reclaimable cgroup file cache but excludes dirty pages
and shared memory; the per-node budget remains below its Slurm hard limit.

Preparation uses `preparation_memory_gb` as its baseline and increases it for
large metadata tables (1 KiB per input cell plus 1 GiB for imports), up to the
dataset budget. `preparation_max_datasets` bounds running preparation and
prepared datasets awaiting their driver; it does not bypass sample decisions. Optional
`preparation_backfill_slots` (default 0) permits that many extra preparations
when a fresh pool resource report shows room for their estimated compute.
Before sample confirmation, the estimate treats the entire dataset as one
sample. Driver memory limits still apply; completed preparation waiting for a
driver continues to occupy its backfill slot. The controller refreshes pool
capacity every 15 seconds and stops backfill if that report becomes stale.
With `preparation_continuation: true`, confirmed samples in datasets waiting for
full driver capacity can use these same bounded slots. The controller reads
sample receipts outside its queue lock. Preparation skips verified results and
limits both new sample size to worker capacity and total unannotated computed
samples to `preparation_samples`; retries retain those limits. This requires a
preparation runtime supporting `--max-prepared-samples` and `--max-sample-cells`.
For continuation, only active continuation attempts occupy the extra slots:
their datasets already belong to the prepared backlog. The per-dataset sample
cap still applies after every retry.

Pinned older OSP runtimes read `ECA_POOL_TASK_CPUS`; setting only
`OSP_POOL_TASK_CPUS` does not change their requests. Execution settings are
captured per attempt, so configuration changes apply at the next admission.
Workers bind the granted CPUs and set native thread limits to the grant.
For managed MSP/ZMIP attempts, the operational `compute_policy` adapter selects
CPU when no compatible GPU is ready and records the backend per integration
output directory. Retries reuse that decision; concurrent lineages choose
independently. Existing completed integrations keep their original checkpoints.


For pooled OSP runs with annotation enabled, per-sample computation and model
annotation have separate slots. `PERSAMPLE_PARALLEL` caps each phase; their
combined active processes still share the driver's memory budget. Computed
samples wait in a bounded annotation queue, retaining verified checkpoints.
Pausing drains active phases and preserves those checkpoints. A computed
sample is not reported as complete until annotation and output validation pass.
This describes the current source; a running pinned environment keeps its
installed behavior until explicitly upgraded. The outer batch queue still
reserves resources for the entire driver attempt: separate OSP slots alone do
not release that reservation while a dataset waits for a model.

For a pinned scientific environment, install `osp_dispatch.py` and `osp_stage.py`
in a separate Python package (for example `eca_stages`) on `env.PYTHONPATH`, and
set `osp_dispatch_module` to `eca_stages.osp_dispatch` in the batch configuration.
New admissions run the installed RSI organizer, the separate OSP dispatcher,
then the installed RSI continuation. Existing attempts retain their snapshot.
The dispatcher uses the installed scientific kernels and original finalizer;
`persample/orchestration.json` records its source identities separately. It does
not change scientific runtime identities or relax checkpoint validation.

## Configure once

Create `~/.config/ecarsi/batch.json` (or pass `--config` to each command):

```json
{
  "directory": "/shared/pool/datasets",
  "python": "/shared/venv/python",
  "scheduler": "/shared/pool/scheduler.json",
  "env": {
    "PYTHONPATH": "/shared/pinned-scientific-runtime",
    "PERSAMPLE_PARALLEL": "2",
    "MSP_COMPUTE_GPU": "1",
    "MSP_PYTHON": "/shared/gpu-venv/bin/python",
    "ZMIP_PYTHON": "/shared/gpu-venv/bin/python"
  },
  "required_env": ["ARK_API_KEY"],
  "max_cpu_percent": 90,
  "resource_policy": "input",
  "max_attempts": 2,
  "min_attempt_hours": 2,
  "pause_dispatch": false,
  "defaults": {"cpus": 2, "memory_gb": 8, "hours": 6}
}
```

Use paths and a provider environment appropriate to your installation. The
`python` entry may be a container wrapper. `MSP_PYTHON` and `ZMIP_PYTHON` must be
usable from inside that container. GPU stages require a compatible GPU worker;
runtime matching remains enforced. CPU-only deployments can omit the GPU entries.
Keep API keys in the node environment, not the JSON file. An optional `registry`
path automatically registers submitted mirrors in Periscope.

Start a node agent **using host Python, inside your existing Slurm allocation**:

```bash
taskset -c <reserved-driver-cpus> python3 -m ecarsi.batch node --memory-gb 32
```

The host Python must be able to import this installation of `ecarsi`. It needs
no scientific dependencies for node management. Input profiling additionally
needs `h5py` in the submission environment. The agent validates the Slurm
cgroup, affinity, memory and expiry, and holds the same CPU locks used by compute
worker launchers. If those CPUs still belong to earlier work, it waits for their
release. Start agents on more allocated nodes to add capacity. No command here
requests a new allocation or returns one to Slurm.

## Submit datasets

Prepare a JSON list, with one entry per independent dataset:

```json
[
  {
    "name": "study-a",
    "input": "/shared/eca-pp/study-a",
    "output": "/scratch/rsi/study-a",
    "mirror": "/shared/eca-pp/study-a/rsi",
    "registry_name": "collection-study-a",
    "cpus": 2,
    "memory_gb": 16,
    "hours": 6
  }
]
```

```bash
eca-rsi batch submit datasets.json
eca-rsi batch status
# Later, while the first submission is still running:
eca-rsi batch submit more-datasets.json
```

`memory_gb` is the driver budget, separate from remote kernel memory. `hours` is
the attempt's maximum runtime. By default admission requires that much Slurm
time remaining; optional `min_attempt_hours` allows a shorter allocation window
for checkpointed execution. Neither setting extends the allocation: the attempt
stops before its allocation ends. A confirmed allocation expiry requeues through
validated checkpoints without consuming the error retry allowance; the queue
waits if no allocation has enough remaining time. An explicit user pause still
prevents automatic resume. Optional
`sample_map` points to an existing experiment mapping; it is explicitly passed
to every unit's persample stage. Output directories identify submissions:
resubmitting the same input/output/mirror entry returns its existing queue ID.
It does not start another copy. An input must already satisfy the ECA-PP contract;
batch submission does not replace preprocessing or scientific input validation.

With `resource_policy: "input"`, omit both `cpus` and `memory_gb` to estimate
new driver budgets from standardized H5AD metadata. Explicit resources override
this policy. The estimate accounts for uncompressed counts and dense HVG work;
it is not a proven peak-memory bound. Slurm continues enforcing memory limits.
To recalculate budgets for currently queued datasets only, run
`eca-rsi batch profile`. Active attempts keep their admitted budgets.

The queue uses arrival order, allowing a fitting dataset past an older one that
needs more resources. It selects a node with spare capacity, accounting for
reserved CPU IDs, reserved RAM, observed driver memory and CPU usage, live cgroup
headroom, and remaining Slurm time. Unused CPU percentage does not erase an
active task's reservation: a temporarily idle model call can become busy again.
Every driver runs in a separate Slurm step with its own enforced memory limit
and CPU affinity. Node-level concurrency follows these budgets, not a fixed
fleet-wide dataset count.

Node agents reload configuration every five seconds. `pause_dispatch` and the
CPU pressure threshold affect new admission immediately. An invalid update
retains the previous configuration. Each admitted attempt retains its own
runtime/environment snapshot; changing defaults does not rewrite active work.
Changing a node's CPU/memory partition requires draining that node agent first.

## Monitoring and recovery

Set `ECA_DATASET_QUEUE=/shared/pool/datasets` for Periscope. Overview shows
workflow drivers, measured process memory, memory limits and the latest logged
phase. Warm Pool shows compute workers and compute requests. A workflow waiting
on a model is not a queued compute request. A legacy
`ECA_PERISCOPE_BATCH_STATUS` may coexist during migration.
Usage follows the actual Slurm task process; the local `srun` client is not the
scientific process. Refreshes are every five seconds.

Agent Bridge shows instrumented agent turns across OSP, MSP and ZMIP, including
active/model-wait/retry states, completed successes and failures, and reported
input/output tokens and cost. Its panel refreshes every ten seconds. These are
calls observed after the updated bridge loads, not a backfill of older runs.
There is no separate AI submission queue: a turn waiting for a model is shown
as such, while compute requests remain in Warm Pool. The daily append-only
event log and current summary are in
`~/.cache/ecarsi/agent-bridge/events-YYYY-MM-DD.jsonl` and `state.json` (or
`AGENT_BRIDGE_TELEMETRY_DIR`). They contain labels, model names, status and
usage, never prompts, model replies or API keys. Token/cost totals include only
results reported by their backend; an absent cost is not treated as zero spend.

For durable compute telemetry, run this on a host with Dask, SSH access to the
workers, and the same shared home and project paths:

```bash
python3 -m ecarsi.service --state /shared/pool/observer-service.json -- \
  python3 -m ecarsi.pool.observe --directory /shared/pool/observations \
  --scheduler /shared/pool/scheduler.json \
  --queue /shared/pool/datasets/status.json
```

The observer targets a resource snapshot every 15 seconds and writes
`ten-minute-windows.jsonl` every ten minutes. It probes each worker supervisor's
process tree every 30 seconds, including child CPU time and summed RSS. Summed
RSS can count shared memory more than once; it is not Slurm cgroup memory use.
Periscope labels this measurement explicitly and falls back to Dask process
metrics if the host probe is missing, mismatched or older than 45 seconds.
CPU percentages use the worker's assigned CPU count, not the entire machine.
For example, `cpu_percent: 25` on an eight-CPU worker means
`cpu_cores_used: 2`. `rss_bytes` measures process memory;
`reserved_memory_bytes` records scheduler reservations. GPU utilization and
VRAM remain separate. Worker records include the Slurm job, CPU IDs, remaining
time, numerical runtime, active task IDs and requested resources. Driver-node
records show measured RSS, reservations and model-wait counts; missing RSS is
counted explicitly. Historical/draining predecessor nodes must not be added
again when computing fleet totals.

`utilization-latest.json` contains the complete current snapshot.
`utilization-averages.json` updates every observation with trailing ten-minute
sample means for worker CPU, RSS/memory utilization and GPU utilization. Missing
readings are excluded; newly joined workers have shorter coverage. Set
`ECA_POOL_OBSERVATIONS` to this log directory when starting Periscope to show
these averages beside live usage. Periscope reads the small summary in the
background and marks readings older than 90 seconds unavailable.
`utilization.jsonl` retains resource history, active tasks, and completed tasks
when first observed or changed. `events.jsonl` records observed worker/task and
dataset state changes, queue reasons and collector errors/recovery. Polling can
miss intermediate transitions; task records retain scheduler timestamps for
submission, start and completion. The scheduler retains completed tasks for one
hour, so an observer outage longer than that can lose task history. Each row has
a UTC timestamp. Collection duration, actual sample gaps, heartbeat ages and
metric source/age distinguish a stale reading from an idle worker. Ten-minute
reports include CPU averages, peak sampled RSS, GPU averages and kernel counts;
worker addresses identify separate processes on the same host.

All three JSONL streams rotate at 64 MiB and retain seven numbered backups by
default (`--log-max-mb 64 --log-backups 7`). This is a size limit, not a fixed
number of days. An oversized legacy log is preserved as a backup on upgrade.
Only one observer may write a directory; use another directory for a separate
`--once` diagnostic run. Dataset log paths and modification ages supply context;
resource logs do not copy model responses or process environments. An unreadable
queue does not stop compute telemetry. Probe and queue failures appear in the
saved reports. `--once` takes one snapshot for verification. This service only
observes resources; it does not change scheduling or request allocations.

The shared queue survives agent restarts and continues accepting submissions
even with no nodes online. SIGTERM to a node agent drains it. Each dataset has an
independent host supervisor that owns its container and retains CPU locks until
execution exits; restarting an agent does not terminate that container. Work
whose execution is uncertain is not silently retried. Once a terminal receipt
confirms that an attempt has stopped, resume it with:

```bash
eca-rsi batch retry <queue-id>
eca-rsi batch retry <queue-id> --memory-gb 32
```

For bounded automatic recovery, run one `eca-rsi batch controller` on a durable
host. `max_attempts` defaults to 1 (no automatic retry); 2 allows one retry.
The legacy client error `ConnectionError: pool task done:` means the worker
disappeared before result retrieval; it qualifies for the same bounded recovery
as a disconnected worker. A final supervisor receipt replaces an earlier
provisional failure reason. Migrated legacy outputs appear once in Periscope,
with the persistent queue taking precedence over the old batch record.
The controller requires a matching terminal receipt. Recorded worker loss,
confirmed orphan termination, temporary process-launch failures, explicitly
retryable OSP failures and driver time limits qualify. Scientific validation failures, explicit pauses and uncertain live
executions remain held. Recovery enters `retry_wait` for 60 seconds per previous
attempt before returning to normal resource admission. Previous receipts and
attempt history remain available. The controller does not provision resources.

The scientific pipeline validates its recorded checkpoints on resume. Completed
outputs and previous attempt receipts remain intact. Queue files live under
`directory`: `status.json`, `.queue.lock`, and `receipts/`; dataset logs default
to `<output>.log`. The shared filesystem must support POSIX file locks and atomic
rename. Keep it on durable shared storage according to your cluster's policy.

Compute workers launched with `python -m ecarsi.pool.slurm` are supervised for
the lifetime of their existing Slurm allocation. If the worker/container exits,
the host launcher revalidates the grant and restarts it with exponential delays
from one to sixty seconds. Queued compute requests then use the ordinary pool
admission path. CPU locks remain held during recovery, and the previous process
group must exit before a replacement starts. Temporary Slurm query failures
are retried; stale inventory never authorizes a new worker. SIGTERM/SIGINT to
the host launcher or allocation expiry stops recovery.

The worker directory contains `launcher.json` with the host/child PIDs, restart
count, last exit/error and next retry time. A task that lost its worker after
execution started still fails visibly. The optional batch controller can resume
eligible workflows through validated driver checkpoints; it does not blindly
re-execute an arbitrary compute function.
GPU-only kernels are not silently changed to CPU kernels. No allocation is
requested or released by the supervisor.

Node agents adopt surviving supervisors after a restart and inherit only each
driver's CPU locks. A dead supervisor's named Slurm step and local launcher are
stopped before capacity is released. Legacy executions are observed while alive
to record exact process/step identities. If that evidence is unavailable, they
remain reserved until execution or allocation termination can be confirmed.
Once an allocation ends, the controller requires a terminal Slurm accounting
record, no remaining steps and exclusive access to every output writer lock
before retiring its attempts. An unavailable Slurm query never proves death.
Step discovery uses `scontrol -o show step <job-id>` so jobs using Slurm's
step manager are covered. `squeue --steps` can omit their live numeric steps.

For a planned handover, `eca-rsi batch handoff <node-id> ...` requests the existing
drivers' safe checkpoints. Confirmed handovers resume automatically without
using the failure retry allowance; explicit workflow pause controls take priority.

Run management commands under the process supervisor when unattended recovery
is required, for example:

```bash
python3 -m ecarsi.service --state /shared/pool/controller-service.json -- \
  python3 -m ecarsi.batch controller
```

The same wrapper accepts a node-agent or scheduler command. Scheduler restarts
reuse the recorded endpoint. Worker and driver memory reservations share a
per-host, per-allocation ledger. Existing CPU locks still enforce disjoint CPU
ownership; unused RSS does not authorize oversubscribing reserved memory.

Compute execution has a separate hard bound: `ECA_POOL_EXECUTION_TIMEOUT` in
the scheduler environment defaults to 21,600 seconds, capped by the worker's
remaining allocation. A request may provide its own `execution_timeout`.
Admission-time estimates do not set this deadline. A timed-out computation
keeps its slot until the old worker process group is fenced and removed.

Managed MSP/ZMIP stages can release their driver memory reservation while
waiting for model decisions. Enable `driver_memory_lending` in the batch config
only with a matching stage runtime. Its Python adapter is selected through
`MSP_PYTHON` and `ZMIP_PYTHON`; `ECA_DRIVER_BUDGET_MODULE` identifies the budget
client. The batch supervisor supplies an isolated lease directory per attempt.
Existing attempts keep their execution configuration until a checkpoint handoff.

The stage retains cell metadata during model calls and loads expression only
inside a budgeted operation. Pool tools read the shared H5AD directly. A compact
request and fresh process measurements are both required before reducing the
reservation; low RSS alone never releases capacity. The controller retains one
return-to-compute memory window per driver node. Local heavy work waits for its
full reservation to be durably restored, and returning to the ordinary RSI
driver also restores that reservation. Pool worker budgets remain separate.

Build stage orchestration into a new immutable namespace with
`ecarsi.build_stage_runtime`; keep the numerical packages unchanged. The adapter
records `.rsi-stage-runtime.json` in each stage directory. Existing agent progress
without this receipt resumes through the original implementation; it does not
gain memory lending in that stage. Never edit packages imported by active
processes or replace a published namespace in place.

Before enabling a new preparation namespace, verify it can be imported by the
scheduler, every pool worker, and the isolated task interpreter. Distributed
task deserialization needs the namespace on the scheduler as well as workers;
a successful driver import alone is insufficient. Publish the operational
namespace separately from the pinned numerical packages.

To let never-started datasets reach OSP before a full driver reservation fits,
publish `build_preparation(destination, pinned_ecarsi_directory, namespace)`
from `ecarsi.build_stage_runtime`. Add these fields to the batch JSON config:

```json
{
  "preparation_module": "eca_prep_v1.prepare_osp",
  "preparation_memory_gb": 4,
  "preparation_max_datasets": 8,
  "preparation_samples": 4
}
```

Preparation reserves one driver CPU and up to 4 GiB. It verifies upstream inputs,
obtains the ordinary organize and sample-mapping decisions, and reads metadata
without loading expression layers into the driver. Pool workers perform source
profiling, confirmed organization, subset writing and the existing OSP compute
stage under their own resource limits. Heavy preparation requests retain the
dataset's original peak memory estimate. No sample computation starts before
its input and sample mapping are confirmed.

The cap counts active preparation attempts and prepared datasets waiting for
full driver admission. Each preparation attempt computes at most four samples;
remaining samples follow the normal OSP workflow. A successful preparation
receipt returns the dataset to the ordinary queue with `preparation_complete`.
It does not mark the dataset complete or consume a full-driver failure retry.
The full driver checks and reuses the OSP compute receipts, then performs the
required annotations and downstream stages. Older attempts retain their
existing resume path. Configuration changes apply to new admissions.

A node agent started with `--role preparation` accepts only these small
preparation attempts; ordinary driver nodes can accept both phases. The node
must already belong to a user-provided Slurm allocation. Preparation capacity
and pool capacity are separately accounted for; this option allocates no nodes.

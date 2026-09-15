# Warm Pool v2: first implementation

The development package `ecarsi.warm_pool` implements durable, bounded compute
requests over **HyperQueue 0.26.2**. HyperQueue handles CPU/memory placement and
backfilling. RSI stores request identities, cancellation intent, execution
receipts, and output hashes independently of the backend journal.

This is the CPU recovery milestone in the
[approved component design](DURABLE_WORKFLOWS.md). It does not route existing
datasets or change the production `ecarsi.pool` service. Commands in the
acceptance test are synthetic; no scientific throughput claim follows from it.
Both local recovery and a two-host Slurm trial have passed, including starting
the replacement Scheduler on the other host while keeping the same state root.

## Run an isolated pool

Use Linux, Python 3.11+, and the native
[HyperQueue 0.26.2 release](https://github.com/It4innovations/hyperqueue/releases/tag/v0.26.2).
The official `hq-v0.26.2-linux-x64.tar.gz` archive has SHA256
`e15dae9113e1a307a97a66bfe90f74f78c6016239436b5d9f1e4efec480e84b5`.
HQ must be a direct native child of the Python supervisor. On an older host,
run Python and HQ together inside Apptainer; wrapping only the HQ command in
another container launcher breaks the tested parent-death behavior.

Run from this checkout, or install it into the chosen environment. The package
itself uses the Python standard library. Put state outside the repository on a
durable filesystem accessible at the same path to every participant:

```bash
pool_state=/absolute/durable/path/development-pool
python -m ecarsi.warm_pool --root "$pool_state" init --hq /absolute/path/hq
```

Initialization requires a user-owned directory with mode `0700`. Backend
configuration is immutable for this pool. The runtime defaults to the current Python; its
binary hash and `--version` are checked before a worker joins and before each
execution. This default identifies only the interpreter. Scientific workers
should additionally pin their image and declared package paths as below.

### Scientific runtime

`configure-runtime runtime.json` validates the target environment before selecting
it for new requests. Run it inside that environment. The JSON includes `command`,
`version`, `files` (absolute file path to SHA256), and can additionally specify:

- `image`: absolute `path` and `sha256` of the running Apptainer image.
- `pythonpath`: explicit absolute package directories, replacing inherited paths.
- `imports`: modules that must import successfully before worker registration.

```bash
python -m ecarsi.warm_pool --root "$pool_state" configure-runtime runtime.json
```

The image hash is checked at worker registration; its active image path and
declared files are checked per task. Each request retains its original runtime
fingerprint. Updating the default does not rewrite old requests or change an
already running worker. Start workers in the new image before expecting new
requests to run, and finish old work before replacing old workers. HQ places each
request only on matching runtime capacity. Numba caches are separated by runtime
fingerprint. This does not package mutable application source automatically;
operations must also declare source/input hashes.

The 2026-09-14 per-sample trial uses `rsi-cpu-20260914.sif`: scientific packages
and OSP live under `/opt/rsi-python`, with package versions and source hashes in
`/opt/rsi-runtime.json`. Compute no longer reads the external `dl2025` package
directory. Bridge and Coordinator control environments are still separate and
have not yet been packaged into this science image.

Start these foreground processes in separate terminals. Select CPU IDs from
`os.sched_getaffinity(0)` and memory from your actual available allocation:

```bash
python -m ecarsi.warm_pool --root "$pool_state" scheduler --host 127.0.0.1
python -m ecarsi.warm_pool --root "$pool_state" worker \
  --cpus 0,1 --memory-mb 256 --work-dir /absolute/node-local/worker-directory
```

The example address is for local testing. Local execution is a one-worker
pool, using the same requests and receipts. Worker CPU locks also use the
existing per-host RSI lock directory, preventing overlap with cooperating
legacy supervisors. Each worker's memory budget must be provisioned explicitly.
No command requests or releases a Slurm allocation.

### Join an existing Slurm allocation

Run `slurm-worker` **on the allocated host**, using a host Python 3.11+ with
this checkout on `PYTHONPATH`. It reads `scontrol` and the actual process cgroup,
then replaces itself with the runtime command supplied after `--`. No Slurm
libraries need to be mounted into the scientific image:

```bash
PYTHONPATH="$PWD" /absolute/host/python3 -m ecarsi.warm_pool --root "$pool_state" slurm-worker \
  --cpus 0,1 --memory-mb 16384 --work-dir /absolute/shared/worker-directory \
  --job-id 12345 -- \
  apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch \
  --env "PYTHONPATH=$PWD:/opt/rsi-python" /absolute/rsi-cpu.sif /usr/local/bin/python3
```

Replace CPU IDs, memory, job ID, paths and bind mounts with your allocation and
configured runtime. `--job-id` is an optional guard, not a substitute for a real
Slurm cgroup. Direct `worker` is for local execution outside Slurm; inside Slurm
it requires the fresh host profile produced by `slurm-worker`.

CPU locks are shared with legacy RSI. Memory reservations use the existing
per-host, per-job ledger; two Slurm jobs on the same machine keep distinct
memory grants. Stale reservations are removed only after their ownership locks
and recorded process/exit evidence show that the old worker stopped. Uncertain
executors keep their reservation. A worker directory stays bound to its pool
and CPU slice; use a new directory when either changes.

Workers stop 60 seconds before the allocation's recorded end. The remaining
lifetime is passed to native HyperQueue `--time-limit`; each request already
has `--time-request` (at least its execution timeout). HyperQueue can backfill
short tasks while longer tasks wait for a worker with enough time. Reconnecting
does not reset the allocation deadline. See the official
[worker lifetime documentation](https://it4innovations.github.io/hyperqueue/stable/deployment/worker/).
`--time-limit-seconds` can shorten a worker's lifetime for explicit use or tests.
Allocation extensions require relaunching the worker with a fresh probe; they
are not silently assumed. An unexpected early Slurm cancellation remains a
worker-loss case.

Save a request as `/absolute/path/request.json`:

```json
{
  "request_id": "example-1",
  "operation_id": "example-operation",
  "args": ["-c", "from pathlib import Path; Path('result.txt').write_text('complete')"],
  "cpus": 1,
  "memory_mb": 64,
  "timeout_seconds": 30,
  "inputs": [],
  "outputs": ["result.txt"]
}
```

```bash
python -m ecarsi.warm_pool --root "$pool_state" submit /absolute/path/request.json
python -m ecarsi.warm_pool --root "$pool_state" status example-1
python -m ecarsi.warm_pool --root "$pool_state" cancel example-1
```

Arguments are passed directly to the configured runtime, without a shell.
Inputs, when present, require an absolute `path` and `sha256`; outputs must be
relative paths inside the attempt output directory. Input hashes are checked
before the command starts. All declared outputs must exist and be synced before
a successful receipt is published. `time_request_seconds` defaults to the
execution timeout plus 30 seconds and cannot be shorter than that timeout.

Submission returns after the request is durable, even when the Scheduler is
offline. Repeating an identical request ID returns its existing attempt;
changing its content is rejected. Cancellation is also durable and remains
effective without Scheduler RPC. A cancelled result cannot advance a workflow.

## Recovery and diagnosis

Stop or restart the RSI **Scheduler process** using SIGTERM or SIGKILL. Workers
finish accepted work, save receipts, then wait for the replacement Scheduler.
The supervisor deliberately disconnects its HQ server: do not substitute
`hq server stop`, which can cancel running computations. Worker supervisors
reconnect after their old HQ worker has finished, not while it still owns work.
An explicit Worker stop requests cancellation rather than graceful draining.

HQ journal recovery can put previously completed work back in its queue. An
attempt execution lock and durable receipt make that transport redelivery a
no-op; an accepted attempt without a receipt is never blindly rerun. New local
commands also check for surviving process groups left by dead executors before
using their CPUs. A confirmed lost executor produces a failed, retryable receipt.

Completed-attempt scans are cached between scheduler ticks. Replacing
`request.json` for a retry, creating a cancellation, or changing the HQ server
generation invalidates the cache. Thus completed history does not repeatedly
take request locks and reload receipts, while new attempts and journal replay
still receive the normal reconciliation. `scheduler.json` records
`dispatch_scan_seconds`; startup performs a full scan again.

In the 2026-09-15 acceptance, the hot dispatch scan fell from roughly four
seconds to 0.19–0.28 seconds. A request already being submitted during scheduler
replacement completed under its original attempt ID, and the worker rejoined
automatically. The first 11 newly accepted operations had a median queue delay
of 1.01 seconds. These observations concern this small acceptance workload,
not a large-dataset throughput benchmark.
Automatic creation of a new numerical attempt is not implemented in this slice.

All control records are JSON, published with file fsync, atomic rename, and
directory fsync. `status` needs no live server and does not mutate the records.
An execution observation older than 15 seconds is `unknown_external_result`,
not evidence that its resources are free or that retry is safe.

| Location under the pool directory | Contents |
| --- | --- |
| `requests/<request>/request.json` | Immutable request, runtime, and attempt identities |
| `requests/<request>/cancel.json` | Cancellation intent |
| `requests/<request>/backend.json` | Last observed HQ placement/state |
| `requests/<request>/<attempt>/accepted.json` | Host, CPU IDs, executor/process identity |
| `requests/<request>/<attempt>/usage.json` | Latest process-group CPU/RSS sample and peak RSS |
| `requests/<request>/<attempt>/stdout.log`, `stderr.log` | Command output and execution errors |
| `requests/<request>/<attempt>/receipt.json` | Terminal result, timestamps, output sizes/hashes |
| `scheduler.log`, `scheduler.json`, `journal` | Backend logs, supervisor observation, HQ recovery journal |

Worker-local `worker.log` and `worker.json` record the native backend process
and reconnection state. JSON observations are timestamped; old records do not
establish that a process is currently alive. CPU usage is sampled over the
command's process group, including descendants that remain in that group.

## Verification and remaining gates

Run the unit checks and the opt-in real process failure test:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_warm_pool_state.py
python tests/warm_pool_acceptance.py --hq /absolute/path/hq \
  --directory /absolute/durable/path/test-results --cpus 0,1
```

The acceptance test uses exactly two explicitly selected CPUs. It exercises
parallelism, backfilling, single-worker concurrency, idempotent submission and
delivery, Scheduler loss/restart during computation, worker reconnection,
cancellation, timeout cleanup, Worker/executor crashes, and input/output
validation. It records `acceptance.json`, command start counts, receipts, and
logs in its result directory and cleans up its own processes.

The opt-in `tests/warm_pool_multinode.py` additionally checks execution on two
Slurm hosts, the actual job cgroups and assigned CPUs, shared input/output
hashes, cross-host filesystem locking, and Worker reconnection after Scheduler
relocation. It requires SSH access, the same mounted image/code/state paths,
and explicitly reserved CPUs. Run its controller with Python 3.11+ on a host
with SSH and Apptainer installed:

```bash
PYTHONPATH="$PWD" python tests/warm_pool_multinode.py \
  --plan /absolute/path/plan.json --root /absolute/durable/path/fresh-test-pool
```

Example plan (replace all host, allocation and CPU identifiers with your grants):

```json
{
  "hq": "/absolute/path/hq",
  "image": "/absolute/path/python312.sif",
  "host_python": "/absolute/host/python3",
  "nodes": [
    {"host": "node-a", "job_id": "12345", "worker_cpu": 0, "control_cpu": 1},
    {"host": "node-b", "job_id": "12346", "worker_cpu": 0, "control_cpu": 1}
  ]
}
```

The 2026-09-14 trial used jobs `43113441` and `43316407` on `sh02-02n44`
and `sh03-15n05`: five hash-computation tasks each executed once. The two
25-second tasks recorded 24.73 and 24.89 CPU seconds. The Scheduler moved from
the first host to the second, and subsequent work completed on both hosts.
These are recorded test allocations, not reusable current resource assignments.
The test does not automatically provision nodes or modify another pool's budgets.

### Slurm budget and expiry acceptance, 2026-09-15

The three development workers were relaunched through `slurm-worker`, retaining
their explicit 2-CPU/16-GiB test slices. Their observed Slurm allocations were
64 CPU/256 GiB (`43316333`), 32 CPU/64 GiB plus one GPU (`43316407`), and
8 CPU/32 GiB (`43316331`). This milestone advertises CPU resources only; the GPU
allocation does not imply GPU execution. Each worker completed a new test task.
Old legacy ledger entries were reconciled using the existing lock/process
checks; each new slice is now recorded against its actual Slurm job.

An isolated pool used another explicitly budgeted CPU and 256 MiB on the first
node. Its 100-second worker accepted a short task while leaving a task with a
140-second time request queued, then exited without relaunching. A replacement
worker completed the original queued request without resubmission. This tests
the configured lifetime and handoff, not forced Slurm cancellation or physical
node loss. Artifacts are under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/slurm-integration-20260914-234920/`.
The test runner's final cleanup had a formatting error; the exact isolated
test processes were stopped separately and verified in `cleanup.json`.

The repository's two-node recovery test also passed with the Slurm launcher:
both computations finished while the Scheduler was down; the Scheduler then
moved to the other host and all five tasks completed exactly once. Test workers
released their reservations after cleanup. Its report is
`multinode-recovery/acceptance.json` under the same artifact directory.

### GPU scheduling and telemetry, 2026-09-15

A GPU worker owns one explicitly granted GPU UUID and a CPU/RAM slice. For a
local worker use `worker --gpu GPU-...`. Inside an existing Slurm allocation,
launch `slurm-worker --gpu` in a step granted one GPU, for example:

```bash
srun --jobid="$job_id" --overlap --exact --nodes=1 --ntasks=1 \
  --cpus-per-task=2 --mem=20G --gpus-per-task=1 \
  python -m ecarsi.warm_pool --root "$pool_state" slurm-worker \
  --cpus "$cpu_ids" --memory-mb 16384 --work-dir "$worker_state" \
  --job-id "$job_id" --gpu -- \
  apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch \
  --env PYTHONPATH=/path/to/rsi:/opt/rsi-python science.sif /usr/local/bin/python3
```

Select CPU IDs from that step's affinity. The launcher probes its actual Slurm
GPU grant, enables Apptainer NVIDIA support and verifies CuPy/RAPIDS before
registration. No command acquires or releases an allocation. A host-wide UUID
lock prevents two workers from registering the same GPU; uncertain surviving
GPU executions block resource reuse, even across pool roots.

Requests may declare `"gpu": {"mode": "preferred", "memory_mb": 8192}` or
`"required"`. Native HQ resource alternatives prefer GPU and permit CPU fallback
only for `preferred`; plain CPU requests never inherit GPU visibility. Each
GPU task holds the whole device, with a usable VRAM budget of 90% of its capacity.
Concurrent tasks can use the worker's remaining CPUs. Multiple tasks sharing
one GPU are not implemented. The VRAM watchdog samples every 5 seconds and is
not a hard memory partition; brief peaks can be missed.

The live development pool was upgraded to `rsi-science-20260915-1.sif` on three
nodes, including the RTX 3090 (24 GiB) on `sh03-15n05`. A required GPU task,
concurrent CPU fallback, and subsequent preferred GPU task all completed with
the recorded grants. The monitor shows model, VRAM, registered and in-use GPU
counts, plus current and 5-minute GPU/VRAM measurements (30-second samples,
10-second page refresh). Busy counts mean granted tasks, not utilization.

The scientific image contains both CPU and RAPIDS dependencies; its package
and OSP source manifest is [container/science-runtime-20260915.json](container/science-runtime-20260915.json).
Validation records are under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/gpu-integration-20260915-004458`:
`scheduling-acceptance.json`, `science-acceptance.json`, and `monitor-during-gpu.json`.
The real 318-cell comparison preserved the same 235 survivors and 83 exclusions;
CPU/GPU cluster ARI was 0.730, so cluster identity is not treated as interchangeable.
Both backends also passed the optional OSP check at 3 and 80 cells. These small
samples do not establish production-scale GPU speedup.

Before connecting scientific production workflows, the next gates are:

1. Large-sample GPU sizing and throughput validation beyond the tested OSP
   variants. CPU/GPU grant discovery, shared memory reservations and remaining
   worker walltime are implemented; node acquisition remains a user responsibility.
2. Complete application-code identity and control-component images. The current
   scientific image includes OSP and CPU/GPU dependencies; runtime matching is enforced.
3. Host-loss and network-partition recovery. Current evidence covers local
   process failures and Scheduler relocation between two Slurm hosts with a
   shared Lustre state directory; neither host was powered off. Resource limits currently use CPU affinity
   and a sampled process-group RSS watchdog, not hard cgroup isolation.
   Commands must not detach into untracked sessions; surviving uncertain
   processes block reuse rather than being assumed dead.
4. Storage-domain quotas and staging/publication across node-local scratch,
   shared scratch and durable storage, plus bounded attempt retry policy.
5. Cross-sample and Zoom-in integration. Organize and per-sample now connect
   Work Coordinator, Agent Bridge and worker tools; see [per-sample acceptance](PERSAMPLE_V2.md).
   Scientific decisions and convergence remain
   outside the Warm Pool Scheduler.

These are implementation gates, not claims that the new system is already
ready to replace the running production service.

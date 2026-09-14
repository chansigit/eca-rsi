# Warm Pool v2: first implementation

The development package `ecarsi.warm_pool` implements durable, bounded compute
requests over **HyperQueue 0.26.2**. HyperQueue handles CPU/memory placement and
backfilling. RSI stores request identities, cancellation intent, execution
receipts, and output hashes independently of the backend journal.

This is the first local recovery milestone in the
[approved component design](DURABLE_WORKFLOWS.md). It does not route existing
datasets or change the production `ecarsi.pool` service. Commands in the
acceptance test are synthetic; no scientific throughput claim follows from it.

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

Initialization requires a user-owned directory with mode `0700`. Configuration
is immutable for this pool. The runtime defaults to the current Python; its
binary hash and `--version` are checked before a worker joins and before each
execution. This is **not yet a complete container/dependency fingerprint**.

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

Before connecting scientific production workflows, the next gates are:

1. Slurm allocation discovery, remaining walltime, worker drain/replacement,
   CPU/GPU identities and memory budgets across workers on the same host.
2. A pinned scientific image and kernel/code identity; CPU Scanpy and GPU
   RAPIDS operation variants, selected by declared capability.
3. Cross-host recovery and shared filesystem locking/fencing tests. Current
   evidence covers separate local processes on a Lustre state directory, not
   host loss or network partition. Resource limits currently use CPU affinity
   and a sampled process-group RSS watchdog, not hard cgroup isolation.
   Commands must not detach into untracked sessions; surviving uncertain
   processes block reuse rather than being assumed dead.
4. Storage-domain quotas and staging/publication across node-local scratch,
   shared scratch and durable storage, plus bounded attempt retry policy.
5. A real confirmed OSP sample compute, then the asynchronous Work Coordinator
   and Agent Bridge handshake. Scientific decisions and convergence remain
   outside the Warm Pool Scheduler.

These are implementation gates, not claims that the new system is already
ready to replace the running production service.

# Warm pool

`ecarsi.warm_pool` runs durable, bounded compute requests on HyperQueue (HQ). HQ places tasks on workers based on CPU, memory, GPU, and remaining worker time. ECA-RSI stores request identities, cancellation intent, execution receipts, and output hashes in its own files. These files are independent of the HQ journal.

The pool runs the owner's patched HQ build from fork branch `local`. This branch uses upstream main with #1136 and #1137. Both images contain the binary at `/opt/rsi-bin/hq`. Run HQ as a direct native child of the Python supervisor. Run Python and HQ together inside the image. Do not wrap only the HQ command in another container launcher.
## Pool directory

Put the pool state on a durable filesystem.
Make sure every participant mounts that filesystem at the same path.

```bash
python -m ecarsi.warm_pool --root <pool> init --hq /opt/rsi-bin/hq
```

The current user must own the directory.
Set its mode to `0700`.
The backend configuration is immutable for the pool.
`config.json` records the HQ path, the executor and the runtime.

### Runtime

`configure-runtime runtime.json` validates the compute environment.
It selects that environment for new requests.
**Run it inside the compute image.**

The JSON contains `command`, `version` and `files`.
`files` maps absolute paths to SHA-256 values.
The JSON also contains these fields:

- `image`: The absolute `path` and `sha256` of the compute image.
- `pythonpath`: Explicit package directories that replace inherited paths. The deployment uses `["/opt/eca-rsi", "/opt/rsi-control", "/opt/rsi-python"]`.
- `imports`: Modules that must import before a worker registers.

```bash
apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONSAFEPATH=1 \
  --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python <compute image> /usr/local/bin/python3.12 \
  -m ecarsi.warm_pool --root <pool> configure-runtime runtime.json
```

Workers advertise the runtime digest as an HQ resource.
HQ places a request only on a worker with the matching digest.
Each request keeps its original runtime fingerprint.
Changing the runtime does not rewrite old requests.
Changing the runtime does not change a running worker.

To switch images, stop the worker supervisors.
Run `configure-runtime` inside the new image.
Then add the workers again.
Numba uses separate caches for each runtime fingerprint.

One runtime serves every worker, with or without a GPU.
The compute image contains both the CPU and RAPIDS stacks.
Therefore, the pool has a single `runtime` block.
## Workers

The owner requests Slurm nodes. No command acquires or releases an allocation. Use either method below to join a node.

**A Slurm job that is the worker.** Submit `container/worker-node.sh` as the job body. Set `POOL` and `SCIENCE_IMG` in the environment. The job runs `slurm-worker` inside the compute image. It joins the pool by itself. It leaves when the allocation ends.

```bash
POOL=<pool> SCIENCE_IMG=<compute image> sbatch -t 8:00:00 -c 16 --mem=32G <run dir>/image-code/container/worker-node.sh
```

**An allocation that already exists.** Use Python 3.11+ on the host with the eca-rsi code on `PYTHONPATH`. `control-plane.sh host-code` unpacks the code to `<run dir>/image-code`. Run this command:

```bash
python -m ecarsi.warm_pool --root <pool> add-worker <host> --job-id <job> [--cpus 8,12,14] [--memory-mb N] [--wait-seconds 300]
```

`add-worker` connects over SSH. It probes the Slurm cgroup and CPU affinity. It uses 90 % of the smaller Slurm or cgroup memory limit. It reads the image and import paths from the pool configuration. It starts a detached worker. It records `startup.json` and `startup.log` under `worker-state/<host>-<job>/`. It waits for HQ registration. Repeating the command returns the existing registration. Omit the host when you run the command on the allocated node. For `--cpus`, specify CPU ids inside the job's affinity. An allocation lock prevents a second worker on the same host and job.

GPU detection uses the allocation's `AllocTRES`. One worker manages the allocation's CPUs, RAM and all granted GPUs. When GPU visibility is incomplete, `add-worker` starts one `srun` step inside the job with the full GPU grant. It then verifies each GPU's UUID and CuPy or RAPIDS execution before registration. An invalid GPU setup fails visibly.

Workers stop 60 seconds before the allocation's recorded end. HQ's `--time-limit` uses the remaining lifetime. Each request carries `--time-request`. This prevents HQ from placing a task on a worker that cannot finish it. Reconnecting does not reset the deadline. Relaunch the worker after an allocation extension.

`slurm-worker` is the explicit form of the same launch. Run it on the allocated host with the runtime command after `--`:

```bash
python -m ecarsi.warm_pool --root <pool> slurm-worker --cpus 0,1 --memory-mb 16384 --work-dir <shared dir> --job-id 12345 -- \
  apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python <compute image> /usr/local/bin/python3.12
```

Use plain `worker` for local execution outside Slurm. Give same-host workers distinct work directories and disjoint CPU ids. Host-scoped locks enforce these requirements. Memory reservations use a per-host, per-job ledger. A worker directory stays bound to its pool and CPU slice.
## Requests

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
python -m ecarsi.warm_pool --root <pool> submit request.json
python -m ecarsi.warm_pool --root <pool> status example-1
python -m ecarsi.warm_pool --root <pool> cancel example-1
python -m ecarsi.warm_pool --root <pool> retry example-1 --reason "verified repair" [--memory-mb N] [--timeout-seconds N] [--use-current-runtime]
```

Arguments go directly to the runtime without a shell. Give each input an absolute `path` and a `sha256`. Specify output paths relative to the attempt directory. Input hash checks run before the command starts. Receipt publication requires all declared outputs to exist. Synchronization of all declared outputs must finish before receipt publication. `time_request_seconds` defaults to the timeout plus 30 seconds. `time_request_seconds` cannot be shorter than the timeout. A request may declare `"gpu": {"mode": "preferred" | "required", "memory_mb": N}`.

Submission returns after the request becomes durable, even when the scheduler is offline. Repeating an identical request ID returns the existing attempt. Changing request content for an existing request ID causes rejection. Cancellation is durable. A cancelled result cannot advance a workflow. `retry` requires a confirmed failed receipt. It keeps the history. It records a new attempt.
## Scheduling

The scheduler releases every request to HQ on the tick when it sees the request. HQ orders its queue by the priority that the scheduler assigns. The priority equals the class base plus 10 per CPU. The class bases are 1000 for model turns, 800 for session tools, and 0 for batch work. HQ reserves cores for wide tasks itself (#1136). The scheduler has no hold, backlog cap or drain. Set `release.hq_priority: false` in `config.json` to submit requests without priorities.

Before submission, the **feasibility gate** checks whether at least one live worker can hold the request. The worker must have enough CPUs and memory. It must have a GPU when the request requires one. It must also have at least `time_request_seconds` of worker time left. A request that fails this check stays out of HQ. Its observation in `warm_pool status` shows `infeasible: <reason>`. The `release.infeasible` entry in `scheduler.json` counts these requests by reason. The gate checks requests again every tick. This lets a worker that joins later pick up the request. The workflow sees the request as `queued`.

The release layer still decides whether to pin a GPU-preferred task to a GPU. It also decides when to unpin a pinned task that keeps waiting. The release layer gets its timings from the pool's task journals. Run `python -m ecarsi.warm_pool --root <pool> measure [--days 3]` to write `measured.json`. The file contains median and p90 run times per operation on CPU cores and on a GPU. It also contains p90 queue wait. The scheduler runs this command every half hour. The settings `gpu_task_seconds`, `cpu_task_seconds` and `pin_wait_seconds` under `release` in `config.json` override the measurements.

Each `deg-batch` request runs eight DEG comparisons. The request's timeout is twice the per-comparison budget. This timeout lets the request fit a worker with little time left.

Each GPU is a native HQ device resource with its own VRAM resource. A GPU task holds the whole GPU. Its VRAM budget is 90 % of capacity. Plain CPU requests never see a GPU. The VRAM watchdog samples every 5 seconds. It is not a hard partition.
## Recovery

Stop or restart the scheduler process with SIGTERM or SIGKILL. Workers finish accepted work. They save receipts. They then wait for the replacement scheduler process. Do not run `hq server stop`. This command can cancel running computations. Run the HQ server as its own component with `control-plane.sh start hq`. This keeps every worker connected during a scheduler restart.

Worker liveness uses HQ's 8-second heartbeat. A stale heartbeat is an unknown observation, not a worker loss. A task counts as lost only after its allocation ends at `expires_at` plus 300 seconds. A worker may still rejoin while the grant remains active. It may also reconcile during this time. A confirmed lost executor produces a failed, retryable receipt.

HQ journal recovery can return completed work to its queue. The attempt execution lock and the durable receipt make this redelivery a no-op. Recovery never blindly reruns an accepted attempt without a receipt. Each new command checks for surviving process groups of dead executors before it reuses their CPUs. The cache stores terminal attempts under `cache/local-recovery/<host>-<boot-id>.json`.

All control record writes use JSON with fsync, atomic rename and directory fsync. `status` needs no live server. An execution observation older than 15 seconds is `unknown_external_result`.

| Location under the pool directory | Contents |
| --- | --- |
| `requests/<request>/request.json` | Immutable request, runtime and attempt identities |
| `requests/<request>/cancel.json` | Cancellation intent |
| `requests/<request>/backend.json` | Last observed HQ placement and state |
| `requests/<request>/<attempt>/accepted.json` | Host, CPU ids, executor identity |
| `requests/<request>/<attempt>/usage.json` | Latest process-group CPU and RSS sample, peak RSS |
| `requests/<request>/<attempt>/stdout.log`, `stderr.log` | Command output |
| `requests/<request>/<attempt>/receipt.json` | Terminal result, timestamps, output sizes and hashes |
| `scheduler.json`, `scheduler.log`, `measured.json`, `journal` | Scheduler observation and release state, logs, measured timings, HQ journal |
| `worker-state/<host>-<job>/` | `worker.json`, `startup.log`, task journals |
| `by-workflow/<workflow id>.txt` | Request ids per workflow, which the pruner reads |

To prune finished runs, `container/request-pruner.py` lists the requests of completed dataset runs. It then submits `prune-list` as a pool task.
## Checks

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_warm_pool_state.py tests/test_warm_pool_release.py tests/test_warm_pool_measure.py
python tests/warm_pool_acceptance.py --hq /opt/rsi-bin/hq --directory <results> --cpus 0,1
HQ_TEST_BINARY=/opt/rsi-bin/hq python -m pytest tests/test_warm_pool_gpu.py
PYTHONPATH="$PWD" python tests/warm_pool_multinode.py --plan plan.json --root <fresh pool>
```

The acceptance test uses two explicit CPUs. It tests parallelism, backfilling, and idempotent submission. It also tests scheduler loss during computation, worker reconnection, cancellation, timeout cleanup, executor crashes, and input/output validation.

The multinode test requires two Slurm hosts, SSH, and the same mounted paths.

The GPU test uses synthetic GPU descriptors.

Run `warm_pool.replay --root <pool> --day <day>` to replay a day's task journals under a release policy.
## History

See [docs/history/WARM_POOL_ACCEPTANCE_20260915.md](../history/WARM_POOL_ACCEPTANCE_20260915.md) for the September 2026 acceptance records.

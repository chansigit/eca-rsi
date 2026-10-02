# Warm pool

`ecarsi.warm_pool` runs durable, bounded compute requests on HyperQueue (HQ). HQ places tasks on workers by CPU, memory, GPU and remaining worker time. ECA-RSI stores request identities, cancellation intent, execution receipts and output hashes in its own files, independent of the HQ journal.

The pool runs the owner's patched HQ build (fork branch `local`: upstream main with #1136 and #1137). The binary is inside both images at `/opt/rsi-bin/hq`. HQ must be a direct native child of the Python supervisor. Run Python and HQ together inside the image; do not wrap only the HQ command in another container launcher.

## Pool directory

Put the pool state on a durable filesystem that every participant mounts at the same path.

```bash
python -m ecarsi.warm_pool --root <pool> init --hq /opt/rsi-bin/hq
```

The directory must belong to the current user with mode `0700`. The backend configuration is immutable for the pool. `config.json` records the HQ path, the executor and the runtime.

### Runtime

`configure-runtime runtime.json` validates the compute environment and selects it for new requests. **Run it inside the compute image.** The JSON contains `command`, `version`, `files` (absolute path to SHA-256) and:

- `image`: absolute `path` and `sha256` of the compute image;
- `pythonpath`: explicit package directories that replace inherited paths. The deployment uses `["/opt/eca-rsi", "/opt/rsi-control", "/opt/rsi-python"]`;
- `imports`: modules that must import before a worker registers.

```bash
apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONSAFEPATH=1 \
  --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python <compute image> /usr/local/bin/python3.12 \
  -m ecarsi.warm_pool --root <pool> configure-runtime runtime.json
```

Workers advertise the runtime digest as an HQ resource. HQ places a request only on a worker with the matching digest. Each request keeps its original runtime fingerprint. Changing the runtime does not rewrite old requests and does not change a running worker. To switch images: stop the worker supervisors, run `configure-runtime` inside the new image, then add the workers again. Numba caches are separated by runtime fingerprint.

One runtime serves every worker, GPU or not. The compute image carries both the CPU and the RAPIDS stacks, so the pool has a single `runtime` block.

## Workers

The owner requests Slurm nodes. No command acquires or releases an allocation. There are two ways to join a node.

**A Slurm job that is the worker.** Submit `container/worker-node.sh` as the job body. It needs `POOL` and `SCIENCE_IMG` in the environment and runs `slurm-worker` inside the compute image. The job joins the pool by itself and leaves when the allocation ends.

```bash
POOL=<pool> SCIENCE_IMG=<compute image> sbatch -t 8:00:00 -c 16 --mem=32G <run dir>/image-code/container/worker-node.sh
```

**An allocation that already exists.** From a host Python 3.11+ with the eca-rsi code on `PYTHONPATH` (`control-plane.sh host-code` unpacks it to `<run dir>/image-code`):

```bash
python -m ecarsi.warm_pool --root <pool> add-worker <host> --job-id <job> [--cpus 8,12,14] [--memory-mb N] [--wait-seconds 300]
```

`add-worker` connects over SSH, probes the Slurm cgroup and CPU affinity, uses 90 % of the smaller Slurm or cgroup memory limit, and reads the image and import paths from the pool configuration. It starts the worker detached, records `startup.json` and `startup.log` under `worker-state/<host>-<job>/`, and waits for HQ registration. Repeating the command returns the existing registration. Omit the host when you run it on the allocated node. `--cpus` must name CPU ids inside the job's affinity. An allocation lock prevents a second worker on the same host and job.

GPU detection uses the allocation's `AllocTRES`. One worker manages the allocation's CPUs, RAM and all granted GPUs. When GPU visibility is incomplete, `add-worker` starts one `srun` step with the full GPU grant inside the job, then verifies each GPU's UUID and CuPy or RAPIDS execution before registration. An invalid GPU setup fails visibly.

Workers stop 60 seconds before the allocation's recorded end. The remaining lifetime goes to HQ's `--time-limit`. Each request carries `--time-request`, so HQ never places a task on a worker that cannot finish it. Reconnecting does not reset the deadline. Allocation extensions need a relaunched worker.

`slurm-worker` is the explicit form of the same launch. Run it on the allocated host with the runtime command after `--`:

```bash
python -m ecarsi.warm_pool --root <pool> slurm-worker --cpus 0,1 --memory-mb 16384 --work-dir <shared dir> --job-id 12345 -- \
  apptainer exec --cleanenv --bind /scratch,/oak,/home,/lscratch --env PYTHONPATH=/opt/eca-rsi:/opt/rsi-control:/opt/rsi-python <compute image> /usr/local/bin/python3.12
```

Plain `worker` is for local execution outside Slurm. Same-host workers need distinct work directories and disjoint CPU ids; host-scoped locks enforce this. Memory reservations use a per-host, per-job ledger. A worker directory stays bound to its pool and CPU slice.

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

Arguments go to the runtime without a shell. Inputs need an absolute `path` and `sha256`. Outputs are paths relative to the attempt directory. Input hashes are checked before the command starts. All declared outputs must exist and be synced before a receipt is published. `time_request_seconds` defaults to the timeout plus 30 seconds and cannot be shorter than the timeout. A request may declare `"gpu": {"mode": "preferred" | "required", "memory_mb": N}`.

Submission returns after the request is durable, even when the scheduler is offline. Repeating an identical request id returns the existing attempt; changed content is rejected. Cancellation is durable. A cancelled result cannot advance a workflow. `retry` needs a confirmed failed receipt; it keeps the history and records a new attempt.

## Scheduling

The scheduler releases every request to HQ on the tick it sees it. HQ orders its queue by the priority the scheduler attaches: class base (model turn 1000, session tool 800, batch work 0) plus 10 per CPU. HQ reserves cores for wide tasks itself (#1136). There is no scheduler-side hold, backlog cap or drain. `release.hq_priority: false` in `config.json` submits without priorities.

Before submission, the **feasibility gate** checks that some live worker can hold the request: enough CPUs and memory, a GPU when required, and at least `time_request_seconds` of worker time left. A request that fails stays out of HQ with `infeasible: <reason>` in its observation (`warm_pool status`) and is counted by reason under `release.infeasible` in `scheduler.json`. The gate re-checks every tick, so a worker that joins later picks the request up. The workflow sees the request as `queued`.

The release layer still decides whether a GPU-preferred task is pinned to a card, and when a pinned task that keeps waiting is unpinned. Its timings come from the pool's task journals: `python -m ecarsi.warm_pool --root <pool> measure [--days 3]` writes `measured.json` with median and p90 run times per operation, on cores and on a card, plus p90 queue wait. The scheduler runs it every half hour. Knobs under `release` in `config.json` (`gpu_task_seconds`, `cpu_task_seconds`, `pin_wait_seconds`) override the measurements.

DEG comparisons run eight per request (`deg-batch`). The request's timeout is twice the per-comparison budget, so it fits a worker with little time left.

Each GPU is a native HQ device resource with its own VRAM resource. A GPU task holds the whole device with a VRAM budget of 90 % of capacity. Plain CPU requests never see a GPU. The VRAM watchdog samples every 5 seconds; it is not a hard partition.

## Recovery

Stop or restart the scheduler process with SIGTERM or SIGKILL. Workers finish accepted work, save receipts and wait for the replacement. Do not run `hq server stop`; it can cancel running computations. Run the HQ server as its own component (`control-plane.sh start hq`), so a scheduler restart keeps every worker connected.

Worker liveness uses HQ's 8-second heartbeat. A stale heartbeat is an unknown observation, not a worker loss. A task counts as lost only when its allocation has ended (`expires_at` plus 300 seconds), because a worker may still rejoin and reconcile while the grant lives. A confirmed lost executor produces a failed, retryable receipt.

HQ journal recovery can put completed work back in its queue. The attempt execution lock and the durable receipt make that redelivery a no-op. An accepted attempt without a receipt is never rerun blindly. Each new command checks for surviving process groups of dead executors before reusing their CPUs. Terminal attempts are cached under `cache/local-recovery/<host>-<boot-id>.json`.

All control records are JSON written with fsync, atomic rename and directory fsync. `status` needs no live server. An execution observation older than 15 seconds is `unknown_external_result`.

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
| `by-workflow/<workflow id>.txt` | Request ids per workflow, read by the pruner |

Finished runs are pruned: `container/request-pruner.py` lists the requests of completed dataset runs and submits `prune-list` as a pool task.

## Checks

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests/test_warm_pool_state.py tests/test_warm_pool_release.py tests/test_warm_pool_measure.py
python tests/warm_pool_acceptance.py --hq /opt/rsi-bin/hq --directory <results> --cpus 0,1
HQ_TEST_BINARY=/opt/rsi-bin/hq python -m pytest tests/test_warm_pool_gpu.py
PYTHONPATH="$PWD" python tests/warm_pool_multinode.py --plan plan.json --root <fresh pool>
```

The acceptance test uses two explicit CPUs and exercises parallelism, backfilling, idempotent submission, scheduler loss during computation, worker reconnection, cancellation, timeout cleanup, executor crashes and input/output validation. The multinode test needs two Slurm hosts, SSH and the same mounted paths. The GPU test uses synthetic GPU descriptors. `warm_pool.replay --root <pool> --day <day>` replays a day's task journals under a release policy.

## History

Acceptance records of September 2026 are in [docs/history/WARM_POOL_ACCEPTANCE_20260915.md](../history/WARM_POOL_ACCEPTANCE_20260915.md).

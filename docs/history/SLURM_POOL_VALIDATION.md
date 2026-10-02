# Slurm pool validation — 2026-09-12

Release: ECA-RSI 0.3.0 and MSP 0.5.2. The records below distinguish
the initial two-worker validation from the subsequent four-node deployment.

## Checks

- Release regression: RSI 235 passed, 2 expected skips; MSP 232 passed.
  Node-backed checks were enabled. MSP lint and formatting passed.
- New policy checks cover FIFO/first-fit, incompatible CPU/memory/GPU/runtime/
  paths/time, duplicate resource claims, draining, expiry, worker loss and grant
  replay rejection. Two real Dask clients exercise the shared scheduler.
- OSP adapter checks cover isolated attempts, driver-owned publication, failure
  QC records and the existing compute-checkpoint/annotation-only recovery path.
- Release wheels and source archives passed `twine check`. All 36 RSI Python
  files (including 6 pool files) and 20 MSP files match the release source.
  Release checks and artifacts are under
  `/scratch/users/chensj16/eca-runs/slurm-pool-release-20260912/`.


## Actual Slurm execution

Scheduler on `sh02-02n44`; workers on `sh03-08n39`, existing job `42933929`.
Two worker processes each used one disjoint CPU and an 8 GiB memory profile.
No new allocation was requested or released. The existing production Dask pool
remained separate and was idle when test capacity was selected.

- Two independent array clients computed remotely, with verified hostname,
  CPU affinity, one native thread and exact numerical results.
- Adding the second worker let it take a queued OSP sample.
- Two real OSP samples: 268 + 63 input cells; 250 + 61 retained; 18 + 2 removed.
  Counts-layer presence, input identity, output receipts and cell conservation
  passed. Scrublet, DecontX and model annotation were disabled in this smoke run;
  the adapter forwards their existing configuration unchanged.
- Killing an active worker caused a visible `ConnectionError` for its request.
  Nanny restarted the process, the replacement registered, and new work succeeded.
- Draining stopped admission. Cancelling an already running request returned
  false and held its reservation until execution finished. Disconnecting the
  driver also retained the reservation until the remote function finished.
- MSP's actual neighbors/Leiden/UMAP kernel ran through `MSP_COMPUTE_ENDPOINT=pool`
  on 120 cells and returned a valid 120 × 2 embedding and cluster labels.

Final-source evidence is under
`/scratch/users/chensj16/eca-runs/slurm-pool-validation-20260912/verified/`:
`osp-verified.json`, `remote-arrays.json`, `faults-verified.json`,
`driver-disconnect.json` and `status-final.json`. Its parent holds test logs,
the wheel and `wheel-verified.json`. Earlier trial directories are retained.
Only the isolated test pool processes were stopped after verification.

The subsequent four-node deployment passed independent CPU calls on all four
hosts and a CuPy GPU computation on an RTX 3090. It used existing allocations
43173336, 43173344, 42933929 and 43173346, excluding cpu-session. The inventory
matched 98 allocated CPUs, 512 GiB Slurm memory and one GPU; worker memory
budgets total 448 GiB. Slurm requested/allocated CPU differences follow the
normal partition's MaxMemPerCPU=8000 MiB constraint. Task occupancy and measured
CPU/RSS/GPU status were checked while busy and idle. Evidence is in
`/scratch/users/chensj16/dask-pool/slurm-20260912/verified.json` and `resources.json`.
GPU Dask/NumPy versions were aligned through a pool-local overlay; the base GPU
environment was not changed. This tests CUDA execution, not a full RAPIDS pipeline.

Time-expiry admission is tested at the policy level;
the live run refreshed real Slurm deadlines but did not wait for a job to expire.
Queue state is deliberately not durable across scheduler restart: resume via
driver checkpoints. Runtime/array-size estimates are coarse and configurable.

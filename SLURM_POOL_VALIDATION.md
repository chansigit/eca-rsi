# Slurm pool validation — 2026-09-12

Implementation is on the `slurm-pool` branches of ECA-RSI and MSP. No package
version bump, main merge or release is part of this change.

## Checks

- RSI: 234 passed, 2 expected skips. The full suite first ran with 229 passed
  and 7 skips; the 5 browser-JavaScript checks were then enabled with the
  container's Node PATH and passed. The remaining skips concern a first
  publication having no previous release directory.
- MSP compute endpoints: 18 passed, including the existing local and Dask modes.
- New policy checks cover FIFO/first-fit, incompatible CPU/memory/GPU/runtime/
  paths/time, duplicate resource claims, draining, expiry, worker loss and grant
  replay rejection. Two real Dask clients exercise the shared scheduler.
- OSP adapter checks cover isolated attempts, driver-owned publication, failure
  QC records and the existing compute-checkpoint/annotation-only recovery path.
- Wheel build and `twine check` passed; all 35 packaged Python files match source,
  including the 5 new pool files. The wheel is a validation artifact, not a release.

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

GPU execution and two distinct worker hosts still need validation when those
allocations are available. Time-expiry admission is tested at the policy level;
the live run refreshed real Slurm deadlines but did not wait for a job to expire.
Queue state is deliberately not durable across scheduler restart: resume via
driver checkpoints. Runtime/array-size estimates are coarse and configurable.

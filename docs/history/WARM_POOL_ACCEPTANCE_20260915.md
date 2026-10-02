# Warm pool acceptance records — 2026-09-14/15

Moved from `docs/control-plane/WARM_POOL_V2.md` on 2026-10-02. Historical record: hosts, images, paths and limits are those of September 2026.

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

## GPU scheduling and telemetry, 2026-09-15

The live development pool was upgraded to `rsi-science-20260915-1.sif` on three
nodes, including the RTX 3090 (24 GiB) on `sh03-15n05`. A required GPU task,
concurrent CPU fallback, and subsequent preferred GPU task all completed with
the recorded grants. The monitor shows model, VRAM, registered and in-use GPU
counts, plus current and 5-minute GPU/VRAM measurements (30-second samples,
10-second page refresh). Busy counts mean granted tasks, not utilization.

The scientific image contains both CPU and RAPIDS dependencies; its package
and OSP source manifest is [container/science-runtime-20260915.json](../../container/science-runtime-20260915.json).
Validation records are under
`/scratch/users/chensj16/eca-runs/warmpool-v2-development/gpu-integration-20260915-004458`:
`scheduling-acceptance.json`, `science-acceptance.json`, and `monitor-during-gpu.json`.
The real 318-cell comparison preserved the same 235 survivors and 83 exclusions;
CPU/GPU cluster ARI was 0.730, so cluster identity is not treated as interchangeable.
Both backends also passed the optional OSP check at 3 and 80 cells. These small
samples do not establish production-scale GPU speedup.

## Recovery scan and dispatch timings, 2026-09-15

During the 2026-09-15 live batch, the first 13 completed commands on sh04-01n13
after this change had a median recovery check of 3.83 seconds, compared with
20.92 seconds for 61 preceding commands. This measures the recovery scan only,
not end-to-end pipeline acceleration. Tests cover cache invalidation on retry
and a real killed executor whose surviving child must keep its CPUs reserved.
In the 2026-09-15 acceptance, the hot dispatch scan fell from roughly four
seconds to 0.19–0.28 seconds. A request already being submitted during scheduler
replacement completed under its original attempt ID, and the worker rejoined
automatically. The first 11 newly accepted operations had a median queue delay
of 1.01 seconds. These observations concern this small acceptance workload,
not a large-dataset throughput benchmark.

## Gates listed before scientific production (2026-09-15)

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
   Work Coordinator, Agent Bridge and worker tools; see [per-sample acceptance](../control-plane/PERSAMPLE_V2.md).
   Scientific decisions and convergence remain
   outside the Warm Pool Scheduler.

These are implementation gates, not claims that the new system is already
ready to replace the running production service.

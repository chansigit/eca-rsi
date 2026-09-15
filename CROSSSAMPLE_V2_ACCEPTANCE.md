# Cross-sample acceptance — 2026-09-15

Two real per-sample publications completed the Temporal cross-sample workflow, including inclusion, integration, independent DEG, type annotation, quality review, full report generation and exact cell accounting. This validates small-data execution and recovery; it is not a large-data throughput benchmark.

| Dataset | Input cells | Surviving cells | Excluded cells | Integration | Independent DEG tasks |
| --- | ---: | ---: | ---: | --- | ---: |
| Prostate | 507 | 502 | 5 | RAPIDS, RTX 3090 | 31 |
| Uterus | 441 | 291 | 150 | CPU | 22 |

The 53 DEG tasks reached five concurrent executions across three hosts. Uterus integration overlapped Prostate model waits. Prostate integration took 31.43 seconds with 345 MiB peak GPU memory; Uterus took 44.11 seconds on CPU. Different inputs make these timings unsuitable for a GPU speedup comparison.

Uterus exclusions were audited against the saved decisions. Of its 150 unique excluded cells, 82 were covered by quality decisions (53 low-quality and 29 stress), 59 by numerical cell-outlier rules, and 21 by upstream OSP proposals. These sets overlap. Every exclusion has its original cell identity and evidence references; the audit establishes execution/accounting consistency, not independent biological confirmation of every model judgment.

## Recovery and validation

The Coordinator was stopped for 12 seconds during Uterus integration. Worker CPU ticks advanced from 124 to 1314 while it was absent; computation continued and DEG dispatch resumed after restart. A later model call omitted a required argument. The shared adapter now returns a recorded validation error without executing the invalid program, preserves tool state, and lets the model correct its call. Uterus resumed from its accepted computation/type results and completed.

The science image contains the MSP, OSP and ZMIP kernels. A Scanpy in-place sparse-matrix operation required private CSR buffers; the shared DEG kernel was corrected. Full report validation also caught the new boolean sample-inclusion format, which the shared MSP report reader now accepts alongside its legacy format. All active development workers were upgraded after draining their tasks, preserving their Slurm allocations.

Backed reads of the final H5AD files verified unique cell IDs, exact input = survivors + exclusions, nonempty labels and original source identities. Every published artifact passed SHA-256 verification. Relevant checks include real full-report generation, sparse DEG numerical equivalence, durable tool-error recovery and workflow ordering. Historical failed attempts remain available for audit.

## Reproducing the evidence

Acceptance directory:

```text
/scratch/users/chensj16/eca-runs/warmpool-v2-development/crosssample-migration-20260915-015647
```

`final-acceptance.json` identifies both verified publications and ledgers. `numerical-acceptance.json`, `coordinator-live-restart.json`, `argument-recovery.json`, saved workflow histories and test logs provide the supporting records. Workflow IDs are `cross-sample/cross-prostate-20260915-0219-r3` and `cross-sample/cross-uterus-20260915-0219-r4`.

Implementation commits: RSI `7a85852`, MSP `474b135`, ZMIP `7df5201`. Final science runtime digest: `c08ef3bf80f4f2d917fd2155bc212f440f2c2eb199fcb6c661c89284bf347c5c` (`rsi-science-20260915-5.sif`). Earlier numerical receipts retain their original runtime pins.

Remaining production gates include Temporal Service storage survival across node expiry, large-data resource calibration, all-empty/all-excluded terminal publication, and reducing long agent-session history growth. Old bulk production remains paused.

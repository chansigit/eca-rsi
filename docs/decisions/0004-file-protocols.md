# 0004 Subsystems talk through request folders on shared storage

Accepted, 2026-09.

**Context.** The workers are Slurm jobs on other nodes that come and go; nothing can listen for connections
between them, and every step must be inspectable after the fact.

**Decision.** The pool and the agent service are directories of request folders on shared storage: a
`request.json` in, attempts with outputs and a receipt out. Coordinators submit by writing a folder and poll
for the receipt. The request id is the replay key: submitting an existing id replays the stored result.

**Consequences.** Every request can be read on disk, and a resumed run reuses finished requests instead of
recomputing them. The price is polling latency and filesystem load: never scan `pool/requests` or
`bridge/requests` unpaced on the control-plane node (an unpaced scan of 99k folders stalled its Lustre client
on 2026-09-17), and a pruner deletes the requests of finished runs. Code: `ecarsi/warm_pool/state.py`,
`ecarsi/agent/__init__.py`, `container/request-pruner.py`.

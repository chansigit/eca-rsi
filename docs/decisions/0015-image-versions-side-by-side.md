# 0015 Two image versions side by side (proposed, not built)

Proposed, 2026-10-02. Needs the owner's go-ahead before any of it is built.

**Context.** A new image pair can only be switched to at zero running executions (0005, 0010): every request
pins its program files by content, every agent turn pins `session.py` / `plan.py`, and workflow code must
replay. With long datasets that means waiting for a batch to drain before any fix ships.

**Proposal.** Run the old and the new pair at once, each serving the work it started, until the old one drains.
The pieces mostly exist:

- **Pool.** Requests already ask HQ for a `runtime/<digest>` resource and a worker offers only its own image's
  digest, so a request can only run on a worker of its own image. Missing: `submit` takes the pool's one
  `config.runtime`; it would take the submitter's (`runtimes: {stamp: runtime}`, the coordinator names its stamp
  from `/opt/eca-rsi/BUILD.json`). Old-image workers stay up until no request of their digest is queued.
- **Orchestration.** temporalio 1.32 has worker deployment versioning: coordinators register with version = image
  stamp, running workflows stay pinned to the version that started them, new ones go to the current version.
  This replaces the replay check as the guard for workflow changes.
- **Agent service.** Turns check the adapter file against the bridge's own copy. Either one bridge and runner set
  per version (separate request folders), or the runner executes the turn's pinned adapter from the request.
- **Switch.** `switch-images.sh` becomes: start the new version's coordinators, bridge and workers next to the
  old; make it current; stop the old version when its executions and requests are gone.

**Cost and risk.** Touches the pool's submit path, the coordinator start-up, the bridge and the switch script,
all production machinery; each part needs its own test and a gate run (`ops/gate.py`) with both versions live.
Until then: build, switch at zero executions, gate, keep the previous pair for a switch back.

# 0006 HyperQueue orders the work; the owner requests the workers

Accepted, 2026-09-27 (54f1f6b, 16412ad).

**Context.** Until 2026-09-27 the scheduler kept its own hold, backlog cap and drain layer in front of
HyperQueue. A replay of a real day (2026-09-24: 352k tasks, 13 workers) through alternative rules finished
every dataset in the same time (span ratio 1.01–1.02); only interactive waits differed.

**Decision.** Every request goes to HQ at once with a native priority: class base (model turn 1000, session
tool 800, batch work 0) plus 10 × cpus. A feasibility gate keeps back a request no live worker can hold
(cpus, memory, GPU, time left) and marks it `infeasible: <reason>`. The scheduler's own queueing was deleted.
Workers are Slurm jobs the owner requests; there is no autoscaler.

**Consequences.** Class-first priority cut the p90 agent/tool wait from 95 s to 2 s. Priorities need the
owner's patched HQ (upstream bug It4innovations/hyperqueue#1135). Capacity is planned by hand.
Code: `ecarsi/warm_pool/backend.py` (`hq_priority`, `infeasible`), `ecarsi/warm_pool/replay.py`.

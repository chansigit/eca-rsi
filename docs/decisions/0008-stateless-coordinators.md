# 0008 Four stateless coordinators with two workflow slots each

Accepted, 2026-09.

**Context.** Coordinators run Python workflow code, one interpreter lock per process. Replaying a long history
is CPU-bound: a cold recovery of 14,000 events missed the 10 s workflow-task deadline when several replays
shared one process. Coordinators also died (OOM on a 32 GB plane job, 2026-09-23/24).

**Decision.** Coordinators keep no state of their own; all of it is in Temporal. Several processes poll the
same task queue (`COORDINATORS`, default 4), each with two concurrent workflow tasks (`--workflow-slots 2`;
one slot left sticky continuations waiting about 10 s). Any coordinator can run any step of any dataset.

**Consequences.** Replays use several cores; a dead coordinator's work moves to the others and the launcher
tops the count back up. Four is a default sized to the 96 GB control node (6–7 GB each), not a measured
optimum: raise it when replay is CPU-bound. The slot count does not limit how many datasets run.
Code: `ecarsi/control/coordinator.py` (`run_worker`), `container/control-plane.sh`.

# 0007 Model turns run in their own service, not in the compute pool

Accepted, 2026-09-24 (resident runners).

**Context.** A model turn waits seconds to minutes on a provider and uses almost no CPU. Run as pool tasks,
turns held worker slots while waiting, and the turn concurrency cap, not compute, limited throughput (724 h of
turns queued against 87 h at the provider, 2026-09-17).

**Decision.** The agent service (directory `bridge/`) queues model turns. Resident runners, one process per
catalog model, keep many turns in flight in one event loop. The catalog `~/.config/ecarsi/models.json`
orders the models; a busy or failing model lets the next one take the turn. Session tools (reading evidence,
DEG lookups) stay pool tasks of the tool class.

**Consequences.** Model throughput no longer competes with computation. The service is one more component to
run, and provider keys live in `~/.bashrc`. Code: `ecarsi/agent/` (`runner`, `dispatch`, `session`).

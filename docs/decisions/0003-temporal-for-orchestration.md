# 0003 Temporal runs the orchestration

Accepted, 2026-09-18 (the `gen2` branch, ecarsi 0.3.1).

**Context.** A dataset runs for hours to days. The control-plane node is a Slurm job of about two days that
ends and moves to another node. Earlier batch drivers lost their place when their process died.

**Decision.** Each dataset is a Temporal workflow tree: dataset → unit → per-sample / cross-sample / zoom-in →
agent session. Temporal Server and PostgreSQL run from the control image and keep all workflow state. The
coordinators are workers that execute workflow code by replaying its history.

**Consequences.** A run survives coordinator restarts and moves of the plane (start the components on the new
node; workflows continue). Workflow code must stay deterministic: run `ops/replay-check.py` before deploying a
change under `ecarsi/control/`. Long histories are slow to replay, so cross-sample and zoom-in continue as new
past 5,000 events, and replay is CPU-bound (see 0008). Code: `ecarsi/control/`, `ecarsi/control/temporal.py`.

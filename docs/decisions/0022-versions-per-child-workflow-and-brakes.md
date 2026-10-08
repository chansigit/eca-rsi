# 0022 A running dataset moves to the current version at every child workflow; four brakes

Decided 2026-10-08 by the owner (#61).

**Context.** 0019 kept every execution on the version that started it, so a dataset of forty rounds ran forty rounds
on day-one code while a fix was current, and a drained version had to keep its coordinators for its last dataset.
The owner relaxed the rule: the parts of a run may come from different versions, as long as each part says which.
The owner also asked for a brake finer than the round and stage ones, and for a formal hard one: stopping the glm
comparison runs had meant a manual Temporal terminate.

**Decision.**

- **Every child workflow starts on the current version.** Before a parent starts a child -- the dataset its units,
  a unit its per-sample, cross-sample and zoom-in stages, the per-sample stage each sample -- the `before_child`
  activity reads `versions/current` and, when that version's queue has a coordinator polling it, answers its queue;
  `common.start_child` starts the child there. Otherwise the child inherits its parent's queue, as Temporal does by
  default. Nothing finer moves: a stage's activities and pool tasks share one version's file contracts, and an
  agent session keeps one prompt and tool set. `resume-dataset` resumes on the current version for the same reason.
- **Each record names its version.** The per-sample publication and every round record carry `version`; a unit
  publication and the dataset publication list `versions`, every version that ran a part.
- **The brakes**, coarse to fine, in `<unit>/loop_control.json` under one key: `brake: round` (after this round;
  the old `pause` and `stop_after_round` are aliases), `brake: stage` (after the stage now running; the old
  `pause_after_stage`), `brake: step` (before the next child workflow or the release: no new sample, stage or
  round starts, the ones running finish). Each ends the unit as `PAUSED: ...`, resumable with `resume-dataset`. The
  **hard brake** is a command, `brake <run_id> --hard --reason ...`: the dataset's workflows are terminated now,
  pool tasks already on a worker run out and are discarded, every directory stays, `resume-dataset` continues.
  `warm_pool cancel` remains the kill: pool tasks killed, nothing resumable.

**What it costs.** `start_child` is the one place workflow code passes a task queue (tests/test_versions.py allows
it by name). A version is retired when nothing runs on its queue, as before; a running dataset leaves it at its next
child. The gate stays one run on one version: a run started on A and finished on B is the check of this decision.

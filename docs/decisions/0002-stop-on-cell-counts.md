# 0002 A unit stops on cell counts only

Accepted, 2026-09; the absolute floor was added after E12.5/E13.5.

**Context.** Labels keep changing wording from round to round, so "the labels stopped changing" never
arrives. Relative thresholds alone let a 400k-cell unit release while still dropping thousands of cells.

**Decision.** In automatic mode a unit releases from round 2 on when the round removed < 1 % or < 100 cells,
or three rounds in a row removed < 2 %, and in either case the round removed < 1,000 cells (`max_removed`).
`cap` (default 15) forces a flagged release. A fixed `rounds` overrides all of it. `loop_control.json`
changes any of these while the run goes. Cumulative removal across rounds is not a criterion and not a risk.

**Consequences.** Stopping is checkable and the same for every dataset. A converged release says nothing about
biological accuracy; that is what `needs_review` is for. Code: `ecarsi/round_policy.py`.

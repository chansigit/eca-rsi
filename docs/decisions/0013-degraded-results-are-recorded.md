# 0013 A step that fails without failing the run leaves a record

Accepted, 2026-10-02.

**Context.** Some steps must never fail a run: a lineage report, a copy of a stage's readable files, a
display sync, a round's ledger report. Their failures used to be one line in a log. The zoom-in lineage
reports did not draw from 2026-09-20 until 2026-10-02, and a race in the final display sync went unseen,
because nobody reads coordinator and pool logs (#26).

**Decision.** Such a step calls `ecarsi.degraded.note` in its `except` block. Stage programs keep the notes
in their result (`final.json` key `degraded`); the control plane copies them, and records its own failures,
as one small file each under `<run>/degraded/` (and in the display zone directly, since a failed final sync
cannot carry them there). Release lists them first in `needs_review` (category `degraded`). Periscope marks
the unit row "N degraded" and lists them on the dataset page. With `ECARSI_STRICT=1`, which the test suite
sets, `note` re-raises: a degradation in a test is a failure.

**Consequences.** A run still never fails over a report, but the owner sees the failure on the run's row the
same day. Strict mode covers the tests only; a real run is never strict. A degradation recorded after release
(the final sync) appears on Periscope but not in that release's `needs_review`. Session restarts and skipped
samples or lineages are not degradations: they have their own `needs_review` categories. Code:
`ecarsi/degraded.py`.

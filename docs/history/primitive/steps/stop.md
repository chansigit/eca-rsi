# Step: stop — decide whether the loop continues

This pipeline is fully autonomous: it never stops to wait for a human.
Anything unresolved becomes a **flag in the final output**, not a blocker.

Compare this round against the previous ones (their reports are on disk) and
make one decision: **continue** or **release**.

Your decision goes in **two places**:

1. `decision.txt` in the round dir — containing exactly one lowercase word,
   `continue` or `release`, and nothing else. This is what the runner reads;
   prose here breaks the loop.
2. `stop.md` — the reasoning (see Report below).

## The decision

- **continue** — actionable work remains: cells were removed this round,
  labels changed materially, or there are open items (splits, postpones,
  "not executed" entries from apply, and flags **that carry a resolving
  test**) that another round can realistically resolve. A flag without a
  resolving test is not an open item — it goes to the "needs review" section
  and never blocks release.
- **release** — converged, meaning ALL of:
  - (a) this round removed **fewer than 1% of the current cells** — count it,
    don't estimate; a round that removed 7.6% was once declared "almost none".
    Any larger removal changes the feature space and mandates one more look
    with fresh eyes, whatever else is true.
  - (b) labels are stable vs the previous round (same populations, same
    names). Round 1 can never release — stability against a previous round is
    part of the definition.
  - (c) no *actionable* open items remain. An item attempted in two rounds
    without resolution is no longer actionable: convert it to a "needs
    review" entry in the summary and let it ride into the release instead of
    blocking it.

**Final round** (the context header says the round cap): if this is the last
allowed round, do not write continue — release what exists. Everything
unresolved goes into the "needs review" section; an imperfect release with
honest flags beats an expired loop with nothing consolidated.

Concerns that would once have paused the loop — removal budget crossed,
decisions apply could not execute, inconsistencies you cannot resolve —
do **not** stop anything. Record each one prominently in the reports and
make sure it survives into the release summary's "needs review" section,
then keep going.

If releasing: create `release/` in the workspace — copy the final
`checkpoint.h5ad` there as `annotated.h5ad`, export the per-cell obs table as
`percell.csv.gz`. On a **reopened** run (the loop was continued past an
earlier release with `--force-reopen`), a `release/` already exists: update
its files in place with the new state, and state in `summary.md` that this
release supersedes the earlier one, which rounds were added, and why the
loop was reopened. No figures are duplicated into the release: the final
round's fixed figure set (`umap_*.png`, `lineage_*.png` in its round dir) is
the visual record — point to it from the summary. Then write `summary.md`
(what the dataset is, rounds run,
cells in/out, final populations with labels and sizes, everything removed and
why, and a **"needs review"** section listing every remaining flag and
unresolved concern with what a reviewer should look at).

## Report (`stop.md`)

State the decision, then the three convergence criteria with what you
actually observed for each — for (a) the actual removal count and percentage
— the open-items list (marking which are still actionable vs converted to
flags), and — if continuing — the one or two things the next round should
attack first. Write `decision.txt` last, after the report exists.

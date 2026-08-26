# Step: qc — quality verdicts on this round's clusters and cells

Method: `docs/RULES_data_cleaning.md`. Read what you use (noise classes:
R10; doublets: R1/R8; states are not grounds: R9; small clusters: R11) —
don't recite from memory.

The core discipline, in one paragraph: cells are presumed biological until
proven noise. A removal must (a) name a noise class, (b) cite evidence, and
(c) defeat the plausible biological alternatives — stress, cycling,
quiescence, an unusual but real cell type are *states or identities*, not
grounds. Whole-cluster removal needs a pervasive flaw; a bad tail inside a
coherent cluster is removed per-cell by an explicit criterion, not by
condemning the cluster. When in doubt, flag — but a flag is a question for
the next round, so it must state **the concrete test that would resolve it**.
A flag with no resolving test is not a question anyone can answer: it does
not count as an open item, does not block convergence, and goes straight to
the release summary's "needs review" section instead. (Precedent: four
borderline-hemoglobin cells were flagged without a test and re-inspected
three rounds in a row with zero new evidence — that is what this rule
prevents.)

You have this round's annotation (`annotate.md`): use it — a "doublet-like"
cluster whose markers are a coherent single program is the classic false
positive. Probe freely with your own scripts; save code + output as evidence.

**Run `standissect-lite` every round** (installed in the python env; `import
standissect`) on the current clustering: it surfaces candidate
tiny/fragmented subclusters with QC drift. Its output is a candidate list,
not a verdict — every candidate still goes through R11's five tests. Save
its outputs in the round dir and cite them like any other evidence.

Also check the budget: report cumulative removal across all rounds (original
cell count vs current). Per-round removal above ~10%, or cumulative drifting
past ~30%, does not stop anything — the automatic response is to get
*conservative*: past the budget, downgrade further borderline removals to
flags, and raise a durable `flag_removal_budget_exceeded` note in your report
so it reaches the release summary's "needs review" section.

Review standing flags from earlier rounds if any exist: resolve (clear or
escalate) the ones whose question this round's evidence can answer. Flags
that nobody ever revisits are how uncertainty silently accumulates.

## Report (`qc.md`)

Per cluster: retain / flag (name + what would resolve it) / remove (noise
class + evidence + why the biological alternatives fail). Per-cell removals:
the exact criterion (column, threshold, which clusters it applies to) and the
count it captures. Then the budget line: removed this round (planned),
cumulative %, and — if a limit is crossed — the budget flag plus which
borderline removals you downgraded to flags because of it.

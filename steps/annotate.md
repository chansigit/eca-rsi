# Step: annotate — cell type labels for this round's clusters

Method: `docs/RULES_annotation.md`. Read the rules you actually use this
round (marker evidence: A2/A3; type vs state: A4; when to split or merge:
A6/A12; naming: A7) — don't recite from memory.

The core discipline, in one paragraph: evidence priority is cross-sample
stability > specificity (pct.in vs pct.out) > effect size > p-value. A
continuum is the null hypothesis — split two clusters into two *types* only
if the boundary survives a real test (e.g. classifier CV error, bimodality);
otherwise one type, with the variation described as a state. Names are full
words aligned to Cell Ontology, two levels: `label_l1` coarse type, `label_l2`
fine (state/context qualifiers in parentheses). "Unassigned" is a legitimate
verdict. Keep `label_l2` readable — a handful of defining markers, not
fifteen.

Explore the data freely with your own scripts (DE between siblings,
subclustering, boundary checks) — save code + output to the round dir; cite
them as evidence.

If a previous round assigned labels, read them (previous `annotate.md` /
label columns in the checkpoint). Re-judging is fine — the feature space
changed — but **every rename of a persisting population must say what
evidence changed**. Silent drift is how the same cells get three names in
three rounds. Also keep the coarse vocabulary consistent with your own
earlier rounds (don't call the same lineage "mural cell" now and "pericyte"
later at l1).

## Report (`annotate.md`)

Per cluster: label_l1, label_l2, CL id if one fits, the evidence (marker
table lines, your probe outputs), and stability across samples. List
explicitly any: split (what along), merge (into which cluster), postpone
(why). These become next steps' work — a claim without a concrete target is
not executable.

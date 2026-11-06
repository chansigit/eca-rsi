# 0026 OSP removes cells only by its QC rules; its agent only flags

Decided 2026-11-05 by the owner, after the tabula-sapiens-clean batch on 0.4.20.

**Context.** OSP's annotation agent could attach `drop` or `flag` to a cluster, or to the cells of a cluster past a
metric value. Its prompt said these were proposals that nothing applies, and the design (`docs/design/01-per-sample/`,
`docs/design/cell-exclusion-ledger.md`) left them to cross-sample's rules. The code did otherwise: at round-1
cross-sample finalize every `drop` joined the pre-annotation removal mask (ledger code `osp_proposal`), and msp
proposed a whole fragment when more than half of its cells carried one, with no check of the reason. 0017, 0023 and
0025 never named this path, so it bypassed them: in the 27 organs of tabula-sapiens-clean 10,384 cells had an OSP
drop among their removal reasons and 7,332 had no other, among them 1,663 by a cut-off on decontX contamination
(0023 gives contamination to the decontX check), 627 by a cut-off on the Scrublet score and 93 as `other` (0025
never removes for `other`). The schema's own example was a cut-off on `decontX_contamination`.

**Decision.** OSP removes cells only by its QC rules (`qc_removed.csv`). Its agent can only flag: `qc_actions` take
`flag` alone (`osp.annotate._QC_ACTIONS`), and obs `_qc_action` is `keep` or `flag`. Cross-sample compute turns a
`drop` of an older OSP output into a flag before integration, so a run restarted from round-1 cross-sample reuses
its OSP results under this rule; `osp_removal_proposals.csv.gz` still records the advice per cell. The flagged cells
are integrated and judged like any other by cross-sample's own checks (msp's doublet checks and fragments, the
contamination check, the quality and type agents under 0025) and by zoom-in.

**Consequences.** No cell is removed for OSP's advice alone. msp's two rules on inherited drops (the union in
`preannotation_removal.csv` and a fragment more than half dropped upstream) no longer fire; they stay in the
kernel, idle. Under the stress policy keep, OSP's dissociation-stress advice is no longer labelled `dissociation`
in `retained_state`, since it removes nothing to retain (0017's rule for it is replaced). The released runs are not
redone by this decision; a restart from round-1 cross-sample applies it.

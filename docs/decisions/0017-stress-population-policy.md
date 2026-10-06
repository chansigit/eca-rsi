# 0017 Stress, dissociation and dying removals follow a policy the code checks

Decided 2026-10-05 by the owner (GitHub issue #9).

**Context.** The cross-sample and zoom-in agents removed clusters as `stress`, `dissociation` or `dying` on their
own judgment; nothing in the code checked those reasons, and the zoom-in prompt said "default to remove". Across
the 183 datasets released by then, agents made 6,906 such decisions and removed 1.28 million cells with them, 59 %
of all cells agents removed (stress alone 602k, the largest single reason). Some were wrong: a proliferating cluster
(STMN1, CDK1, UBE2C) was removed as stress. Two code paths removed stress-like cells without any agent: msp's
minor-sibling fragment QC (one significant test of four, among them the dissociation score and the mitochondrial
fraction, removes the whole fragment; 2.11 million cells in all) and OSP's `dissociation-stress` drop advice,
applied in cross-sample round 1.

**Decision.**

- **A dataset policy,** spec key `stress_policy`: `remove` (default) or `keep`. It reaches the cross-sample and
  zoom-in stage configs only when the spec sets it, so runs started before keep their stage specs on resume.
- **Under `remove`, a removal of at least 10 cells stands only on evidence the code checks**
  (`stages/common.py`):
  - `stress`, `dissociation`: msp's stress-gene rule marks the cluster (`stress_clusters.csv` column `stress`:
    more than 3 of its top 10 DEG genes are heat-shock or immediate-early genes, in the global or the local view;
    MT- genes counted too until the amendment below). Cross-sample
    looks at the base cluster; zoom-in at the 2.0 cluster or any of the decision's 1.0 types.
  - `dying`: msp's mitochondrial rule marks the cluster (column `mito`, amendment below), or the cells have a
    clearly higher `pct_counts_mt` or clearly fewer `n_genes_by_counts` than the cells
    of the same identity that no decision removes (same coarse label; same 1.0 type), falling back to every cell
    no decision removes when fewer than 10: one-sided Mann-Whitney, AUC ≥ 0.7 and p < 0.05.
  - Removals of fewer than 10 cells stand on the agent's reason (owner: 29 % of the decisions, 0.4 % of the cells;
    msp computes no DEG there).
- **A removal the check does not support stays**, like a batch-only removal (`guard_batch_annotation`): the
  decision keeps `requested_action`, `requested_remove_reason` and a `host_adjustment` with the evidence note.
  It is not rejected: the submission completes the session, and a rejection would invite another reason for the
  same cells.
- **Under `keep`**, every stress, dissociation or dying removal stays; so do fragments whose only hits are the
  dissociation and mitochondrial tests, and cells whose OSP drop advice is all `dissociation-stress` (cross-sample
  compute turns it into a flag).
- **Kept cells are labelled, not relabelled.** obs `retained_state` (`''`, stress, dissociation or dying) travels
  through the rounds into `final.h5ad`; a cell keeps the state it was first retained for. `cell_ledger.csv.gz` gains
  `retained_state` and `retained_stage`, and needs_review lists `stress_retained` by stage, state and coarse label.

**What it costs.**

- Fewer removals per round under `keep` and for unsupported removals: the cell-count rule may be met in different
  rounds, and retained cells come back every round.
- A fragment kept under `keep` stays out of its stage's DEG and is still drawn as removed in msp's pre-annotation
  figure: the mask is msp's. An msp option to leave such fragments out of the mask is the upgrade.
- The fallback comparison is not identity-matched: a cell type with a naturally high mitochondrial fraction can pass
  as dying when its stressed cluster has a coarse label of its own.

**Amended 2026-10-06 (owner): mitochondrial genes are their own axis.** A high mitochondrial share marks damaged or
dying cells, not the transcriptional stress response, so MT- genes no longer count toward the stress-gene rule.
They get a rule of their own in `stress_clusters.csv`: a cluster is `mito` when it is small next to its local
siblings (fewer cells than a quarter of its top-3 PAGA neighbours pooled, the share msp's minor-sibling fragments
use) and more than 3 of its top 10 genes against them are MT- genes. Local view only: against the whole dataset a
cell type with a naturally high mitochondrial fraction would qualify. A `mito` cluster is `recommend_removal`, as a
stress cluster is, and supports a `dying` removal; a `stress` or `dissociation` removal still needs the stress mark.
The per-cell `pct_counts_mt` test is unchanged. Which genes count as stress genes is #29.

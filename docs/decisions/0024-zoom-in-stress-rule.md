# 0024 Zoom-in removes stress by its own rule; the agent may only keep

Decided 2026-10-25 by the owner (#68 part 2, option B), after part 1 (0023) took stress out of cross-sample.

**Context.** Zoom-in still removed stress the old way: a zoom-in agent removed a 2.0 cluster whole when
`stress_clusters.csv` marked it (more than 3 panel genes among its top 10 DEG). In the first runs on 0.4.13-0.4.15
this removed 2,716 cells of 05_Fuetal round 1 (29 % of its myeloid lineage, whose activated macrophages express
FOS, JUN, EGR1 and NFKBIA by function) and was most of what kept tabula-sapiens-clean Lung from converging (rounds
2 and 3 each removed about 1,450 cells, mostly stress and dying). The owner's calibration on HCL adult adrenal gland
(genuine dissociation stress) and PBMC68k (none) gave a rule that picks cells, not clusters (#68).

**Decision.**
- *The rule* (`zmip/stress.py`, run in every zoomed lineage after its re-embedding): over-cluster the lineage to a
  median of about 200 cells; a candidate is an over-cluster of 20 or more cells with 6 or more stress-panel genes at
  logFC >= 1.5, Bonferroni p < 0.01 and >= 10 % of its cells against the rest of the lineage; the background is the
  lineage's cells in no candidate; a candidate's cells whose panel score (scanpy `score_genes`) is above the
  background's 95th percentile are stress cells. Cells a numerical check already removes are not tested.
  Results: `stress_cells.csv`, `stress_overclusters.csv`, `stress_by_cluster.csv` (per 2.0 cluster, what the agent
  sees).
- *The host removes them* at apply with code `stress` and the score, threshold and over-cluster as evidence, like
  the other numerical removals; under the stress policy keep they stay, labelled.
- *The agent may only keep* (option B): `keep_stress: [{cluster_id, evidence}]` on a 2.0 cluster that has stress
  cells keeps them, labelled in obs `retained_state`, and needs_review (`stress_retained`) shows the evidence. An
  agent removal for stress or dissociation of 10 or more cells stays, labelled (below 10 the agent's reason stands,
  as for every check). The dying rule (mito mark or clearly higher mitochondrial fraction) is unchanged.

*Six genes, not four* (owner, 2026-10-25). With 4, the rule took 1.4-1.7 % of PBMC68k's T/NK lineage every round (one group with FOS, DUSP1, NFKBIA, ZFP36 and FOSB raised, no heat-shock gene), whatever the over-cluster size (200, 500 or 1,000 cells). With 6 it takes 0.1 %, while the heat-shock-led stress of 05_Fuetal (fibrochondrocyte 6.5 %, mural 11 %) and Lung (myeloid 1.4 %, endothelial 1.3 %) stays; the adrenal control's genuine clusters pass 11-21 genes.

**Consequences.** Stress removals are reproducible and per cell; a cluster is no longer lost for a few stressed
cells, and a lineage whose cells all express the panel by function can be kept on evidence. The thresholds come from
one positive dataset. Evidence assembled before this version has no `stress_cells.csv`: the old mark still applies
there.

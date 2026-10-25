# 0023 Doublets from Scrublet, contamination from decontX, stress within a lineage

Decided 2026-10-24 by the owner (#68, #70), from the pbmc68k run of 2026-10-22 (8 libraries, `pbmc68k-qc67b-20261022`).

**Context.** Round 1 removed 13,053 T cells as stress, 24 % of the T cells entering it, and 2,034 cells as
doublet or ambient outliers, 2,013 of them on decontX alone. The stress cells were no more stressed than the T
cells kept: cross-sample's rule (more than 3 stress genes among a cluster's top 10 DEG, against all lineages or
against 3 PAGA neighbours) ranks without an effect-size floor and compares across cell types, and the quality
agent dropped a 5,393-cell naive CD4 cluster on it. decontX, run per library with no empty-droplet background,
measures "expresses another cluster's genes": half of its outliers were heterotypic doublets (the right cells for
the wrong reason), the rest ordinary T cells with another subset's cytotoxic genes. OSP called no doublet at all,
since its Scrublet threshold was untrusted in every library.

**Decision.**

- *Stress is judged against cells of the same type.* Cross-sample marks no cluster stress
  (`write_deg_results(stress=False)`; the hit genes stay in `stress_clusters.csv` for reading), its prompts say
  stress is not a reason there, and the type guard of 0017 keeps a stress or dissociation removal of 10 or more
  cells, labelled. Zoom-in keeps testing it within the lineage; its rule is the second part of #68. The mito
  (dying) rule stays at cross-sample and at zoom-in.
- *Doublets come from Scrublet.* OSP flags Scrublet doublets (`qc_doublet`) and removes none. Each library's
  threshold is OSP's when at least 25 % of its simulated doublets exceed it, else their 75th percentile
  (`library_threshold`; cross-sample writes it per cell as `obs["osp_doublet_threshold"]`). Before annotation
  msp removes a cell by any of three checks (`msp/integrate/doublets.py`): above the threshold with 1.3x the RNA
  of the cells below it in its r1.0 cluster and library; in an over-clustering cluster (median size about 200,
  fixed seed) of 20 or more cells half above the threshold with no gene of its own; a decontX candidate with a
  doublet score above its cluster's 95th percentile and raised RNA.
- *decontX decides contamination only*, after the type agent, in cross-sample finalize: a candidate is removed
  when its share of UMIs on the soup genes foreign to its r1.0 cluster is above the cluster's 99.9th percentile
  with at least 3 such UMIs; clusters with fewer than 1,000 background cells, and the soup's dominant lineage, are
  not tested. Removals carry the code `contamination` and `contamination_check.csv`.
- *A percentile needs a background of 1/(1-p) cells* (`min_background`), or the check is not made and the cell
  stays.
- *Fragments* lose their decontX and dissociation tests; the doublet test is the doublet-cluster criterion and
  the mitochondrial test compares a fragment with its own parent core.

**Consequences.** In the issues' replay of round 1 the doublet checks remove 1,218 cells, 767 of them supported doublets, against
2,034 before; no stress cell and no fragment is removed at cross-sample. Platelet-T aggregates are a known miss:
Scrublet rarely simulates them and the contamination check skips T cells. A doublet group of a real
intermediate population (half of its cells above the threshold, no gene of its own) would be removed whole; bone
marrow is the test. The thresholds come from one dataset. A run started before this version keeps its rules for
the stages it already ran; `restart-from` redoes the later ones with these.

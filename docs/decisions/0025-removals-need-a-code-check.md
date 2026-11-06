# 0025 An agent's removal stands only when a code check supports its reason

Decided 2026-10-26 by the owner (route 1), after the restarts of 05_Fuetal and tabula-sapiens-clean Lung on 0.4.17.

**Context.** 0017 and 0024 checked stress, dissociation and dying removals; every other reason was the agent's
word. In 05_Fuetal a zoom-in agent removed 166 venous endothelial cells as `other` with the evidence "Wait - this is
166 cells from type 5. Let me reconsider." and 93 mural cells as `other` whose rationale ended "Changed decision:
keep with medium confidence."; the schema asked for the action before the evidence, and an accepted proposal ends
the session. Cross-sample quality agents dropped cells by their own cut-offs on decontX (> 0.5) and Scrublet
(> 0.2) scores, which 0023 had given to code checks. A replay on the released 05_Fuetal, Lung and PBMC68k runs
compared every agent removal with the cells nothing removes: comparing with the removal's own type failed when the
agent removed a whole type or a type defined by the defect, and "more RNA" failed clear heterotypic doublets
(an AT2 cell with a T cell has hardly more RNA than an AT2 cell).

**Decision.** A removal of at least 10 cells stands only when the code check of its reason supports it; otherwise
its cells stay with their identity labels, obs `retained_state` names the reason, and needs_review lists them
(`removal_retained`). The checks (`ecarsi/stages/common.py` `removal_evidence`) compare the removed cells that no
other rule removes with the cells nothing removes (one-sided Mann-Whitney, AUC >= 0.7, p < 0.05, as for dying):
- *low-quality*: clearly fewer genes or counts, or a clearly higher mitochondrial fraction, and no gene of its own
  (detected in >= 30 % of its cells and <= 5 % of every other cluster, msp `own_gene_counts`): a real cell type of
  low RNA has some;
- *doublet*: half or more of its cells above their library's Scrublet threshold and no gene of its own (msp's
  doublet cluster); or, at zoom-in, another lineage's foreign score clearly higher while the cells keep at least
  half of the lineage's own-marker positivity (the doublet of the reassign rule; cells that lost the lineage's
  markers are misplaced and should be reassigned);
- *ambient* and *other*: never removed; contamination is the decontX check of 0023, and other names nothing to
  check. *batch* stays msp's non-destructive batch guard. Stress, dissociation and dying keep 0017 and 0024.

At cross-sample a whole-cluster drop is checked by its verdict (artifact-doublet, artifact-lowquality; ambient,
ambiguous and real are never dropped) and becomes a flag when unsupported, after #71's rule; a cell rule drops only
on a QC metric (genes, counts, mitochondrial, ribosomal, haemoglobin and MALAT1 fractions), so a rule on
`doublet_score` or `decontX_contamination` becomes a flag. The check follows the reason the agent gave: a removal
whose evidence fits another reason is not re-filed. Every submission schema asks for the evidence and the rationale
before the action.

**Consequences.** On the three released runs the checks keep 1,074 cells of 05_Fuetal (259 as other, 86 of four
marker-less small types whose QC was no worse, 145 dropped as doublets with 3 % above the Scrublet threshold, 584 by
cell rules on decontX or Scrublet scores), 774 of Lung (325 "doublets" in the epithelial lineage that are pure
macrophages and T cells, 114 more without a clearly higher foreign score, 106 of three small drops, 229 by cell
rules) and 458 of PBMC68k (418 monocytes removed as ambient, 40 without a clearly higher foreign score), and support
the rest. Genes two lineages share (MHC class II in activated endothelium) can raise a foreign score, so a doublet
check can pass a real population; the AUC standard comes from the dying check. Evidence bundles from before this
version carry no change: the checks run when a proposal is submitted.

**Amended 2026-11-06 by the owner (#73).**
- *Whole clusters.* The checks of the type and quality phases, and of zoom-in, count and test the whole target,
  the cells the rule list P (fragments, doublet checks) already holds included: P is only a list until finalize,
  so testing the whole cluster is testing before the rules. With P left out, an agent's doublet drop almost never
  passed (the high-scoring cells were mostly in P), and a cluster of 10 or more cells with fewer outside P could
  not be tested at all. The reference stays the cells nothing removes.
- *Two phases at cross-sample.* A removal of 10 or more cells stands when either phase asks for it and the check
  passes (type by its reason, quality by its verdict); #71's rule, which turned every quality drop of a cluster the
  type phase kept into a flag, now holds only below 10 cells. Below 10 cells, where nothing is checked, a removal
  stands only when both phases ask for it; otherwise its cells stay, labelled, and needs_review lists them. A
  cluster kept under the stress policy keep cannot be dropped by quality.
- *Cell rules.* A quality cell rule drops only for low quality, on the mitochondrial fraction cut above its line
  or the genes or counts cut below it, and only when the line lies among the cluster's outliers: beyond its median
  by 5 MADs (osp's multiple; log1p scale for genes and counts; the whole cluster's cells, not per sample); when it
  cuts 10 or more cells, they have no gene of their own against the rest of the cluster. Every other cell-rule
  drop is a flag. Since 0.4.18, 41 drop rules had removed 2,282 cells at lines from 13 % to 35 % mitochondrial
  reads, up to 39 % of a cluster, with nothing checking the line; the 0025 check passes any line, because it
  compares cells chosen by that line on that metric.
- *Sample exclusion.* The inclusion agent names one of two reasons. `low-quality` stands only when the sample's
  cells have clearly fewer genes or counts, or a clearly higher mitochondrial fraction, than those of the included
  samples, and it has no gene of its own against them; otherwise the sample is included and needs_review lists it
  (`removal_retained`). `not-this-dataset` (wrong tissue or species, a duplicate upload, a control or technical
  sample) cannot be checked and stands. Batch, composition, treatment and few cells are not reasons. A sample under
  10 cells is not checked.

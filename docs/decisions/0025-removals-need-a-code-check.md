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

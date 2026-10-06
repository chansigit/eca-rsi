# 0020 One stress panel and one mitochondrial rule, for every species

Decided 2026-10-06 by the owner (#29): the panel by the method below, metallothioneins out of it, mitochondrial genes
their own axis (0017, amended), rhesus, cynomolgus and mouse lemur supported. Ships before the second batch of #32;
the first batch runs on the version before it, so no batch mixes two panels.

**Context.** Two stress panels disagreed. OSP's `dissociation_score` used the 140 genes of van den Brink 2017,
including identity genes (DCN, DES, LMNA, SERPINE1) and old symbols that no longer match human data (H3F3B, CYR61);
msp's stress rule used a hand-picked core of 25. Both matched human symbols case-insensitively. Mitochondrial genes
were found by the prefix `mt-` (OSP) or `MT-` (msp): rhesus, cynomolgus and mouse lemur references name them ND1,
COX1, CYTB, so their samples would get `pct_counts_mt` = 0 without a sign.

**Decision.** A package `genesets/` that the kernels share (osp and msp may import it, tests/test_layers.py).

- **Mitochondrial genes** (`genesets.is_mito`) follow stangene.mito: an `MT-` prefix (human MT-CO1, mouse mt-Co1, rat
  Mt-co1; fruit fly mt:CoI) or a bare name (ND1-ND6, ND4L, COX1-COX3, CYTB, ATP6, ATP8; the C. elegans names). No bare
  name is an official symbol of human, mouse, rat or zebrafish, so the rule needs no species. OSP's `pct_counts_mt`
  and msp's `mito` mark use it. OSP's summary records `n_mito_genes`; a sample where it is 0 is listed in
  needs_review (`upstream_review`): its mitochondrial filters saw nothing.
- **The stress panel** (`genesets.is_stress`, 35 genes) serves both OSP's `dissociation_score` and msp's stress
  mark. `genesets/stress_panel.tsv` holds every gene considered, its evidence, the decision and its reason.
  - *Seed and literature:* the 25 genes of msp's core, and every gene in at least two of eight published
    dissociation-stress signatures: van den Brink 2017, Adam 2017, O'Flanagan 2019, Denisenko 2020, Massoni-Badosa
    2020, Machado 2017/2021, Marsh 2022, Van Hove 2019. 199 genes; none of the eight lists has a mitochondrial gene,
    all treat the mitochondrial fraction as a separate QC axis.
  - *Data:* within each cell type of 18 human and 16 mouse dissociated-cell atlases, the Spearman correlation of a
    gene with the leave-one-out score of the seed core. A stress gene moves with the core in most types; an identity
    gene does not (anchors such as PECAM1 or PTPRC: r ~ 0.02). In 4 mouse nuclei atlases, where dissociation stress
    cannot arise, the core genes fall to r ~ 0.02-0.11. Cell-type specificity (tau) did not separate the two kinds.
  - *Rule:* median r >= 0.15 in human and in mouse cells, and r > 0.1 in at least 60 % of types in both; then
    identity and housekeeping genes out (KLF2, KLF4, H3-3B). Metallothioneins (MT1X, MT2A) stay out by the owner's
    decision: they answer metal ions. Five genes of the old core fell out (DNAJB4, EGR2, HSPB1, HSPE1, HSPH1: mouse
    r 0.00-0.12).
  - *Validation:* the share of a dataset's stress-score variance that cell type explains (eta squared, median):
    human cells 0.331 with the 140 genes, 0.308 with the panel (lower in 67 % of datasets); mouse cells 0.371 and
    0.278 (80 %); nuclei 0.111 and 0.116. With both panels the highest-scoring types are stromal and vascular: cells
    in extracellular matrix take longer, harsher digestion, so what remains is likely real stress, not identity.
- **Every species by one table.** The panel's rows list Ensembl Compara orthologs (symbol and Ensembl ID) for mouse,
  rat, rhesus, cynomolgus, marmoset, mouse lemur and zebrafish; a gene name matches if it is any of them, its human
  symbol, an old symbol or the human Ensembl ID, case-insensitively. One-to-one with a symbol: mouse 32, rat 30,
  rhesus 29, cynomolgus 25 (6 more by ID only), marmoset 25, mouse lemur 25, zebrafish 20; Compara misses some known
  orthologs (mouse Hspa8) that the human symbol matches anyway. No matched name is another gene's official symbol in
  human, mouse or rat.

**Consequences.** Every dataset scores stress and finds mitochondrial genes the same way, whatever its species, and
the two kernels cannot drift apart again. A change of the panel is a change of `stress_panel.tsv` with its reasons.
OSP's `dissociation_genes=` and `species=` presets stay for callers who want another set; the `mt_prefix` argument
is gone. The analysis scripts and tables behind the panel are kept with the project's archives.

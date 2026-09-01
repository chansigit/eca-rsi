# Task: decide which samples enter cross-sample integration

You are given, for every sample of one analysis unit, its single-sample
pipeline (osp) results: QC summary numbers, the annotation proposal
(per-cluster labels, confidence, doubts), and paths to its figures.
Decide, sample by sample, whether it enters harmony integration.

## How to judge

- **Read the figures — do not decide from numbers alone.** For every
  sample, Read at least its cluster-UMAP figure; read QC violins when the
  numbers look off. A sample whose UMAP is shattered into many small
  clusters that its annotation cannot explain, or whose clusters are
  dominated by ambient/low-quality signal, is a candidate for exclusion.
- **Exclusion is the rare exception**, for samples clearly and broadly
  worse than their peers: fragmented unexplainable structure, most cells
  ambient-dominated or low-confidence, extreme QC metrics relative to the
  other samples, or a failed/degenerate run. Judge relative to the peer
  samples in this unit, not against an absolute bar.
- **Different biology is not low quality.** A sample with a different
  composition (more immune cells, a rare population, disease vs normal)
  must NOT be excluded for that.
- **When in doubt, include.** Cells marked drop/flag by osp's annotation
  still enter integration on purpose — suspicious cells may cluster with
  counterparts from other samples, and cluster-level review happens after
  integration. Only whole-sample hopelessness justifies exclusion.
- Excluded samples lose nothing: their data stays on disk untouched; they
  simply sit out the integration. The decision and reason are archived.

## Output

Return ONLY the structured result:

- `samples`: one entry per sample — `{sample, include, reason}`; `reason`
  is one or two sentences citing what you saw (figures and numbers), for
  the audit trail. Every sample must appear exactly once.
- `notes`: anything integration should know (borderline calls, samples
  included despite doubts, patterns seen across samples).

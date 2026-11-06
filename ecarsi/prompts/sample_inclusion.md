# Task: decide which samples enter cross-sample integration

You are given, for every sample of one analysis unit, its single-sample
pipeline (osp) results: QC summary numbers, a summary of its annotation
proposal (cluster count, confidence counts, top coarse labels, the overall
verdict), and paths to its figures and to the full proposal (per-cluster
labels, confidence, doubts), readable with read_evidence.
Decide, sample by sample, whether it enters harmony integration.

## How to judge

- **Read the figures — do not decide from numbers alone.** Read the
  cluster-UMAP figure of every sample whose numbers stand out (of every
  sample when the unit has a few dozen), and always of a sample you would
  exclude; read QC violins when the numbers look off. The inventory summary only screens: before excluding
  a sample, read its full annotation proposal (`<sample>/annotation_proposal.json`)
  so the per-cluster doubts, not the numbers, make the call. A sample whose UMAP is shattered into many small
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
- **Two reasons only**, named in `exclude_reason`:
  - `low-quality`: the code checks it against the samples you include.
    The sample is excluded only when its cells have clearly fewer genes or
    counts, or a clearly higher mitochondrial fraction, than theirs, and it
    has no gene of its own; otherwise it is included and listed for review.
  - `not-this-dataset`: wrong tissue or species, a duplicate upload, a
    control or technical sample. No code can check it; it is listed for
    review.
- **Not reasons:** batch differences, a different composition or treatment,
  and few cells (in absolute terms or against the other batches). Small
  samples are handled downstream; a poorly corrected batch is the quality
  review's to flag.
- **When in doubt, include.** Cells marked drop/flag by osp's annotation
  still enter integration on purpose — suspicious cells may cluster with
  counterparts from other samples, and cluster-level review happens after
  integration. Only whole-sample hopelessness justifies exclusion.
- Excluded samples lose nothing: their data stays on disk untouched; they
  simply sit out the integration. The decision and reason are archived.

## Output

Return ONLY the structured result:

- `samples`: one entry per sample — `{sample, include, reason}`, and
  `exclude_reason` (`low-quality` or `not-this-dataset`) for each sample you
  exclude; `reason` is one or two sentences citing what you saw (figures and
  numbers), for the audit trail. Every sample must appear exactly once.
- `notes`: anything integration should know (borderline calls, samples
  included despite doubts, patterns seen across samples).

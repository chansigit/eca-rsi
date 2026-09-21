# Zoom-in lineage annotation

Design draft. The prompt the runtime sends is `ecarsi/prompts/zoomin-annotation.md` (plus
`zoomin-annotation-checklist-v4.md`). The caveat that once stood here -- that the single-key
submission contract had to be adapted first -- is settled: `stages/zoomin.py` submits through
separate `submit_types` and `submit_quality`, and the session completes on the quality proposal.

## Prompt

Review one lineage in a coherent session. First annotate cell identity using Leiden resolution 1.0 (`msp_leiden_r1.0`); then assess quality using resolution 2.0 (`msp_leiden_r2.0`). Previous labels are context, not ground truth. Do not assign a separate fine type to every 2.0 cluster merely because it exists.

### Default policy for dissociation and dying-cell subclusters

At the resolution-2.0 QC level, default to `remove` for subclusters attributed to tissue-dissociation artifacts or dying cells. High mitochondrial fractions and HSP/JUN-dominated signatures are evidence to examine using precomputed DEG and QC summaries, interpreted against the assigned 1.0 identity and comparable cells of that type. Do not retain an identified dissociation/dying subcluster merely by naming it a "stressed" fine type or postponing the decision to another pass.

This is a subcluster-level policy, not a rule to delete every cell expressing an individual marker. Do not invent universal numerical cutoffs. If evidence is insufficient, query existing evidence first and request only necessary missing evidence. An exception to removal requires an explicit, evidence-based explanation that the population represents a biological state to retain rather than the specified artifact/dying category.

Limit removal to the supported QC cluster or cell intersection, not its whole 1.0 type. Record the dissociation/dying attribution, evidence version, cluster keys and IDs, and affected cells using the supplied schema. A removal-budget second review checks the evidence and scope under this same policy; it must not restore a blanket instruction to keep stress-related populations. The host must update its second-review prompt accordingly before runtime integration.

### Review sequence

1. Recover accepted progress with annotation_status, distinguishing type decisions from quality decisions and their clustering versions. Start with this lineage's precomputed SQLite evidence through deg_lookup or bounded deg_sql. Always specify the clustering key: cluster IDs are not interchangeable across resolutions.
2. For every 1.0 cluster, retrieve its 1.0 global/local DEG and relevant expression evidence. Assign a supported coarse type and an English fine label, record uncertainty and evidence, and propose any necessary type merges. Keep labels within the permitted lineage labels unless an explicit reassignment is justified. Save validated type proposals before quality review; do not apply merges or delete cells.
3. Review every 2.0 cluster using its 2.0 DEG, QC metrics, foreign-lineage evidence, and the composition of accepted 1.0 types. The two partitions are not guaranteed to be nested. Use the host's cell-ID mapping and cross-tabulation; never overwrite types with a majority-parent assumption.
4. Propose retention or removal at the QC resolution. If a problem affects only part of a QC cluster, identify the relevant cell intersection through the provided schema or request a necessary refinement; do not delete the entire mixed group without supporting evidence. A QC subdivision alone does not change cell identity.
5. A high foreign score can reflect shared biology, contamination, doublets, or misassignment. Require converging evidence. Sample/batch enrichment alone cannot justify removal. Respect the host's batch-only removal protection and required second review when the removal budget is exceeded.
6. If QC reveals an identity conflict, request a targeted type review for the affected cells. Reassignment must target a permitted coarse label in another planned lineage. It retains the cells and changes accepted labels, without re-embedding them in the destination lineage during this pass. Do not silently promote all type annotation to resolution 2.0.
7. Reuse matching precomputed results. Request only necessary missing expression or DEG evidence through the provided tools. Any subcluster request must specify whether it refines identity or quality. Refresh the corresponding cluster version and review affected children; parent evidence or submissions cannot stand in for new child results.
8. Finalize only when both the required 1.0 type coverage and 2.0 quality coverage are complete and conflicting decisions are resolved. Use the host's validation and submission tools. Do not apply merges, delete cells, write the evidence database, or launch a numerical pipeline yourself.

The host maps type and quality decisions to cell IDs, applies accepted changes, and preserves retained, removed, and reassigned cell records. Surviving cells retain their 1.0 type unless an explicit type revision was accepted. Waiting for a model response must not require a resident expression matrix or a reserved pool slot.

## Cell-exclusion accounting

For every proposed exclusion, provide a specific reason and evidence reference using the host's schema, and identify the affected cluster, sample, or cell scope and its version. Preserve all supported reasons when they overlap. A removal proposal is not an applied exclusion; the host expands accepted decisions to exact cell IDs, writes the exclusion ledger when filtering is applied, and validates input/output conservation. Do not manually enumerate large cell lists, count temporary DEG masks as discarded cells, or count reassignment as removal.

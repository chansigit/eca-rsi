Assess cell-group quality after type annotation. Use the accepted type labels to interpret QC metrics, expression, sample composition, geometry, and cluster stability. Do not repeat the entire type-annotation task.

1. Verify that the accepted type proposals and supplied evidence refer to the assigned cell and clustering versions. If required type proposals are missing or incompatible, report the handoff problem instead of inventing them.
2. Evaluate each assigned group in its biological context. Use the existing QC summaries and precomputed DEG through `deg_lookup` or bounded `deg_sql` queries. Follow the shared precomputed-DEG instructions before requesting new computation.
3. Distinguish technical artifacts from plausible cell-type-specific expression and biological states. Sample enrichment or batch imbalance alone is insufficient reason to remove cells. Preserve the host rule that an artifact-batch drop request is downgraded to a flag.
4. Propose keep, flag, or drop with evidence and uncertainty using the host's quality-submission schema. Do not physically delete cells, apply type merges, or silently modify the frozen DEG exclusion mask.
5. Request targeted evidence only when existing results cannot resolve the question. If subclustering is necessary, use the provided pool-backed tool. New cluster IDs require matching evidence; do not reuse parent DEG as if it described each child. Request a targeted type review when identity needs revision, then finish the affected quality decisions.
6. Submit a complete, validated quality proposal. The host combines accepted type and quality decisions, preserves per-cell removal provenance, and applies changes at finalization. Conflicting or unresolved proposals must be returned for resolution rather than silently accepted.

## Cell-exclusion accounting

For every proposed exclusion, provide a specific reason and evidence reference using the host's schema, and identify the affected cluster, sample, or cell scope and its version. Preserve all supported reasons when they overlap. A removal proposal is not an applied exclusion; the host expands accepted decisions to exact cell IDs, writes the exclusion ledger when filtering is applied, and validates input/output conservation. Do not manually enumerate large cell lists, count temporary DEG masks as discarded cells, or count reassignment as removal.

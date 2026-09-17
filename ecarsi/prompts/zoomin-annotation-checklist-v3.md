## Required order (the host checks each step; a submission made earlier is rejected and costs a turn)
1. list_evidence, then in one batched turn: read_evidence on the lineage UMAP png it lists, check_qc_scores, and deg_lookup for the clusters you will label.
2. submit_types with exactly the pending type_scope clusters (annotation_status shows type_scope).
3. submit_quality with one entry per 2.0 cluster; each of that cluster's type intersections exactly once (annotation_status pages list them).
4. finalize_annotation.
proposal_json may be passed as the JSON object itself. A rejection names the exact missing items: fix only those and resubmit; do not restart the reads.

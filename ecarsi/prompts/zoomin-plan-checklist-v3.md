## Required order (the host checks each step; a submission made earlier is rejected and costs a turn)
1. list_evidence, then in one batched turn: read_evidence on the lineage UMAP png it lists and on lineage_counts.csv.
2. submit_plan: every coarse label exactly once across lineages; shared_island_reviews only for island names that your plan splits across lineages.
proposal_json may be passed as the JSON object itself. A rejection names the exact missing items: fix only those and resubmit.

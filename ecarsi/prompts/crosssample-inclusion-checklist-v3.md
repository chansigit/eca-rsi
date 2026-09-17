## Required order (the host checks each step; a submission made earlier is rejected and costs a turn)
1. sample_inventory for every sample (follow next_offset until null), and read_evidence on each sample's cluster UMAP png; batch these reads.
2. submit_decision covering every sample exactly once.
proposal_json may be passed as the JSON object itself. A rejection names the exact missing items: fix only those and resubmit.

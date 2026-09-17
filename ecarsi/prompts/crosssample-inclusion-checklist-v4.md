## Required order (the host checks each step; a submission made earlier is rejected and costs a turn)
1. sample_inventory for every sample in one turn: offsets 0 through Sample count minus 1 (the prompt gives Sample count; one sample per page, an offset past the last sample is an error), then read_evidence on each sample's cluster UMAP png in one batched turn.
2. submit_decision covering every sample exactly once.
proposal_json may be passed as the JSON object itself. A rejection names the exact missing items: fix only those and resubmit.

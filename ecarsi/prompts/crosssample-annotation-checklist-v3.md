## Required order (the host checks each step; a submission made earlier is rejected and costs a turn)
1. list_evidence, then in one batched turn: read_evidence on one png it lists, check_qc_scores, and deg_lookup on the assigned clusters.
2. For quality: type_context with increasing offset until next_offset is null, for every cluster.
3. submit_decision. Type: cover each assigned cluster exactly once, with one boundary_review per adjacent pair of kept coarse labels. Quality: decide every cluster.
proposal_json may be passed as the JSON object itself. A rejection names the exact missing items: fix only those and resubmit; do not restart the reads.

## Required order (the host checks each step; a submission made earlier is rejected and costs a turn)
1. sample_inventory from offset 0, following next_offset until null (a page holds as many sample summaries as fit; an offset past the last sample is an error), then read_evidence on each sample's cluster UMAP png in one batched turn.
2. Before excluding a sample, read_evidence its full annotation proposal (`<sample>/annotation_proposal.json`, the `proposal` path of its inventory entry): the summary screens, the full text with per-cluster doubts decides. The host rejects an exclusion whose proposal was not read.
3. submit_decision covering every sample exactly once.
proposal_json may be passed as the JSON object itself. A rejection names the exact missing items: fix only those and resubmit.

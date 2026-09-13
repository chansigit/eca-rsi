# Recorded-lineage evaluation — 2026-09-12

Both candidates completed the same frozen Fu 2022 Mural task and passed the
production submission, annotation and cell-conservation checks. The fixture
contains 4,597 cells and 15 clusters. Its historical proposal keeps 14 clusters
and removes one; that proposal is a consistency reference, not a biological
answer key.

| Measure | Doubao Seed 2.1 Turbo | Doubao Seed 2.1 Pro |
| --- | ---: | ---: |
| Host submission rejections | 0 | 0 |
| Other tool errors | 0 | 0 |
| Wall time | 694.9 s | 926.8 s |
| Model requests | 22 | 44 |
| Input tokens reported | 954,600 | 2,486,453 |
| Output tokens reported | 33,433 | 33,106 |
| Cluster actions | 15 keep | 14 keep, 1 remove |
| Retained cells | 4,579 | 4,524 |
| Confidence: high / medium / low | 6 / 7 / 2 | 8 / 5 / 2 |

The complete model identifiers are `doubao-seed-2-1-turbo-260628` and
`doubao-seed-2-1-pro-260628`, both under `HARNESS=openai`. The model pool was
disabled, each candidate received a fresh work directory and a 120-turn limit,
and their fixture, evaluator and runtime hashes match. Neither backend reported
a dollar cost; token counts above are usage reports, not a cost estimate.

The 55-cell difference is cluster 5. Turbo retains it as a small perivascular
stromal population; Pro removes it as a possible endothelial–mural doublet
population. Both assign low confidence. Another 18 cells are removed by the
shared pre-annotation filtering. Resolving the cluster-5 decision needs
independent review; matching the historical removal does not establish accuracy.

Both candidates give every cluster a distinct fine label. Their pairwise fine
partition agreement with the historical reference is 0.962, but most pairs in
that reference are already separate. This high value should not be read as
evidence of accurate subtype annotation. One task also cannot establish a
general model ranking.

The frozen input manifest, complete scores, proposals and logs are retained at
`/scratch/users/chensj16/eca-runs/checkpoint-validation-20260912/` under
`fixtures/` and `model-eval/`. The score files include the full runtime and
evaluator hashes. This evaluation uses MSP 0.5.1, ZMIP 0.3.9 and bridge 0.2.14.

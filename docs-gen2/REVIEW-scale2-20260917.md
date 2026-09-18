# Scale batch review (28 Tabula Sapiens organs, gen1 tree, protocol v2/v3) — 2026-09-17

Data: `scale-acceptance-20260915-v2/*/units/*/release/{cell_exclusions,cell_ledger}.csv.gz` and each unit's publication chain, aggregated by `/tmp/review_scale2.py` (per-unit numbers in `/tmp/review_scale2.json`). 29 released units, eye excluded (terminated at round 7).

## 1. Where the cells go

Input 1,050,995 cells → released 487,658 (46 %). Removed 563,337:

- per-sample: 231,037 (22.0 % of input); of which in rounds ≥ 5: 0
- cross-sample: 82,934 (7.9 % of input); of which in rounds ≥ 5: 13,306
- zoom-in: 249,366 (23.7 % of input); of which in rounds ≥ 5: 78,085

Zoom-in removes more cells than OSP QC does, and a third of its removals happen at round 5 or later, where the loop is supposed to be converging.

## 2. What the removals are called

Top reason codes (all units, a cell can carry several codes):

| stage:code | cells |
|---|---:|
| zoom-in:fragment_qc | 128,245 |
| per-sample:hard_threshold | 94,340 |
| per-sample:hard_threshold+high_mito | 38,868 |
| cross-sample:fragment_qc | 36,999 |
| zoom-in:stress | 35,870 |
| per-sample:mad_outlier | 33,565 |
| zoom-in:dissociation | 32,361 |
| per-sample:high_mito | 31,338 |
| cross-sample:quality_decision | 25,989 |
| zoom-in:dying | 24,399 |
| cross-sample:cell_outlier | 22,096 |
| per-sample:hard_threshold+mad_outlier+high_mito | 18,479 |
| zoom-in:doublet | 15,401 |
| cross-sample:doublet | 13,788 |
| zoom-in:low-quality | 10,747 |
| per-sample:doublet | 9,573 |
| cross-sample:stress | 6,210 |
| zoom-in:ambient | 5,316 |

Confidence recorded on agent removals: zoom-in:? 130,193, cross-sample:? 113,347, zoom-in:high 80,601, zoom-in:medium 43,276, zoom-in:low 1,129 (`?` = no confidence field on that record, mostly fragment_qc / cell_outlier entries produced by the host rule rather than a model decision).

`fragment_qc` alone accounts for 128k zoom-in removals and 37k cross-sample removals: 15 % of every input cell, more than any biological category (stress 42k, dissociation 32k, dying 24k, doublet 29k across both stages).

## 3. Labels do not settle

Median fraction of kept cells whose *coarse* label changes at a step (n = units that reached it):

| step | median | max |
|---|---:|---:|
| round01.cross-sample | 0.42 | 0.93 |
| round01.zoom-in | 0.04 | 0.33 |
| round02.cross-sample | 0.23 | 0.99 |
| round02.zoom-in | 0.02 | 0.22 |
| round03.cross-sample | 0.25 | 0.98 |
| round03.zoom-in | 0.03 | 0.21 |
| round04.cross-sample | 0.25 | 0.98 |
| round04.zoom-in | 0.02 | 0.13 |
| round05.cross-sample | 0.15 | 0.92 |
| round05.zoom-in | 0.02 | 0.11 |
| round06.cross-sample | 0.21 | 0.93 |
| round06.zoom-in | 0.02 | 0.24 |
| round07.cross-sample | 0.23 | 0.58 |
| round07.zoom-in | 0.04 | 0.23 |
| round08.cross-sample | 0.21 | 0.59 |
| round08.zoom-in | 0.02 | 0.25 |
| round09.cross-sample | 0.21 | 0.81 |
| round09.zoom-in | 0.02 | 0.18 |
| round10.cross-sample | 0.26 | 0.82 |
| round10.zoom-in | 0.04 | 0.26 |

Cross-sample re-annotation rewrites the coarse label of roughly a quarter of the surviving cells every round, from round 1 to round 10, while zoom-in changes 2–4 %. Some of it is synonyms (Myeloid cell → Macrophage), some is real reassignment; the stop rule only looks at cell counts, so this churn is invisible to it, and every relabelled cluster is a fresh candidate for fragment / stress removal in the next zoom-in.

## 4. Per unit

```
dataset                        unit             input osp qc rounds  removal % per round                  final  kept  reason | review kinds
Tabula Sapiens / Bladder       bladder          64132    24%     10  14.2 8.6 7.5 3.1 4.1 4.8 7.8 2.1 5   25947   40%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=345 low_confidence=94 lineage_skipped=32 annotation_boundary=16 removed=12 plan_warning=3 convergence=1
Tabula Sapiens / Blood         blood-human      76057    15%     10  9.0 10.9 5.4 7.5 2.6 8.4 5.0 2.4 4   35620   47%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=175 low_confidence=56 lineage_skipped=18 removed=10 annotation_boundary=8 plan_warning=4 convergence=2
Tabula Sapiens / Bone_Marrow   bone-marrow      23960    16%      6  5.7 5.5 6.8 6.5 2.0 0.9              15246   64%  removed 0.94% < 1% | inspect_flag=195 low_confidence=49 removed=8 lineage_skipped=8 annotation_boundary=8 plan_warning=3
Tabula Sapiens / Ear           human-ear         3055    11%      2  17.2 0.6                              2241   73%  removed 0.62% < 1% | lineage_skipped=12 low_confidence=11 inspect_flag=8 annotation_boundary=6 removed=3 plan_warning=2
Tabula Sapiens / Fat           fat-all          93034    15%      9  15.0 7.6 4.5 7.2 4.9 1.3 4.8 2.0 0   47693   51%  removed 0.85% < 1% | inspect_flag=585 low_confidence=99 lineage_skipped=17 annotation_boundary=12 removed=10 plan_warning=3
Tabula Sapiens / Heart         heart            25303    36%      6  15.0 4.7 1.3 4.9 4.1 0.9             11698   46%  removed 0.86% < 1% | inspect_flag=165 low_confidence=56 lineage_skipped=29 annotation_boundary=17 removed=8 plan_warning=7
Tabula Sapiens / Kidney        human-kidney     11024    83%      2  6.3 2.4                               1675   15%  removed 42 cells < 100 | inspect_flag=9 lineage_skipped=7 low_confidence=5 plan_warning=2 removed=1
Tabula Sapiens / Large_Intesti large-intestin   28368    46%      4  10.4 9.3 3.5 0.2                     11956   42%  removed 0.21% < 1% | inspect_flag=52 low_confidence=24 lineage_skipped=23 annotation_boundary=5 removed=4 plan_warning=1
Tabula Sapiens / Liver         liver            21101    67%      6  21.6 23.9 12.1 5.7 3.3 1.7            3271   16%  removed 55 cells < 100 | inspect_flag=69 lineage_skipped=22 low_confidence=16 annotation_boundary=12 removed=6 convergence=2 plan_warning=2
Tabula Sapiens / Lung          lung             62938    20%     10  11.0 3.7 2.8 5.9 5.6 2.2 3.5 2.0 4   31042   49%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=336 low_confidence=98 lineage_skipped=22 annotation_boundary=20 removed=12 plan_warning=4 convergence=1
Tabula Sapiens / Lymph_Node    human-lymph-no  126016    12%     10  11.1 8.2 5.1 6.9 6.0 3.1 1.8 3.0 3   66224   53%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=470 low_confidence=89 lineage_skipped=23 annotation_boundary=10 removed=7 plan_warning=6 convergence=1
Tabula Sapiens / Mammary       mammary          30543    13%      6  18.0 5.3 4.3 4.6 3.5 0.9             18006   59%  removed 0.91% < 1% | inspect_flag=116 low_confidence=41 lineage_skipped=16 removed=7 annotation_boundary=6 plan_warning=1
Tabula Sapiens / Muscle        muscle           41843    14%     10  17.5 12.5 11.4 7.9 4.7 6.0 10.3 6.   14030   34%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=259 low_confidence=149 lineage_skipped=23 annotation_boundary=12 removed=11 convergence=4 plan_warning=3
Tabula Sapiens / Ovary         ovary-human      45321    28%     10  28.4 7.8 10.9 1.7 1.1 2.9 13.6 3.5   14583   32%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=171 lineage_skipped=50 low_confidence=43 annotation_boundary=11 removed=7 convergence=3 plan_warning=1
Tabula Sapiens / Pancreas      human-pancreas   14019    30%      5  9.2 10.7 6.1 2.0 1.3                  7219   51%  removed 93 cells < 100 | inspect_flag=126 lineage_skipped=31 low_confidence=30 annotation_boundary=21 removed=9 convergence=1 plan_warning=1
Tabula Sapiens / Prostate      prostate-human   20286    30%      6  16.6 5.0 12.3 31.8 2.3 1.4            6489   32%  removed 92 cells < 100 | inspect_flag=114 low_confidence=34 lineage_skipped=26 annotation_boundary=9 removed=8 plan_warning=3 convergence=2
Tabula Sapiens / Salivary_Glan salivary-gland   38160    18%     10  13.2 2.9 9.7 2.2 1.3 2.6 8.5 5.1 1   18065   47%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=283 low_confidence=116 lineage_skipped=34 removed=14 annotation_boundary=10 convergence=1 plan_warning=1
Tabula Sapiens / Skin          skin             15752     9%      6  15.0 2.8 6.4 1.4 3.3 0.6             10478   67%  removed 0.60% < 1% | inspect_flag=199 low_confidence=55 lineage_skipped=24 annotation_boundary=11 removed=7 plan_warning=1
Tabula Sapiens / Small_Intesti small-intestin   40346    66%      4  15.0 9.3 2.1 1.0                     10173   25%  removed 0.98% < 1% | inspect_flag=82 lineage_skipped=34 low_confidence=16 removed=4 annotation_boundary=3
Tabula Sapiens / Spleen        spleen           67402    12%      9  12.3 7.2 5.7 3.7 1.8 2.9 2.0 1.2 1   39773   59%  last 3 rounds each removed < 2% (1.98%, 1.24%, 1 | inspect_flag=321 low_confidence=63 lineage_skipped=18 annotation_boundary=13 removed=11 plan_warning=5
Tabula Sapiens / Stomach       stomach          32671    12%     10  21.6 11.3 6.0 1.9 11.8 2.0 5.0 4.9   13375   41%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=327 low_confidence=49 lineage_skipped=40 removed=11 annotation_boundary=11 convergence=3 plan_warning=3
Tabula Sapiens / Testis        human-testis      7512     5%      4  4.2 5.2 10.9 0.4                      5771   77%  removed 0.43% < 1% | inspect_flag=30 lineage_skipped=15 low_confidence=7 annotation_boundary=4 convergence=1 removed=1 plan_warning=1
Tabula Sapiens / Thymus        thymus-human     41261    14%     10  10.5 5.6 7.3 10.9 2.5 2.9 5.2 2.3    21096   51%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=327 low_confidence=53 lineage_skipped=43 removed=12 annotation_boundary=11 convergence=2 plan_warning=1
Tabula Sapiens / Tongue        human-tongue     37175    37%      8  19.3 8.3 3.7 1.7 5.5 1.1 1.3 1.0     14975   40%  last 3 rounds each removed < 2% (1.10%, 1.26%, 1 | inspect_flag=158 lineage_skipped=47 low_confidence=40 annotation_boundary=12 removed=7 plan_warning=5
Tabula Sapiens / Trachea       trachea          21848    15%     10  10.5 6.7 6.8 2.9 3.0 4.1 5.1 4.2 5   10946   50%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=226 low_confidence=62 lineage_skipped=47 annotation_boundary=28 removed=9 convergence=1 plan_warning=1
Tabula Sapiens / Uterus        uterus           21357    15%     10  22.2 3.8 10.4 6.0 3.4 1.9 5.3 3.0     9597   45%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=251 low_confidence=46 lineage_skipped=45 annotation_boundary=15 removed=12 convergence=2 plan_warning=2
Tabula Sapiens / Vasculature   vasculature      40511    13%     10  11.2 6.5 6.5 4.7 2.6 2.2 4.0 7.1 3   20469   51%  FORCED FORCED: safety cap 10 rounds reached | inspect_flag=370 low_confidence=94 lineage_skipped=32 removed=12 annotation_boundary=11 plan_warning=2 convergence=1
```

## 5. What this suggests (for decision, not yet applied)

1. **Removal budget instead of a round cap.** 13 of 28 units hit the 10-round safety cap removing 2–7 % per round. The
   gen-1 design already states the policy ("删除预算越线不停机, 转保守: 边缘删除降级为 flag"); the loop does not implement it.
   Candidate: after round 3 (or once cumulative post-QC removal passes ~25 %), removals with confidence below high become
   flags (`needs_review`), not deletions; convergence then arrives by construction.
2. **fragment_qc needs a ceiling.** It is a host rule, not a model judgment, and it is the single largest sink. Candidate:
   per-round fragment removal capped at a few percent of the lineage, and fragments re-evaluated only when their parent
   cluster changed.
3. **Coarse labels should carry over.** The type session should start from the previous round's labels and change one only
   with stated evidence; synonyms must be normalised before comparison. A label-stability term could join the stop rule
   (cells removed < 1 % *and* coarse labels changed < 5 %).
4. **OSP hard QC is a separate lever** (24–83 % removed before any model sees the data in kidney, liver, small/large
   intestine, heart). The mt cutoff 15 → 25 % (OSP 0.1.7) is running in the gen2 batch started 22:33; compare its
   per-sample column against this table when it lands.
5. **Where the decision points live**: `fragment_qc` is msp / standissect-lite's `minor_sibling_qc.csv`
   (`recommend_removal` per subcluster fragment) applied wholesale by the host in `stages/zoomin.py` (apply) and
   `stages/crosssample.py` (finalize) as a numerical removal that no model ever judges; `cell_outlier` likewise from
   `cell_outliers.csv`. Model removals (stress, dissociation, dying, doublet, low-quality) come from the quality
   proposals. The stop rule is `ecarsi/round_policy.py` + `control/dataset.py`; the prompts are
   `ecarsi/prompts/*-checklist-v4.md`. None of 1–3 is a big change; each is one rule plus a test.

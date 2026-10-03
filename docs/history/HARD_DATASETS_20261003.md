# Hard datasets: why they failed (2026-10-03)

Read-only investigation of the datasets that failed or behaved badly in the September batches: the mouse PanSci
organs, Hua Heart / integrated, 3CA Li2019_skin and four other 3CA datasets, plus the Parse "parse-5M" collection
(4 of 13 released; the 9 large ones never run). The question was whether the cause is the data (size,
non-droplet platforms) or the way the pipeline drives its agents (long contexts). The answer is mostly the
data, through one mechanism: **the sample unit**. Wrong sample units then overload the agents.

Sources: the obs tables of the ECA-PP inputs (h5py, no matrices), the released display zones under
`$OAK/eca-rsi/display/`, the current `bridge/requests` (303 turns, all Shi runs since 10-01), the September token
summary (`$OAK/eca-rsi/work/_batches/gen2-acceptance-20260917-tokens-final.json`), `OPEN-ITEMS.md`, the code at
the deployed commits, vendor and paper documentation (links at the end). Scripts: `/tmp/zl/hard/` (not kept).

## 1. Attribution

| Dataset | Platform | What went wrong | Category |
|---|---|---|---|
| PanSci lung_WT_p2of5, duodenum_Prkdc (first runs) | EasySci combinatorial indexing (nuclei) | `batch` (a PCR/ligation well, ~90-900 cells, every well holds every age and sex) taken as the sample: 189-380 "samples", 4,103 OSP agent turns (rank 1 of 209 datasets), inclusion tool output over the 256 KiB handoff | data → agent |
| PanSci heart_Rag, lung_Rag, liver_Rag, Prkdc | same | `sample_id` = Age_group × Gender: 4 "samples" of 18k-89k nuclei each. Per-sample OSP died at 8 GiB (heart_Prkdc needed 32 GiB). First attempts also hit a 120 s coordinator timeout (see Hua) | data (size) + infrastructure |
| all PanSci released since 9-22 | same | the cross-sample `batch_col` is `eca_sample_id`, i.e. Age_group × Gender: **Harmony corrected across ages and sexes** | data (science) |
| Hua Heart / integrated | 10x, two donors | 2 donors of 75k and 85k cells; donor ≡ chemistry (Donor1 all 10X-V3, Donor2 all 10X-V2). Attempt 2 died at 8 GiB per sample. Attempts 1 and 3 timed out at the per-sample start | data (size) + infrastructure |
| Li2019_skin | MARS-seq | true samples: `sample` (23) / `patient` (22); the run's 196 "samples" match `amp_batch` (198 amplification batches, 36 % of which hold more than one patient). Inclusion then needed ~510k tokens (196 required UMAP reads) against a 256k window | data → agent |
| Griffiths (62 samples), Chen2021_validation (55), Young2018 (49), Raghavan ×2 (48) | 10x / inDrop | correct units, but inclusion requires one UMAP read per sample and re-sends every image each turn; overflowed until paging (Griffiths passed at 234k of 256k) | agent |
| parse-5M, 9 large organs | Parse Evercode WT Penta (nuclei, ~10k reads per cell) | each file is **one** biological sample (Parse's atlas used one male and one female mouse), 212k-465k nuclei. The prepared sample maps use `sublibrary` (32 technical splits, each containing the whole sample) as the sample | data |
| parse-5M, 4 released | same | inconsistent: brain_female and quadricept_male split per sublibrary, brain_male and colon_female run as one sample | data |

## 2. The data side

**Sample units by platform.** In split-pool platforms only the first barcoding round is sample-specific:
Parse's round-1 well (bc1), EasySci's RT well. Later rounds (bc2/bc3, ligation, PCR wells) and Parse sublibraries
pool every sample, so they are technical units and must never be the sample or the Harmony batch. Doublets in
these platforms are barcode collisions or clumps that travel together, so they occur within one round-1 well.

**PanSci.** Our files (from the UCSC Cell Browser export) carry no per-mouse `ID`, although the authors group
replicates by it. The last two segments of the cell name are sample-pure: every value belongs to exactly one
Age_group × Gender group (heart_Rag: 64 values, median 2,751 cells; lung_Rag: 49). They are the RT wells, the
finest unit that is certainly one sample. `sample_id` = Age_group × Gender pools all RT wells, and probably several
mice, of a group, and is also the batch Harmony corrects. Every PanSci release built on it has had its age and sex
differences treated as batch effects.

**parse-5M.** [Parse's 5 Million Mouse Single Cell Atlas](https://www.parsebiosciences.com/datasets/5-million-mouse-single-cell-atlas-from-7-tissues/):
7 tissues from one male and one female mouse, nuclei, WT Penta, 32 sublibraries, ~10k reads per cell. Each of our
13 files is one sample. No sample column survives the conversion (`parse-5M/to_h5ad.py`), only `bc1_well`
(2-10 asymmetrically loaded wells of 12k-59k nuclei) and `sublibrary` (32 of 0.7k-17k). The conversion also
applied a 10x-level filter (≥500 genes, ≥800 transcripts) before ECA-PP, which is strict for shallow nuclei data.

**Hua Heart.** `sample.source` (Donor1/Donor2) is the sample. It cannot be separated from chemistry: any batch
correction across donors also corrects V2 against V3 and vice versa.

**Per-sample size and memory.** Per-sample OSP memory grows with cells per sample. The PanSci specs gave 8 or
16 GiB; heart_Prkdc needed 32 GiB (peak 24.7 GiB), Hua's 75k-85k died at 8 GiB. Since 9-18 an RSS kill is retried
at twice the budget, at most twice (8 → 16 → 32 GiB, `control/coordinator.py:139-148`), so samples up to roughly
this size now cost wasted attempts rather than a failed dataset. A parse-5M organ as one sample (up to 465k nuclei)
is beyond that.

**QC fit** (per-sample stage of the released runs, `release/cell_exclusions.csv.gz`):

| Dataset | per-sample removed | of which hard thresholds | doublets flagged |
|---|---|---|---|
| PanSci heart_Rag / lung_Rag / liver_Rag / ileum_p1of3 | 10.8 / 4.4 / 4.9 / 7.0 % | 725 / 0 / 0 / 62 cells | 0.9 / 3.2 / **0.1** / **0.1** % |
| Chen2021_colorectal_validation (inDrop) | **62.5 %** | 34,816 of 35,674 | 0.6 % |
| Young2018_kidney | 22.6 % | 10,649 | 0.4 % |
| Griffiths2021 (10x) | 3.6 % | 0 | 0.3 % |

OSP uses `hard_min_genes=200`, `hard_min_counts=500`, a 25 % mito cap, MAD outliers and Scrublet per sample
(`osp/qc.py:330-336`). The Cao lab keeps ≥200 UMIs and expects 6-10 % doublets in sci/EasySci data; doublet rates
of 0.1 % in liver_Rag and ileum mean Scrublet found no threshold and flagged nothing. inDrop's low counts are cut
by the hard thresholds.

## 3. The agent side

**Mechanism** (code at 0.4.3): every turn re-sends the whole conversation (all tool outputs, every rejected
proposal, every image as base64); nothing truncates it, there is no per-turn or per-session cap, only per-call
limits. A context overflow surfaces as `provider_error`, the same name as 429s, 5xx and local exceptions; it is
retried (3 attempts), reset (twice) and restarted (`-r2`) although it is deterministic, and each failure puts the
model into a 300 s cooldown for every dataset. `OPENAI_AGENTS_MAX_CONTEXT_RESETS` and its siblings do not reach the
production sessions (they run `session.run_turn`).

**Measured** (current bridge, Shi; all 303 turns `reply_saved`): largest context per session

| Session | sessions | turns (max) | largest context (median / max) |
|---|---|---|---|
| organize.plan | 5 | 2 | 3k |
| osp.annotate | 10 | 7 | 34k / 41k |
| cross-sample.inclusion (2 samples) | 5 | 5 | 11k / 12k |
| cross-sample.type (21-30 clusters) | 12 | 9 | 35k / 51k |
| cross-sample.quality | 12 | 5 | 31k / 53k |
| zoom-in.plan | 11 | 2 | 3k |
| zoom-in.lineage (20-26 clusters) | 12 | 13 | 39k / 73k |

**Cells do not drive the context; samples do.** The released PanSci matrices of 100k-200k cells end with 15-43
clusters at resolution 2.0 (Shi: 16-30), so their type, quality and lineage sessions stay in the range above. What
grows with the data is the number of samples: inclusion needs every sample's UMAP and inventory entry (Griffiths,
62 samples: 234k; Li2019, 196: ~510k), and the number of per-sample sessions (one per sample: 4,103 turns for the
well-as-sample PanSci run). A wrong sample unit multiplies both.

## 4. Infrastructure (all fixed)

- Hua Heart attempts 1 and 3, and the first attempts of heart_Rag and lung_Rag, died on a coordinator activity
  timeout (120 s) on 9-22. The activity at that point writes `spec.json` and submits one request; it does not read
  the matrix. The cause: on 9-22 every per-sample workflow with a failed sample globbed and read every
  `request.json` of the pool and the bridge every 30 s (#19, ~100k folders), which stalls the coordinator node's
  Lustre client; any activity on that node could miss 120 s. Fixed 10-02 (lookup by name).
- Also fixed: scratch inode quota (ileum, 9-23), an edit in the live tree (liver_Rag), a release crash on an
  inspection proposal carrying `types` (lung_WT_p5of5), Temporal's history limit (zoom-in continue-as-new).

## 5. Fix candidates (not implemented), by impact and risk

1. **Sample units right before anything runs.** Host-side check in organize: a sample column whose values mix
   ages, sexes, donors or genotypes (the "well test") is refused; very many tiny levels warn. Explicit sample maps
   (0.4.1) for the known cases: PanSci RT well (or mouse ID if GEO provides it), Li2019 `sample`, parse-5M a
   technical split with `batch_key: false`.
2. **PanSci releases.** Decide whether to rerun with a batch that is not biology (RT plate, experiment, or none)
   and smaller sample units; the current releases corrected across age and sex.
3. **Technical splits skip inclusion.** When the sample map declares one batch (`batch_key: false`), the units
   are processing chunks of one sample: there is nothing to include or exclude.
4. **Per-sample memory from cell count**, so 50k-cell samples start with the budget they need instead of
   reaching it through two killed attempts.
5. **Overflow handled as what it is**: classify `provider_error`; a deterministic overflow fails at once with its
   reason, with no reset, no restart and no model cooldown.
6. **Inclusion without N image reads**: a host montage or summary; images not re-sent after they were read;
   rejected proposals not kept verbatim in the history.
7. **Platform QC presets**: nuclei and combinatorial (≥200 counts, MAD-based mito), inDrop; a Scrublet run without
   a threshold recorded as a degraded step; for parse-5M, revisit the pre-filter of the conversion.

## Sources

- Parse technology and split-pipe output: https://www.parsebiosciences.com/technology/ , https://hpc.nih.gov/apps/spipe.html , https://github.com/mortazavilab/parse_pipeline
- Parse 5M mouse atlas: https://www.parsebiosciences.com/datasets/5-million-mouse-single-cell-atlas-from-7-tissues/
- Parse multiplets and QC: https://pmc.ncbi.nlm.nih.gov/articles/PMC11552371/ , https://pmc.ncbi.nlm.nih.gov/articles/PMC10743076/ , https://github.com/plger/scDblFinder/issues/90
- PanSci (EasySci-RNA): Zhang et al., Science 2025, https://www.science.org/doi/10.1126/science.adn3949 ; https://github.com/zhangzehao626/PanSci ; EasySci: https://www.nature.com/articles/s41588-023-01572-y , https://github.com/JunyueCaoLab/EasySci
- sci-RNA-seq3 doublets: https://pmc.ncbi.nlm.nih.gov/articles/PMC6434952/

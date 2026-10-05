# 0016 Samples and batches come from ECA-PP; big samples run as chunks

Decided 2026-10-03 by the owner, after the hard-dataset investigation
([HARD_DATASETS_20261003.md](../history/HARD_DATASETS_20261003.md)).

**Context.** The large datasets failed because organize chose the wrong sample. The planning agent picked one obs
column per source with no fixed procedure. It took PanSci's plate wells and Li2019's amplification batches as
samples, and nothing could say that Hua Heart's two 80k-cell donors were 23 hidden 10x libraries. Every wrong
sample multiplied the per-sample sessions and the inclusion agent's context. Meanwhile ECA-PP's identify-columns
step had already *measured* each dataset's batch with Harmony probes, and eca-rsi read its answer into
`eca_pp_batch` without using it.

The owner's position: the data feeds atlases and foundation-model training sets. The goal is to recognise one cell
type across conditions and drop noise. Sex, age, chemistry and donor are all legitimate batch signals. And the
system must cope with very large data that has no batch at all (Parse, PanSci) without hand-made random splits.

**Decision.**

- **ECA-PP finds the batch on a fixed ladder** (identify-columns 0.5.3, `docs/identify-columns-spec.md` §10.6 in
  eca-pp). Rung 1 is existing technical and donor columns. Rung 2 is segments of the cell names, including the new
  `head`: everything but the barcode, which turns `Donor1.M1-1.<barcode>` into a library. Rung 3 is conditions,
  sex and their composites. The model orders candidates within a rung; probes decide.
- **ECA-PP knows the platform.** `--platform`, or detection from Parse/SPLiT-seq barcode columns and
  technology-like values. On split-pool platforms, wells, sublibraries and cell-name segments are never a batch,
  and "no batch" is a normal result. On droplet-like platforms, ECA-PP also reports a `library`: the finest
  existing column or cell-name segment nested in the batch whose groups fit one library (200 to 30,000 cells).
  This keeps per-library QC when the adopted batch is coarse. Against the September 3CA releases it gives back
  the `sample` of Wu2020, Yost2019 and Wang2019 (whose adopted batch was patient or group), and finds GEM-well
  suffixes in the cell names of five more datasets.
- **eca-rsi organize takes each source's samples from ECA-PP** (`stages/upstream.py` `eca_pp_decision`). The
  sample, i.e. the per-sample QC unit, is ECA-PP's library, else its batch, else the whole source. The unit's batch
  is ECA-PP's batch when it recommends the correction; otherwise the unit is one batch with no Harmony.
  - Order of precedence: the owner's `organize.sample_map` first, then ECA-PP, then the organize agent. The agent
    decides only sources without an identify-columns result, or whose ECA-PP column leaves cells unassigned.
  - A source with no batch and no library is one sample only on a split-pool or plate platform, or when it fits
    one library. Otherwise organize stops and says to name the sample in the sample map or re-run identify-columns
    with `--platform` (owner's choice: no silent fallback).
- **Samples above 20,000 cells run per-sample QC as chunks** (`sample_mapping.chunk`). Chunks are assigned by a
  stable hash of the original cell ID and named `<sample>.chunkNN`.
  - Each chunk keeps its sample as its batch, so Harmony never corrects between chunks.
  - A unit with chunks skips the inclusion agent and includes every sample: chunks are random slices, and nobody
    can judge which slice to drop.
  - `sample_map.chunk_cells` overrides the size.
- **Whole-matrix steps are sized from their input**, never below the spec (`stages/resources.py` `from_cells`, was `warm_pool/budget.py`;
  `from_artifact` for partition; organize execute from its source files). Before this, a large sample reached
  enough memory only through two killed attempts (8 → 16 → 32 GiB) and then stopped.

**What it costs.**

- Every dataset's samples now follow ECA-PP. A dataset whose ECA-PP result predates 0.5.3 has no platform and no library, so a
  large source with no batch stops at organize until identify-columns is re-run
  (`scripts/rerun-identify-columns.sh` in eca-pp).
- Chunks are random. Per-sample QC thresholds are population statistics and survive random slicing at 20,000
  cells. Doublets of a split-pool assay sit within a round-1 well, which a random chunk keeps in proportion.
- With chunks, inclusion cannot drop a bad real sample. The cross-sample inspect and annotate steps still remove
  bad cells.
- Release condition C (fewer than 1,000 cells removed in a round) is unchanged. A 400k-cell unit may only release
  at the round cap.
- The per-cell constants for cross-sample and zoom-in compute are estimates (no journaled run held more than ~10k
  cells). Re-measure them from the first large run's journal.

**Checked against the September releases** (read-only, 2026-10-03). The check covered all 207 sources of the 182
gen-2 runs in the display zone, using the ECA-PP results on disk (0.5.1 and older) and the 0.5.3 library rule.

- **Unseen until now.** Those runs never saw ECA-PP's answer. The specs name the `standardize/` folder as the input
  root, and `identify_columns/` sits beside it. `discover` now looks there too.
- **Same as before.** 107 sources get ECA-PP's batch; 96 of them pick the column the runs used (mostly `sample`).
  With the library rule the remaining coarse ones get their `sample` back.
- **Stop at organize.** 72 sources stop until identify-columns is re-run at 0.5.3: 69 PanSci organs (re-run with
  `--platform split-pool`), 3 chondroatlas sources whose results are 0.2.0, and the 3CA Gonzalez2022, Jansky2021
  and Nam2019 (31k-94k cells, no batch found, no platform recorded).
- **Agent decides.** HCL's 19 sources have no identify-columns result; the planning agent keeps deciding them.

**Amended 2026-10-05.** ECA-PP identify-columns 0.5.4 writes the verdict itself: `sample_unit` (library, batch,
whole or stop, with its reason) and `n_obs`. `stages/upstream.eca_pp_decision` only maps it to columns and keeps no
copy of the 30,000-cell and split-pool/plate rule; the result file is checked as contract
`eca-pp-identify-columns/1`. A result from before 0.5.4 still gives its library or batch; a source with neither
stops until identify-columns is re-run.

# Step: explore — probe the state, plan the round

Decide what this round should work on. You plan; you do not label, clean, or
modify anything.

## Round 1 (no checkpoint yet): survey the input folder from scratch

The input files are not self-describing — assume nothing about how they
relate. Probe (with your own short scripts, saved to the round dir):

- Each file: shape, obs columns + example values, whether X is raw counts or
  normalized, species, gene-name style.
- Between files: shared genes and **shared barcodes**. Shared barcodes with
  identical expression = the same physical cells in two files (e.g. one file
  is a compartment extracted from another) — merging those would double-count
  cells, so one of them must be excluded. Shared barcode *strings* with
  different expression are just independent runs reusing names.
- Decide which files form the working dataset (and which are excluded, with
  the reason), which obs column is the sample/batch key, which columns are
  biology to preserve (condition, tissue...). If the folder spans strata that
  must not be co-embedded (organs, species), plan per-stratum work.

## Round ≥ 2: read the record, then plan

- Read the previous round's reports (`explore/compute/annotate/qc/apply/stop.md`)
  and its open items. Verify the checkpoint's current cell count matches what
  apply reported; a mismatch means something edited the data outside the loop
  — report it and plan nothing else.
- Carry unresolved items forward; drop what got resolved; add what the
  previous round's findings make urgent.

## Report (plan.md is your report — write it as `explore.md`)

- What you found (round 1: the file map and merge/exclude verdicts with
  evidence; later: what changed last round).
- This round's scope: whole dataset, or a named subset (say exactly which
  cells, by which obs column and values).
- 3–6 concrete goals, each naming its target. Fewer, sharper goals beat a
  list of everything.

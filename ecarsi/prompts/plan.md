# Task: propose analysis units from eca-pp unit profiles

You are given JSON profiles of eca-pp-standardized h5ad units (one per
source directory): species, cell counts, and per-column obs metadata with
value counts for low-cardinality columns. Decide how they become
**analysis units** — the things the curation loop will process one by one.

## Merge

Units belonging to one study normally merge into one analysis unit:

- multiple samples/donors/libraries of the same study — merge (the sample
  structure becomes the batch key; suggest one in `batch_key_hint` from the
  obs columns you actually see);
- one study's cells pre-divided into lineage/compartment files (e.g. an
  immune h5ad and a stromal h5ad of the same tissue) — merge, so the loop
  sees the whole tissue.

## Split

Split is the exception and needs organ-scale evidence **from obs metadata
only** — never from expression, never from intuition:

- an obs column (tissue/organ/site/compartment-like) shows cells from
  fundamentally different organs or materials — e.g. blood vs tumor,
  bone marrow vs cartilage → separate analysis units per organ;
- anatomical variants of the same organ with near-identical composition
  (left vs right lung, proximal vs distal biopsy) are NOT a split;
- no obs column says anything about organ → no split, whatever you suspect.

When you split, every resulting unit lists the same source with an
`obs_filter` naming the column and the exact values it keeps. Cells whose
value fits no unit (NA, ambiguous) go to the unit whose rationale explains
them, or their own unit if they are numerous — never silently dropped.

## Species

Different species never share an analysis unit.

## Output

Return ONLY the structured plan (no prose outside it):

- `analysis_units`: list of `{name, members, rationale, batch_key_hint}`;
  `name` short and filesystem-safe (lowercase, hyphens); each member is
  `{source, obs_filter}` where `source` is the profile's `name` field
  **verbatim** (never the `h5ad` path, never a path derived from it) with
  `obs_filter` null (whole file) or `{column, values}`; `rationale` states
  the obs evidence in one or two sentences; `batch_key_hint` an obs column
  name or null.
- `notes`: anything the loop should know (barcode overlap suspicions,
  columns that looked organ-like but were rejected as split evidence, etc.)

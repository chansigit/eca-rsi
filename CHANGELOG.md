# Changelog

## 0.2.10 — 2026-09-12

- Add `pause` and `pause_after_stage` controls, drain running sample/lineage workers and preserve exit 3 through the driver.
- Require MSP 0.5.1 / ZMIP 0.3.9 / bridge 0.2.14 for validated partial annotation recovery and cooperative pause.
- Integrate recorded-lineage model evaluation with hashed fixtures, fresh candidate work directories, failure records and production output validation.

## 0.2.9 — 2026-09-12

- Keep bridge version/source as agent provenance, outside scientific runtime identity; retain provider-qualified names.
- Fix developer resume to verify reused OSP receipts with their original identity and preserve skipped runtime checks.
- Surface uncertain MSP coarse boundaries and written ZMIP island reviews; round one no longer receives an over-budget convergence flag.
- Require the bridge 0.2.13 / MSP 0.5.0 / ZMIP 0.3.8 combination and document the manual CPU/GPU pool scope.

## 0.2.1 — 2026-09-07

- Source provenance tolerates a missing `git` binary (slim containers): the commit is recorded as null instead of
  failing persample. Found on the first Apptainer-env run (calico-aging kidney).

## 0.2.0 — 2026-09-07

- Sample-map cell policies (`ecarsi.policies`): `exclude_cells` rules applied before any OSP subset is cut (every
  excluded cell on the ledger as `removed:persample-policy:<reason>`, listed in needs_review) and a declared
  `batch_key` (validated constant per experiment, back-filled for blank cells, passed to MSP). Without a map the
  sample-column agent may propose exclusions (host-validated) and a separate call only *recommends* a batch key.
- Run identity compares content only (package version + source hash); checkout path and git HEAD are recorded as
  `provenance`. Doc-only commits or a relocated worktree no longer invalidate a resume.
- `loop`: manual overrides via `<unit>/loop_control.json`, re-read at every round boundary (`cap`, `rounds`,
  `extra_rounds_after_convergence`, `stop_after_round` → pause with exit 3); the round loop is a `while`.
- A sample whose OSP QC removes every cell is finished-and-empty: accounted in `qc_removed.csv`, auto-excluded
  before the inclusion agent, listed under needs_review; the loop prerequisite accepts it.
- `organize` ignores ECA-RSI run roots mirrored inside the ECA-PP input tree; sample maps gain
  `derive_from_cell_id` and `missing_as` for explicit experiment partitions.
- Landing pages: one design system, overview page and navigator grouped by collection; step state derived from
  light markers only, so a `--mirror` copy without h5ad shows the same stage as the run root; Sankey stage titles
  vertical, labels decluttered, big nodes centred.
- `serve`: access log with the visitor's address (`X-Forwarded-For` behind ngrok) and user agent; resizable
  sidebar, sorting, no Slurm-specific column.

- `--mirror DIR` on `run` / `organize` / `persample` / `loop` (`ecarsi.mirror`): remembered in `<root>/mirror.json`;
  light files copied to DIR after every landing-page write, the whole root at release (with pruned files removed
  from DIR's copy of that unit only). Page footers carry a `run state updated <time>` stamp derived from state-file mtimes.
- Derive a study-design text per unit (`ecarsi.design`: obs columns constant within each sample)
  and pass it to MSP and ZMIP as `--design-context` in every round. Agent context only; not part of run identity.

## 0.1.0 — 2026-09-05

Initial PyPI release of the ECA-RSI workflow driver.

- Connect ECA-PP products to OSP per-sample processing and iterative MSP/ZMIP analysis, with explicit sample identities, upstream status checks, removal ledgers, and resume validation.
- Require the published bridge 0.2.3, OSP 0.1.2, and MSP/ZMIP 0.3.3 compatibility baselines; install kernels with `ecarsi[kernels]`.
- Embed final UMAP data in unit HTML pages for offline viewing, with adaptive point sizes, zoom, hover, and legend selection.
- Default report prose to English; other languages require explicit configuration.
- Preserve current stress and mitochondrial removal policies. A separate processing-stress policy remains under discussion.

Validation includes repository tests, installed-package checks, and offline browser interaction checks. Earlier real-data acceptance covers a two-round RSI run on Clayton and a separate full-size MSP/ZMIP run on 19Liu; the latter is not a full RSI release or a rerun of all model decisions on this release.

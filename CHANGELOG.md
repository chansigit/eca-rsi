# Changelog

## Unreleased

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

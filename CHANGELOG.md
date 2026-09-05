# Changelog

## 0.1.0 — 2026-09-05

Initial PyPI release of the ECA-RSI workflow driver.

- Connect ECA-PP products to OSP per-sample processing and iterative MSP/ZMIP analysis, with explicit sample identities, upstream status checks, removal ledgers, and resume validation.
- Require the published bridge 0.2.3, OSP 0.1.2, and MSP/ZMIP 0.3.3 compatibility baselines; install kernels with `ecarsi[kernels]`.
- Embed final UMAP data in unit HTML pages for offline viewing, with adaptive point sizes, zoom, hover, and legend selection.
- Default report prose to English; other languages require explicit configuration.
- Preserve current stress and mitochondrial removal policies. A separate processing-stress policy remains under discussion.

Validation includes repository tests, installed-package checks, and offline browser interaction checks. Earlier real-data acceptance covers a two-round RSI run on Clayton and a separate full-size MSP/ZMIP run on 19Liu; the latter is not a full RSI release or a rerun of all model decisions on this release.

"""Presentation: landing pages, the Periscope server, the UMAP extract.

A module may live here when changing it can only change how a run is displayed: `stages/release.py`
calls `umapdata.write_umap_json` to put `release/umap.json` next to the final matrix, a picture of the
result and not part of it. Anything that decides which cells survive belongs outside `ui/`."""

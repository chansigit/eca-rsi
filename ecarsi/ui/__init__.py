"""Presentation: landing pages, the Periscope server, the UMAP extract.

This subpackage is deliberately outside `runtime_identity()` and
`downstream.runtime()` (eca-rsi#10). Those hash every file of the compute
package so a stage can prove it resumed on the same code — which also meant
that editing a stylesheet in `serve.py` invalidated whatever stage happened to
be running `verify()` at that second. It cost two real re-computations on
2026-09-07 (tome E9.5 round 3 and E8.5b round 2 zoom-in), and the workaround
ever since has been `chmod -R a-w` on four checkouts for the length of a batch.

The line is *scientific* result, not "never imported": `stages/release.py` calls
`umapdata.write_umap_json` to put `release/umap.json` next to the final matrix,
and that is fine, because the file it writes is a picture of the result and not
part of it. A module may live here when changing it can only change how a
finished run is displayed. Anything that decides which cells survive belongs in
the hashed tree, whatever it looks like.
"""

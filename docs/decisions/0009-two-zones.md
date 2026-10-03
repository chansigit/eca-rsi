# 0009 A run has a work tree and a display zone

Accepted, 2026-10-02 (eca-rsi#25).

**Context.** A finished run is ~30,000 files, most of them session and step records the system needs only to
resume and replay. Copying whole run trees to Oak filled a group's inode quota. Yet the results people read
must never be lost.

**Decision.** The work tree (`output_root`, on scratch) holds everything needed to resume and replay. The
display zone (`<display_root>/<collection>/<dataset>/<run_id>/`) holds what Periscope shows: the pages' files,
the stage reports, the release, defined by what the renderer actually reads. A small pool task syncs it after
every stage; the final sync also archives the work tree as one `.tar.gz`. Syncs are not awaited, and a failed
sync never fails a run.

**Consequences.** Results are durable and cheap (40–150 files per run); the work tree can be archived or
discarded by policy. Periscope serves display zones, so a running dataset is visible from its first stage.
Old runs were normalized the same way. Code: `ecarsi/display.py`, `ecarsi/stages/archive.py`,
`ecarsi/stages/display.py`.

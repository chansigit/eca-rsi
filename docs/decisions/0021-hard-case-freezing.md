# 0021 Hard agent steps are frozen before their evidence is pruned

Decided 2026-10-06 by the owner (#51, step 1 of #14): batch 2 must keep its hard cases, the material for
comparing base models later.

**Context.** An agent session reads evidence that lives in the run's pool requests: its tool state, the
evidence bundle that names every file, the H5ADs, DEG inputs, figures and tables. `container/request-pruner.py`
deletes those requests once the dataset workflow completes, usually within the hour. The session folder in the
run tree survives, and with it the conversation, but a replay with another model needs the files: a different
model asks for different tool calls.

**Decision.** The run's `published` display sync (`ecarsi/stages/display.py`) first runs
`ecarsi/stages/cases.py::freeze`.

- *Ordering without new machinery.* The dataset workflow awaits that sync before it completes, and the pruner
  only touches completed runs, so the freeze always comes first. No shared component changes. A failed run's
  `published` sync is not awaited, but its requests stay a week after a later run of the dataset completes.
- *What is hard.* A session that died and restarted (`restart.json`), lost its transcript to a context reset
  (`context-reset-N.json`), or had at least `REJECTIONS` (2) submissions rejected by the host. A skipped sample or
  lineage always restarted first, so it is covered. The restart and reset generations stay with the session
  they continue.
- *What a case keeps.* The session folder, and every file its records reach within `EVIDENCE_DEPTH` (3)
  references: tool state, evidence bundle, the bundle's files and the references inside the JSON files among
  them; model replies, tool results and pinned programs. Deeper references lead upstream (the organized
  matrices) and are left out.
- *Where.* `<archive_root>/_cases/<collection>/<dataset>/<run>/`: `cases.json` (frozen and skipped cases with the
  reason), `files/<sha256>` (each file once per run, checked against its reference while copied),
  `<session path>/case.json` (why it is hard, the model, every file with its sha256 and size, the missing ones)
  and `<session path>/session/`.
- *Bounds.* `CASE_BYTES` 24 GiB per case and `RUN_BYTES` 96 GiB per run; a case over either is listed as
  skipped, not copied. Measured on batch 1 (in progress, 2026-10-06): hua-heart (160k cells) 6 hard sessions,
  12.3 GB after deduplication, the largest a cross-sample type session at 7.5 GB (integrated.h5ad 5.0 GB, DEG
  inputs 2.4 GB); kidney_Rag_p3of3 3 sessions, 1.6 GB; BAT_Rag 2 sessions, 2.3 GB.
- *Never fails a run* (0013): an exception is a `degraded` record with stage `published`.

**Consequences.** Every completed run leaves its hard cases on Oak beside its work archive; the gate run
leaves its own in the test archive root. Recurring reassignments are not a rule of their own yet: those the
D7 guard rejected count as rejections. Changing a rule or a bound is a change of the module's constants.

# Changelog

## 0.3.1 — 2026-09-17 (branch `gen2`)

The second generation (Temporal control plane, HyperQueue warm pool, durable model-turn bridge, stage programs),
developed as feature/warmpool-v2 since 2026-09-14, integrated on one branch and given a package structure. The first
generation (`eca-rsi run`, Slurm pool, batch admission) keeps its modules where 0.3.0 left them.

- Move the flat gen-2 modules into `ecarsi.control` (work_coordinator as `control.coordinator`, temporal_service, *_workflow), `ecarsi.agent`
  (agent_bridge, agent_session, agent_dispatch, …; named `agent` so that `bridge` means only the external
  agent-harness-bridge package), `ecarsi.stages` (organize/persample/crosssample/zoomin `_v2`, dataset_release),
  `ecarsi.warm_pool.budget` (operation_budget) and `ecarsi.observatory` (dev_observatory); `python -m
  ecarsi.control|agent|observatory` entry points.
- `reference` / `verified` / `immutable` belong to `warm_pool.state`; pinned program files come from `stages.program()`,
  the pinned adapter from `agent.adapter_path()`.
- Fold the v3 protocol wrappers into the stage programs: one program and one contract per stage, `stages/contract.py`
  holds what they share (no-argument listings, deg_lookup thresholds, lenient proposal parsing, checklists).
- Stages plan their own tool execution: `stages/evidence.py` and `stages/execution.py` (formerly agent_evidence /
  agent_tool_execution) are registered by the session as its `planner`; the model-turn service imports nothing from stages.
- Retire the first generation's batch admission on this branch: `eca-rsi batch`, the node agents, OSP compute-ahead,
  the stage runtime builder and the driver memory leases (batch, preparation_offer, prepare_osp, build_stage_runtime,
  stage_python, driver_python, runtime_logging, driver_budget, workflow_web). Datasets are admitted by the Temporal
  control plane; Periscope keeps its dataset and pool views.
- Absorbed from the batch: replay of already-saved pool/bridge requests, inode-keyed settled caches, poll tolerance and
  120 s host steps, saved-program resume, repeat-rejection stop, protocol v4 (inline evidence, single-call paged tools,
  lenient JSON, no finalize step), GPU columns in the status report.
- Protocol v4 tools without arguments tolerate an ignored `offset`, the type-context hint no longer asks for pages, and a
  rejected cross-sample submission names the missing DEG query or figure.
- The observatory defaults to `<root>/pool` and `<root>/bridge` and takes `--pool-root` / `--bridge-root` from the
  control-plane template; `ecarsi.control` imports nothing heavy, so the host-side observatory can read the
  Temporal endpoint without temporalio.
- Stages declare their read-only tools (`read_only` in the session spec; per-sample, cross-sample and zoom-in each
  register their own) and size their tool requests (`stages.evidence.budget`); the model-turn service keeps no list of
  stage tool or module names, only the read-only flag and the `{state}` handoff decide what may run in parallel.
- Accepted 2026-09-17 evening on a fresh run directory (Tabula Sapiens ear, testis, kidney; two pool nodes): organize →
  per-sample → cross-sample → zoom-in → round 2 with no failed workflow, a coordinator restart in mid-session that
  every dataset survived, a zoom-in lineage completed by an accepted submit_quality, and the observatory page and APIs
  serving the run; 125 model replies, 0 failed, every rejected submission a host rule the model then satisfied.
- `ecarsi.observatory tokens --bridge-root …` (`control-plane.sh tokens`): per dataset run, model turns and prompt /
  completion tokens summed over the saved replies, by session kind and model; `releases` lists released units.
- Per-sample evidence tables reach the model compacted (`stages.execution.compact_tables`: top 15 markers per cluster with
  two-decimal lfc and pct, top 8 ambient genes, PAGA as a sparse neighbour list; 185k → 24k characters for a 43-cluster
  sample): raw CSV pages overran the provider context at the third turn of a large sample, in every generation.
- A model turn's pool request id includes the turn's content digest: a turn folder re-created under the same name with
  different content no longer replays the earlier reply (a resumed session had replayed 34 archived replies).
- Gen-2 documents move to `docs-gen2/` (plus ARCHITECTURE.md); `container/control-plane.sh` is the launcher template and
  `container/agent-worker-runtime-20260917.json` the science runtime for the new import names.

## 0.3.0 — 2026-09-12

- Add an optional Slurm warm pool with a shared FIFO/resource-fit queue, manually started workers and CPU/memory/GPU/time inventory. No automatic allocation or job cancellation.
- Add OSP compute dispatch with isolated attempts and driver-owned validated publication; retain local execution and driver-side annotation.
- Add local/pool/auto routing, worker drain/loss fencing and live CPU/memory/GPU status. Require MSP 0.5.2 for the optional pool adapter.
- Match Slurm GPU device minors to CUDA-visible devices through UUIDs.

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

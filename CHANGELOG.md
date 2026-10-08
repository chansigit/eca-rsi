# Changelog

## Unreleased

- **A dataset never moves to an older version**: `current_queue` compares the versions' `published` times, so a gate on a candidate newer than current stays on it (the first gate of 0022 moved its unit to the older current version and tested nothing).
- **A running dataset moves to the current version at every child workflow, and four brakes** (#61, decision 0022,
  owner 2026-10-08). `common.start_child` asks the `before_child` activity for the current version's queue (when a
  coordinator polls it) and starts the unit, per-sample, sample, cross-sample and zoom-in children there; each
  per-sample publication and round record names its version and a unit or dataset publication lists `versions`;
  `resume-dataset` resumes on the current version. `loop_control.json` `brake: round|stage|step` (the old `pause`,
  `stop_after_round`, `pause_after_stage` stay as aliases); `step` admits no new sample, stage or round and lets the
  running ones finish; the hard brake `brake <run_id> --hard --reason …` terminates now and keeps everything
  resumable.
- **`ops/worker-node.sh` runs the worker script of `INFRA`'s version**, no longer the image's copy, so a GPU job
  submitted with it joins as a GPU worker (#58) once `INFRA` is 0.4.6 or later.
- **Zoom-in reassign follow-ups (#55):** a proposal with several refused reassignments gets one rejection naming
  every entry (65 lineage sessions of batches 1-2 spent a turn per entry); needs_review `reassigned` items carry
  their cell counts; a recurring move is matched on the cells across every earlier round, not on the latest
  round's target label (1,312 repeat movers were missed behind renamed labels or a quiet round).
- **Tests: one layer in 10-30 s, the whole suite in about 1.5 min** (`ops/test-lane.sh <layer>|all`, pytest-xdist).
  The suite took 6 min on one core; 160 s of it were four cross-sample workflow tests paying `check_pool`'s 20 s
  activity-side wait for each 'waiting' answer, now switched off there (tests/test_long_poll.py covers the wait).
- **ECA-PP's sample column may leave a few cells blank** (#56, owner 2026-10-08): up to 10 % of a source's cells
  (`upstream.ECA_PP_BLANK_MAX`) are dropped before OSP as `eca-pp-blank-sample`, ledgered and listed under
  needs_review `policy_excluded`; before, any blank cell handed the source to the organize agent (Li2019_skin's
  patient column, 5.5 % blank, became 198 amplification batches).
- **The sample apart from the batch** (#56, ECA-PP 0.5.5, owner 2026-10-08): when ECA-PP's batch is a condition or
  sex grouping, or none qualified, its `sample_unit` `sample` names the rung-1 column (PanSci: `sample_id`, age x
  sex) and organize takes the samples from it (`eca_pp_sample`) while Harmony keeps ECA-PP's batch; before, the
  samples of heart_Prkdc and lung_Rag were the age groups. Also fixes f7beb93, which gave every ECA-PP library or
  batch decision without blank cells the whole-source rationale and `confirmed_single`.

## 0.4.6 — 2026-10-08

One repository with the kernels and the agent harness (decision 0018, first numbered 0.4.5), code versions that run
side by side and ship without switching images (decision 0019), one stress panel and mitochondrial rule for every
species (decision 0020), hard agent steps frozen for replay (decision 0021), and a performance pass measured on batch 2
of parse-5M: the Wilcoxon DEG, minor-sibling QC and check_genes take seconds to a minute instead of tens of minutes,
with identical outputs; lookup tool calls start 5-7 s sooner; a rejected proposal can be amended instead of rewritten.
Lint rules and the tests run on GitHub.

- **A GPU job joins the pool as a GPU worker (#58).** `container/worker-node.sh` re-enters a job with a GPU grant as a
  job step that holds its GPUs (`srun --gpus=N --mem=0`) and starts `slurm-worker --gpu` there, as `add-worker` does;
  started in the batch step, a GPU job joined CPU-only and its card idled (batch 2, 46924984: 6 h, 0 % GPU). INSTALL.md
  A.7 gives the GPU `sbatch` line. `ops/worker-node.sh` still runs the image's copy of the script until it follows
  `INFRA`.

- **Per-sample compute asks 0.5 MiB per cell instead of 0.4.** In batch 2, 43 of 222 `osp.compute` attempts on
  20k-cell parse-5M chunks were killed at 8.5-9 GiB and succeeded on retry with peaks up to 10.0 GiB; a 20k-cell
  chunk now asks 10.8 GiB.

- **Wilcoxon DEG ranks only the stored values: 20-40x faster, the same tables to the bit.** Scanpy's
  one-vs-rest Wilcoxon densified every gene chunk and ranked all cells on one thread (`settings.n_jobs` 1): 78 % of
  its time in `rankdata`, the cost of the parent-core DEG (15-31 min of every integration), the DEG pool tasks (164 of
  batch 2's 407 core-hours) and zoom-in's lineage markers (median 19 min). `msp.deg_logging` now replaces
  `_RankGenes.wilcoxon` (scanpy 1.12.4) for sparse X, reference "rest", no tie correction: per gene it sorts the
  stored values only, the cells without one tie at zero (rank known in closed form), and sums ranks per group on the
  task's CPUs (numba, column blocks of 2^26 values). Ranks are halves of integers, so the rank sums are exact in
  float64 and everything after them is Scanpy's own code; other forms run Scanpy's method. zmip's `lineage_markers`
  calls it through `msp.api.rank_genes_groups`. On kidney_male of batch 2 (254,240 cells, 56,406 genes, 314M stored
  values): parent-core DEG (211k cells, 23 parents) 1,235 s -> 45 s on 2 CPUs (36 s on 4), global DEG (30 groups)
  1,292 s -> 56 s (44 s), local DEG (78k cells) 230 s -> 27 s (24 s), lineage markers (12 lineages) 1,497 s -> 49 s
  (37 s); all four tables identical (`DataFrame.equals`, gene order included), lineage_markers.csv byte-identical,
  peak RSS unchanged except the local comparison (+0.4 GB). `tests/msp/test_wilcoxon_parity.py` holds the parity on
  sparse data with ties, negative values, stored zeros and an untested group. The kernel is cached in the pool's
  `NUMBA_CACHE_DIR` like osp's decontx kernels: a later process starts it in 1.4 s instead of 4.0 s.
- **A rejected proposal can be amended instead of rewritten.** `submit_decision` (cross-sample), `submit_types` and
  `submit_quality` (zoom-in) accept `{"amend": true, ...}`: the given entries replace those of the session's last
  parsed submission by identity (clusters by `cluster_id`/`cluster`, samples by `sample`, boundary reviews by their
  label pair; new ones are added), other given fields replace theirs, and the merged proposal is validated whole as
  before (`contract.amended`). Every rejection after a parsed submission says so, and the three checklists ask for
  it. In batch 2, 403 rejected submissions were rewritten whole (10-26k characters each), a fifth of the model time;
  half of all decision and quality submissions were such rewrites. A zoom-in quality rejection's hint no longer
  loses the tool's name to the lineage's.

- **Lookup tool calls no longer import scanpy.** Every agent tool call is a fresh `python -m ecarsi.stages.<stage>
  tool` process, and before this each cross-sample and zoom-in call imported msp's integrate stack (scanpy, anndata,
  sklearn, numba, matplotlib, zarr) before knowing which tool ran. Now `msp`, `zmip` and `osp` load their submodules
  on first use (PEP 562; `from msp import integrate_adata` still works), msp `evidence` imports scanpy only in
  `deg_frame` and msp `annotate` matplotlib only in `_plot`, and the stage `tool()` functions import kernel names in
  the branch that uses them. In the compute image: read_evidence 7.5 s -> 0.8 s end to end, deg_lookup and deg_sql
  7.5 s -> 2.3-2.5 s (they still import numpy, pandas and scipy, 1.7 s), outputs identical;
  `tests/test_tool_imports.py` holds the import set.
- **Minor-sibling QC no longer waits minutes on scipy's exact Mann-Whitney.** For a sibling of at most 8 cells
  without ties scipy computes the exact p-value with a recursion quadratic in U: one test against the ~280k pooled
  core cells of a parse-5M round took minutes, and the step took 3-33 min per integration in batch 2 depending on how
  many such siblings it had. `msp.integrate.fragments` now counts the same null distribution as the coefficients of
  the Gaussian binomial in exact integers, linear per factor and cached per shape: 0.7-3 s, p-values within 3e-13 of
  scipy's (400 random cases), every other test unchanged. On a 294k-cell eye_male round: 503 s -> 4.9 s, the same
  minor_sibling_qc.csv byte for byte.
- **A proposal that does not parse gets a hint at its break.** `contract.json_hint` quotes the text around the
  character the parser names and says what breaks there (a missing or extra bracket, brace, comma or quote); only
  text after the document still gets the old note. In batch 2 the breaks were braces of nested `evidence` objects in
  20-26k-character proposals, and the old note pointed the model at the end of the document.
- **The fractal marker heatmap has a size (#31).** It draws at most 150 marker rows (each parent's first markers;
  `fractal_markers.csv` and the expression tables keep 10 per parent) and stays under 16 Mpx by lowering its DPI. In
  batch 2 it grew to 378 rows x 200 fragments, 9853x12055 px; 22 of its 285 copies exceeded the provider's 36 Mpx.

- **Figures fit the provider's pixel limit (#31).** `stages/common.png_url` scales a figure above 36,000,000 pixels
  (Ark's per-image limit, quoted by its 400 error) down to fit before it reaches the model. In batch 2 a 4237x9631
  figure failed every attempt of an eye_male cross-sample session, on turbo and on the pro fallback, until the session
  restarted.

- **check_genes and check_qc_scores no longer load the expression matrix (#57).** Where integrated.h5ad is written
  (cross-sample integrate and refine, zoom-in lineage compute and refine), `cluster_genes.npz` beside it holds every
  gene's per-cluster expression sum and expressing count (msp `gene_summary`), naming the file it describes;
  check_genes answers from it (`gene_table_summary`, the same text as `gene_table`) and falls back to the matrix
  when the summary is missing or describes another file. check_qc_scores reads obs and uns only. On a 231k-cell
  parse-5M dataset: 54,750 numbers of 300 queries identical, summary built in 7 s (11.5 MiB), a query 45 ms instead
  of 1.3 s plus loading and hashing the 4.3 GiB file; the tools' memory no longer grows with the cell count (the
  RSS-budget retries of check_genes on parse-5M).

- **Every needs_review category comes out of a release (#47).** `tests/test_release_review_categories.py` builds one
  release input that touches every category of `review.KINDS` and requires the gen2 builder
  (`stages/release.review_items`) to produce each of them, so a category can no longer exist only on paper or only on
  the gen1 pages (#46). `review.collect`, the gen1 reader, is documented as frozen: it renders the runs of the layout
  removed in 0.4.0 and decides nothing new.

- **A minimal lint rule set (#48).** `[tool.ruff]` in pyproject selects one statement per line (E701, E702, E703), no
  unused imports or variables (F401, F841) and no undefined names (F821); `tests/test_lint.py` runs `ruff check`
  (ruff 0.16 from the pytest directory). The 483 existing findings are fixed: statements joined by `;` or after `:` are
  split by formatting only those statements (every changed file has the same AST as before), unused imports and
  variables removed. No behaviour changes.
- **Tests on GitHub (#49).** `.github/workflows/tests.yml` installs the control lock's package versions and runs the
  lint rules and the tests outside `SCIENCE_ONLY` (`tests/conftest.py`, collected only without `ECA_TESTS=control`) on
  every push to main or dev and on pull requests; the stale PyPI workflow is gone. Three tests that start Python with a
  clean environment now pass `LD_LIBRARY_PATH` through, which an interpreter with a shared libpython needs.

- **A release convention (#50).** A release is a version whose commit bumps the patch number in `pyproject.toml` and
  gives the changelog's entries their own `## 0.4.x` section; after its gate it is tagged `v0.4.x` with a GitHub
  release (INSTALL.md A.10). `ops/publish-version.sh` records the commit's `package_version` in `version.json`.

- **The control page in a terminal (`eca-rsi top`, `ops/rtop`).** Periscope's `/_control/` records in a colour
  terminal, refreshed every 2 s (`--interval`, `--once`; `q` quits, `r` refreshes): workers with CPU and memory
  meters, a ten-minute CPU sparkline, slots, tasks and time left, then the tasks they run; models; the pool's
  running operations, waiting requests and infeasible reasons; the datasets at work and what each computes; node
  productivity; the failed attempts of the window, repeats of one dataset and operation folded into one line. Side by
  side from 150 columns, stacked below. It renders `control.snapshot()` like the page (read-only, no connection,
  tests/test_monitor_isolation.py), and a record older than 3 minutes shows as silent. The page's status totals and
  recent failures moved into `control.status()`, which both use. `rich` becomes a declared dependency (already in
  the science image).

- **Organize planning reads before it decides (#54 follow-up).** The planning prompt now opens with a procedure (`prompts/organize_procedure.md`): read every source profile with `inspect_source(column=null)`, ask only for columns listed in `obs_columns`, rest every decision on values read and name them in the rationale, submit and correct. `inspect_source` answers a column that does not exist with the source's real column list, and reads the text "null" as null. Found on the glm-5.3-flash gate (2026-10-07), which guessed 27 column names and never submitted a plan; `plan.md` no longer claims the profiles are in the message.
- **More model keys and endpoints (#54).** A catalog entry may name the variable holding its key (`key_env`) with its
  own `url`, so a second key or another endpoint of a provider (Ark Agent Plan `/api/plan/v3`) sits beside the
  first; such entries are exempt from one-URL-per-backend. Keys may live in `~/.config/ecarsi/keys.env` (mode 600,
  `ECA_KEYS_FILE`), read by runners and pool turns before `~/.bashrc`. A dataset or gate chooses another calling
  order by its `bridge_root`: `ops/new-bridge.sh` makes a second bridge with its own catalog, `ops/gate.py start
  --bridge=` runs a gate on it, `BRIDGE=` on the command line of `ops/control-plane.sh` starts its dispatcher and
  runners; the launcher matches a bridge's processes by its exact root and logs them apart.
- **Hard agent steps are frozen before their evidence is pruned (decision 0021, #51, #14 step 1).** The run's
  `published` display sync, which the dataset workflow awaits before it completes, copies every hard session
  (restarted, context reset, or at least 2 rejected submissions) with the files its records reach within three
  references into `<archive_root>/_cases/<collection>/<dataset>/<run>/`, each file once per run and checked
  against its sha256; 24 GiB per case and 96 GiB per run, skipped cases listed with the reason. No shared
  component changes.
- **Every model turn records its size, latency and failure class (#52, #31 step 1).** The turn's `result.json` gains `provider_calls` (per HTTP request: bytes, inlined images and their bytes, latency, status, tokens, error code) and their totals `input_tokens`, `output_tokens`, `images`, `image_bytes`, `latency_s`, plus `error_class` (timeout, rate_limit, context_too_long, output_limit, parse_error, other). Written by the version's runner (or pool task); the shared bridge is unchanged. `ops/turn-report.py <run root>` groups a run's attempts by class and size, and classifies older attempts from their error text. `harness_bridge` exports its context-limit messages as `CONTEXT_LIMIT_MESSAGES` for both.
- **needs_review lists the samples the inclusion agent excluded (#46).** The gen2 release never produced the
  documented `sample_excluded` category: one item per excluded sample, with its cell count and the agent's reason,
  built from the `sample_excluded` rows of `cell_exclusions.csv.gz`. Found on Li2019_skin's first batch-1 release
  (5 samples, 37 cells).
- **One stress panel and one mitochondrial rule for every species (decision 0020, #29).** A package `genesets/`
  shared by osp and msp. `is_mito`: an MT- prefix or the bare names of rhesus, cynomolgus and mouse lemur (ND1, COX1,
  CYTB, ...), the rule of stangene.mito; OSP's `pct_counts_mt` and msp's `mito` mark use it, OSP's summary records
  `n_mito_genes`, and a sample with none is listed in needs_review. `is_stress`: 35 genes chosen from eight published
  signatures by their coherence with the core in 34 human and mouse atlases, matched by symbol or Ensembl ID in eight
  species; it replaces both OSP's 140-gene `DISSOCIATION_GENES_HS` (dissociation_score, fragment QC's dissociation
  test) and msp's 25-gene `STRESS_GENES_CORE` (stress_clusters.csv). `genesets/stress_panel.tsv` records every gene
  considered with its evidence and reason. OSP's `mt_prefix` argument is gone.
- **Worker nodes join by themselves (#44).** INSTALL.md A.7 documents `ops/worker-node.sh` as the way to submit
  workers on this deployment (Slurm on Sherlock, not a mechanism of the package), with a bigmem example, and what
  `switch-images.sh` does to such jobs.
- **Fragment QC needs an effect size (#27).** A test of msp's minor-sibling fragment QC hits only on p < 0.05 and
  AUC >= 0.7 (`MIN_AUC`, the rule decision 0017 uses for dying cells); `minor_sibling_qc.csv` gains `<test>_auc`. Each
  `fragment_qc` exclusion records the tests that hit, and needs_review lists fragment removals per round, stage and
  tests (`fragment_removed`).
- **Inspect cannot drop what the stress policy kept (#30).** A cross-sample quality drop of a cluster the type phase
  kept under the stress policy becomes a flag with a `host_adjustment` (`guard_retained_drops`), listed in
  needs_review.
- **Inclusion reads the UMAPs of the samples it excludes (#33).** Every sample inventory is still required; a cluster
  UMAP only for each excluded sample, so a unit of 196 samples fits one session.
- **Mitochondrial genes are their own axis (decision 0017, amended).** msp's stress-gene rule no longer counts MT-
  genes. `stress_clusters.csv` gains `n_mito_hits`, `mito_genes` and `mito`: a cluster under a quarter of its local
  siblings' pooled cells with more than 3 MT- genes among its top 10 against them is `mito`, `recommend_removal`,
  and supports a `dying` removal of 10 or more cells (`guard_stress`); a stress or dissociation removal still needs
  the stress mark. The prompts say so, and cross-sample's type checklist lists `dying`.
- **The gate checks the display zone's links (#41).** `gate.py check` renders the zone with Periscope's renderer,
  follows every link of its pages and reports, and fails on a target the zone lacks or one outside it
  (`display.broken_links`). The walk it shares with the display sync now reads a unit page's links from the unit: in
  a run of several units, files linked only from a unit's reports (tables, figures) never reached the display zone.
- **Periscope split by responsibility (#43).** `ecarsi/ui/serve.py` (2,055 lines) and `index.py` (1,576) become
  `serve` (HTTP handler, command line), `registry` (dataset list, display zones, scan-add and the other subcommands),
  `fleet` (dataset summaries, their cache, control-plane verdicts), `home` (navigator and overview), `index` (run and
  unit pages), `gen1` and `gen2` (each generation's unit state and page; `gen1` goes when the old runs do) and `common`;
  the CSS and JavaScript moved from Python strings to `ecarsi/ui/static/`. No module is above 600 lines. Code moved
  unchanged: the pages of four real runs (gen 1 and gen 2), the overview and the navigator render byte-identical before
  and after.
- **The scripts and zmip are inside the layer test (#42).** `tests/test_layers.py` checks that every repository name
  the scripts in `ops/` and `container/` import, or name in their shell lines, still exists, so moving a module they
  use fails a test (the pruner broke silently on the 0.4.3 move). zmip now reaches msp only through `msp.api` (10 more
  names there), and msp's aliases kept for 0.3 callers (`evidence.components`, `report.csv_table`, `report.img`) are gone.
- **Zoom-in low-confidence keeps no longer flood needs_review (#45).** A QC cluster's keeps at low confidence share
  one `inspect_flag` line with their intersections and cell counts, and intersections under 20 cells
  (`release.LOW_KEEP_MIN_CELLS`) are left out. On gate-20261006-0202 the 123 such lines (1-16 cells each) become none.
- **Periscope's background scripts open no tunnel.** `ops/start-periscope.sh` and `PERISCOPE_DOMAIN` no longer pass
  `--ngrok` (Sherlock forbids unattended tunnels), and `ops/restart-periscope.sh` no longer waits 60 s for an ngrok
  session to end. `ecarsi serve --ngrok [--domain D]` stays for attended sessions in the foreground.
- **A version can bring its own images (decision 0019, step 5).** `warm_pool configure-runtime` registers every runtime
  it selects and the one it replaces (`--register-only` adds one without making it current); a version's pool
  requests take the runtime of the compute image in its `version.json`; `add-worker --image` starts a worker of a
  registered image, which declares that image's runtime; the scheduler marks a request `infeasible` while no live
  worker declares its image runtime; a version's coordinators and runners run in its control image.
  `publish-version.sh` takes `VERSION_IMG` / `VERSION_SCIENCE_IMG`. INSTALL.md A.11.
- **Contracts on the files versions share with the shared components (decision 0019).** `pool-request/1` (a pool
  request, written by any version, read by the shared workers) and `turn-plan/1` (written by the shared bridge, read
  by the version that runs the turn); writers check before they write, the workers and turn executors after they
  read. `ops/publish-version.sh` refuses a version that would write a version of a shared file (`contracts.SHARED`)
  the shared components do not know.
- **The compute image's Python environment is locked (decision 0019, step 5).** `container/science-requirements.lock`
  pins the 118 distributions of `/opt/rsi-python` with their hashes; installed from it into the base image, they
  reproduce the image's environment file for file. INSTALL.md B.5 has the command.
- **Versions on the control page (decision 0019).** fleet-status records each workflow's task queue, and
  Periscope's `/_control/` names the version each running dataset runs on. `ops/control-plane.sh` keeps the two
  settings apart: `VERSION` (or the current version) for coordinators and runners, `INFRA` for the shared components.
- **No more workflow patch markers (decision 0019).** A version replays only the histories of its own task queue,
  so the `workflow.deprecate_patch` lines are gone and the replay check is no longer a deployment step. CLAUDE.md,
  INSTALL.md (new A.10) and the decisions describe shipping a change as a version.
- **Retiring a version (decision 0019).** `ops/retire-version.sh <name>` stops a version's coordinators and runners once
  no execution runs on its queue (refused for the current version) and lists its datasets that ended unfinished;
  `ops/count-wf.py` counts running executions per task queue.
- **The kernels lose their standalone flows (#28).** osp, msp and zmip keep what their `api` modules reach: their
  command lines, agent flows (osp `propose_annotation`, msp `inspect_clusters` / `annotate_clusters`, zmip
  `annotate_lineage` / `plan_lineages` and the lineage runner), msp's dask endpoints, agent checkpoints and
  `run_multi_sample_pipeline`, zmip's `runtime` identity and `msp_compat` re-exports are deleted (about 4,800 lines and
  2,700 lines of tests), and so is the generation-1 `eval/` replay (its notes are in `docs/history/eval/`). No kernel
  imports `harness_bridge` (test_layers). One list of removal reasons, `msp.annotate.REMOVE_REASONS`, now including
  `dissociation` and `dying`: cross-sample accepts them as zoom-in did, and decision 0017's guard checks them in both;
  one batch guard, msp's, for cross-sample and zoom-in.
- **The first version is current (decision 0019, step 3).** Version `ded8588df51d` passed the gate beside
  production (all 23 workflows on its queue) and is current; its 4 coordinators and its runners serve every new
  dataset, and the image's coordinators on `ecarsi-durable-v2` are stopped. `ops/control-plane.sh
  start|stop|restart|status` without `VERSION` now runs the current version's coordinators and runners beside the
  image's shared components, so a plane restart no longer brings back coordinators on the old queue.
- **Code versions side by side, step 1 (decision 0019).** The machinery is on main and inert until a version is
  published: `ecarsi.version()` / `ecarsi.task_queue()` (a queue per version, read in the coordinator's `main`
  because Temporal's sandbox forbids file access while it imports workflow modules; a new test loads every workflow
  in the sandbox from a checkout and from a version); pool requests of a version carry its directory first on the
  Python path and a `placement` (the image runtime's digest) that HQ and the node-local numba cache key on;
  bridge requests record the version, whose turns keep their own adapter and go to that version's runners
  (`<version>.<model key>`) or to a pool task on its code; `container/control-plane.sh` takes `VERSION` (coordinators
  and runners of one version, matched by queue so versions never stop each other's) and `INFRA`;
  `ops/publish-version.sh`, `ops/start-version.sh`, `ops/set-current.sh`; `ops/run.sh` and `gate.py` take `VERSION`;
  the resume helpers resume on each run's own queue.
- **One repository (decision 0018), 0.4.5.** osp, msp, zmip, standissect-lite and agent-harness-bridge now live at
  the top of this repository with their histories (`git log msp/` reaches msp's first commit); their tests are in
  `tests/<package>/` and run with the suite, their READMEs and changelogs in `docs/kernels/<package>/`. Import names
  are unchanged. The images take the packages from the `git archive` snapshot and lose their old wheels
  (`ops/build-images-update.sh` checks each comes from `/opt/eca-rsi`); `ops/run.sh` drops `DEV_PATHS` and
  `ECA_SIBLINGS`. `tests/test_layers.py` scans the merged packages: none imports `ecarsi` (msp's dead
  `ecarsi.pool` endpoint is gone), osp/msp/zmip may import only the packages listed in `MAY_IMPORT`. One version for
  everything; the merged packages keep their last release number as a constant. The former repositories are archived.
- **Stress policy (decision 0017, issue #9).** Spec key `stress_policy`: `remove` (default) or `keep`. Under remove,
  an agent's removal of 10 or more cells as stress or dissociation stands only when msp's `stress_clusters.csv` marks
  its cluster, as dying only on a clearly higher mitochondrial fraction or fewer genes than the same identity
  (Mann-Whitney AUC ≥ 0.7, p < 0.05); the code keeps the others, as it keeps batch-only removals, and records why in
  the decision's `host_adjustment`. Under keep they always stay, and so do fragments removed only by the dissociation
  or mitochondrial test and OSP dissociation-stress drops. Retained cells keep their labels and carry obs
  `retained_state` into `final.h5ad`; `cell_ledger.csv.gz` adds `retained_state`/`retained_stage`; needs_review adds
  `stress_retained`. The zoom-in prompt no longer says "default to remove". Cross-sample type removals now record
  their confidence, so needs_review `removed` sees medium and low ones. A paused run resumed after the deploy applies
  the rules from its next stage on.
- **Release review links resolve (#26).** The needs_review rows `removed` and `policy_excluded` linked
  `cell_exclusions.csv.gz` beside the unit instead of `release/cell_exclusions.csv.gz`, and `agent_skipped` linked an
  absolute path; both are unit-relative now. Found by rendering a finished gate's display zone and following every link.
- **Remote `add-worker` carries the library path.** It ran the host Python over `ssh <node> '<python> …'`, whose shell
  loads no modules since `~/.bashrc` loads `~/pp` in interactive shells only (2026-10-05); the module-built Python then
  could not find `libpython3.12.so` and the image switch left the pool with no worker. The remote command now gets this
  side's `LD_LIBRARY_PATH`, and `switch-images.sh` prints a failed add-worker's error instead of a blank line.
- **No workflow patch branches left.** All 21 `workflow.patched` branches of the stage workflows lost their old path;
  each patch id keeps one `workflow.deprecate_patch` line so the histories since 2026-10-04 still replay (139 of 141;
  the scale test's cross-sample and zoom-in predate DEG batching by cells). The dead organize `submit_plan` activity is
  gone and organize's tools are sized by `stages.resources.size`. Deploy only at 0 running executions.
- **Turn results are contract `turn/1`** (outcome one of seven, a worker record), checked by both writers and the reader.
- **Workflow tests run on Temporal's time-skipping test server** (`tests/temporal_env.py`): the real workflow classes,
  fake activities and child workflows registered under their real names, real updates, queries, timers and
  continue-as-new. No test patches `ecarsi.control` internals or `temporalio.workflow` any more.
- **The pool names no stage operation.** `warm_pool/budget.py` moved to `stages/resources.py`: the measured ceilings,
  CPU caps and `from_cells` / `from_artifact` / `from_compute` / `from_deg_buffers`. Stage code sizes each request
  where it is built (`size` in the three stage workflows, organize prepare and `stages.evidence.plan` for every
  tool call); `warm_pool.state.submit` applies only the operator's online `ceilings` (`operator_ceiling`).
  `tests/test_layers.py` fails when a warm_pool module names an operation of the table.
- **Stage programs and stage workflows share code through `common` modules only.** `stages/common.py` holds sealed
  bundles, artifacts, `png_url` and the DEG comparisons (from `stages/persample.py` and `stages/crosssample.py`);
  `control/common.py` holds `call`, `await_pool`, `handoff`, `stage_with_waits`, `HISTORY_LIMIT`,
  `SKIPPED_CELL_LIMIT` and the DEG fan-out (from `control/persample.py`). Each program pins its own file, `common.py`
  and `contract.py`, so a change to `persample.py` no longer invalidates queued zoom-in requests.
  `tests/test_layers.py` fails when one stage program imports another.
- **The per-sample unit rule lives in ECA-PP only** (eca-pp 0.5.4 `sample_unit`; decision 0016 amended):
  organize maps ECA-PP's verdict (library, batch, whole, stop) to columns; `LIBRARY_MAX_CELLS` and the platform list
  left eca-rsi. The identify-columns result is contract `eca-pp-identify-columns/1`. A pre-0.5.4 result without a
  batch or library stops organize until identify-columns is re-run.
- **MSP 0.5.4**: DEG leaves out the genes no cell of a comparison expresses (a gene expressed in one group only
  is kept); pvals_adj is still corrected over all genes, so the DEG tables do not change. In the scale test no cell
  expressed 20 % of the genes and a single cluster none of 49 % (median).
- **DEG batches follow the cell count** (`control/common.py` `deg_batches`, workflow patch `deg-batch-cells-v1`):
  eight comparisons per request up to 50,000 cells, fewer above, one from 400,000, and `max_in_flight_deg` grows by
  the same factor. In the scale test (418k cells) one request of eight ran 75 min and timed out once while most of a
  64-core node sat idle. Each comparison is computed alone either way, so results do not change. Cross-sample and
  zoom-in share the fan-out loop (`run_degs`). `ops/replay-check.py` takes `--status` and `--where` to replay
  closed histories.
- Cross-sample and zoom-in compute ask 0.12 MiB per cell (was an estimate of 0.15): the scale test peaked at
  0.069 (418k cells, 30.3 GiB) and 0.084 (a 181k-cell lineage).
- Figures above 512 KiB reach the model as a 256-colour palette PNG at full size (`stages.persample.png_url`, used by
  per-sample, cross-sample and zoom-in reads). The 418k-cell `umap__ann_coarse.png` fell from 1.66 MB to 533 KiB; in
  the scale test, turns carrying it failed at the provider after 10 minutes and one cross-sample session died.
- Per-sample `read_evidence` reads figures in fixed pages (offset 0, then each returned `next_offset`). Any other
  offset is an error. In the 2026-10-04 scale test one session read offsets 1–9 after a complete first page, put
  55 images (7.5 MB) in its context, and its next model call timed out three times.

## 0.4.4 — 2026-10-03

Samples and batches follow ECA-PP; large samples no longer need hand-made splits
([decision 0016](docs/decisions/0016-samples-and-batches-from-eca-pp.md)).

- **Organize takes each source's samples from ECA-PP identify-columns** (eca-pp 0.5.3: batch ladder, platform, library):
  the library, else the batch, else the whole source; Harmony only when ECA-PP recommends it. Precedence: the
  spec's `sample_map`, then ECA-PP, then the planning agent (sources without an identify-columns result). A large
  source without batch or library stops at organize unless the platform is split-pool or plate.
- **Chunks**: a sample above 20,000 cells (`sample_map.chunk_cells`) runs per-sample QC as `<sample>.chunkNN`
  chunks that keep the sample as their batch; a chunked unit skips the inclusion agent.
- **Budgets from size**: per-sample, cross-sample and zoom-in compute ask for memory by cell count
  (`budget.from_cells`, raise only); partition and organize execute by file size.
- Periscope's Samples header says where the samples and the batch came from.
- `docs/history/HARD_DATASETS_20261003.md` corrected: correcting PanSci across age and sex is what the atlas wants.

## 0.4.3 — 2026-10-02

Every module now sits in one part of the system.

- **Stage helpers moved into `ecarsi/stages/`**: `organize_execute` (was `execute`), `upstream` (with organize's
  `profile_unit`), `h5ad` (`read_obs`, `open_counts`; was `design` and `downstream`), `inclusion` (was the top-level
  `crosssample`), `osp_worker`, `osp_contract`, `ledger`, `release_state`, `archive`. Pinned program lists follow.
- **`ecarsi/files.py`**: the durable-record helpers (`read`, `save`, `lock`, `immutable`, `reference`, `verified`,
  `digest`) every part used from `warm_pool/state.py`; the pool keeps only requests and receipts.
- **Shared modules import no part** (`tests/test_layers.py`; `display` and `observatory` are the listed exceptions);
  `control/dataset.py` takes run-directory names from `layout`. Organize no longer writes static Periscope pages.
- **Removed**: the `ecarsi.index`, `ecarsi.umapdata` and `ecarsi.harness` shims, `ecarsi.cost` and the sample map's
  unfed agent fallback (`build_mapping` has no `identify` argument).
- **Release gate** `ops/gate.py`: after a switch, runs `~/.config/ecarsi/gate-dataset.json` end to end and checks
  release, degraded steps, lineage reports (#26), display zone and archive (#25).
- Decision 0015 proposes running two image versions side by side; not built.

## 0.4.2 — 2026-10-02

Make the system smaller to explain and harder to break silently.

- **Degraded steps leave a record** ([decision 0013](docs/decisions/0013-degraded-results-are-recorded.md)): a
  report, a copy of readable files, a display sync or a round ledger that fails without failing the run is kept
  under `<run>/degraded/`, listed first in `needs_review` (`degraded`) and marked on Periscope ("N degraded").
  The tests run with `ECARSI_STRICT=1`, where such a failure raises.
- **Boundaries are tested** ([decision 0014](docs/decisions/0014-boundaries-are-tested.md)): the stage programs
  reach osp / msp / zmip only through their new `api` modules (OSP 0.1.8, MSP 0.5.3, ZMIP 0.3.10), which alias
  the 27 private kernel functions eca-rsi used; `tests/test_layers.py` enforces that and which subsystem may
  import which. `ecarsi/contracts.py` names the fields of every shared JSON file; writers and readers check them.
- **Deployment scripts in the repository** (`ops/`, linked from `$BASE/ops`), reading every path from
  `deployment.env`. `build-images-update.sh` replaces a wheel's distribution wherever the image has it (the
  kernels live in the science image's `/opt/rsi-python`). The unused `observatory` component of
  `control-plane.sh` and the `ecarsi/serve.py` shim are gone.

## 0.4.1 — 2026-10-02

- **Explicit sample maps on the control plane**: the dataset spec's `organize.sample_map` (`sources`, `merges`,
  `exclude_cells`, `batch_key`) reaches organize, pinned by content with its request. `sources` override the
  planning agent's experiment column; excluded cells are ledger rows with their reason and `policy_excluded`
  review items. `"batch_key": false` declares a unit one batch: `eca_batch = single_batch` on every cell, so MSP
  and ZMIP skip Harmony and the per-batch HVG vote. A named `batch_key` is checked as before (constant per
  experiment, two or more values).
- **Rejected ECA-PP sources are skipped**, as the local path did, instead of failing organize: the source
  inventory keeps them and every unit's `needs_review` lists them (`upstream_review`).
- Rounds after the first record each cell's experiment (`eca_sample_id`), not its batch value, as the sample
  of its input and exclusion rows.
- Tests run the real MSP integration for a named batch column (Harmony runs) and for one batch (skipped).

## 0.4.0 — 2026-10-02

The control plane is the only way ECA-RSI runs; the local path is gone.

- **Removed the local path**: the CLI commands `run`, `organize`, `persample`, `crosssample`, `zoomin`, `loop`,
  `ledger`, `prune` (`eca-rsi` keeps `serve`, `index`, `umapdata`), `run-eca-rsi.sh`, the modules `loop`, `zoomin`,
  `persample`, `osp_dispatch`, `prune`, `service`, `mirror` (`--mirror`), `agent_retry`, `osp_stage`, and the parts
  of `crosssample`, `downstream`, `ledger`, `design`, `cost`, `policies`, `organize`, `osp_contract`, `osp_worker`,
  `resources`, `run_state` and `layout` only it reached: about 3,900 lines of package code and 2,000 of tests, found by a module-qualified
  reachability walk from the code the control plane runs. With it went the runtime-identity digest
  (`runtime_identity`, `ECA_RSI_DEVELOPER_MODE`) and the environment variables only it read (`AGENT_WALL_MIN`,
  `PERSAMPLE_PARALLEL`, `ZMIP_PARALLEL`, `MSP_BATCH_COL`, `MSP_PYTHON`/`ZMIP_PYTHON`/`OSP_PYTHON`, `ECA_RSI_PAUSE_FILE`, ...).
- Pages still read the local path's layout (generation 1): its runs are served from their display zones.
- Found on the way, not changed: on the control plane the organize plan sets only each source's experiment column,
  so explicit sample maps (`merges`, `exclude_cells`, `batch_key`) have no input; and the study-design context for
  the agents (`ecarsi.design`) was only ever passed by the local path.

## Unreleased — 2026-09-28

The warm-pool scheduler after a week of gen-2 batches (2026-09-24 to 09-28): the two-day maintenance sprint of
09-27/28 replaced the scheduler's own queueing with HyperQueue's, cut the DEG request count eightfold, and ended
with an end-to-end run on real data. Version is still 0.3.2; this entry collects what is on `main` since then.

- **HyperQueue priorities are back, on a patched HQ.** The gap-computation panic that forced priorities off on
  09-19 is an upstream bug (It4innovations/hyperqueue#1135: `WorkerResources::remove*` indexes a worker's resource
  vector with global resource ids). Our fix is PR #1137; the maintainer's #1136 rewrites the same area and also
  covers it. The pool runs a local build of upstream `main` + #1136 + #1137 (`release.hq_priority` switches
  priorities off if that ever has to be undone); `check_hq` accepts 0.26.2 or any later build (ef512b2).
- **Priority = class first, then width** (`hq_priority`: model turn 1000, session tool 800, batch work 0, plus ten
  per CPU). Decided by replaying a journal day under alternative rules: `python -m ecarsi.warm_pool.replay
  --root <pool> --day <day>` (1f5567b, 87c1ce4) rebuilds a day from the workers' task journals with causal lags and
  runs it through a policy. On 2026-09-24 (352k tasks, 13 workers) every variant finished datasets in the same
  time (span ratio 1.01–1.02); only interactive waits differed, and class-first priority cut the p90 agent/tool
  wait from 95 s to 2 s.
- **The scheduler-side hold, backlog cap and drain are gone** (54f1f6b). Every request goes to HQ the tick it is
  seen; HQ orders by priority and, with #1136, reserves cores for wide tasks itself. `release` keeps
  `model_call_resource`, `hq_priority`, the GPU pinning knobs and `pin_wait_seconds` (was `drain_age_seconds`).
- **A request no live worker can hold stays out of HQ** (16412ad): `infeasible: <reason>` in its observation,
  counted per reason under `release.infeasible` in `scheduler.json`, re-checked every tick. The waiting workflow
  still sees `queued`.
- **Release timings come from the journals** (38c550a): `warm_pool measure` writes `pool/measured.json` (per
  operation median/p90 run on cores and cards, p90 wait); the scheduler refreshes it every 30 min and uses it for
  GPU pinning and unpinning unless a knob is set in `config.json`.
- **DEG comparisons run eight per pool request** (0b2d5b2, 622fc5a): `stages.crosssample.deg_batch` verifies and
  maps the shared buffers once, writes each comparison's `deg-<i>/result.json` and a `results.json` manifest;
  `assemble` takes manifests and single results alike. Both workflows gate it with `deg-batch-v1`; `DEG_BATCH_SIZE`
  lives in `control.persample`. The request's timeout is twice the per-comparison budget, not budget × batch: HQ
  matches `time_request` against a worker's remaining allocation, so an hours-long request never lands on a short node.
- Memory ceilings are the values the pool ran on after the PanSci organs (ad82f9c); resume supersedes every
  generation of a restarted session and accepts a publication whose Pool folder was pruned (462f069); the bridge
  summary counts model turns per dataset for Periscope (91e17fc).
- **Verified on real data, 2026-09-27/28:** 11_Shietal (9,163 cells) on two 16-core `normal` nodes plus the plane
  node: cross-sample 47 comparisons → 6 requests (22–44 s, peak 590–670 MB), zoom-in 42 → 6, four rounds, 93
  requests all succeeded first time, `hq_waiting` 0 throughout. Stopped by the owner at round 4 and pruned.
- Operations: Sherlock rejects sleeper jobs, so a worker node is now requested with a job that *is* the worker
  (`ecarsi.warm_pool slurm-worker` over the whole grant; it joins the pool by itself and leaves with the job).
  Deploy guards read the pool's own `hq` binary from `config.json` and exit with `os._exit` (the Temporal client
  can segfault at interpreter teardown and abort a `set -e` deploy after printing its verdict).

## 0.3.2 — 2026-09-19

Remove the first generation's Dask warm pool. Generation 1 runs are no longer resumed — their reports and metadata
stay readable in Periscope, and anything unfinished is recomputed by generation 2 — so the pool that used to feed
them has no remaining user.

- Delete `ecarsi.pool` (client, scheduler, executor, observe, status, CLI), `ecarsi.pool_web`, `ecarsi.compute_policy`,
  `container/dask-pool.sh` and their tests, plus the `pool` extra. Periscope loses the **Warm pool** sidebar button,
  the `/_pool/*` routes and `--pool-scheduler`; it no longer probes a dead scheduler every five seconds.
- Keep the two shared helpers generation 2 still calls: `pool/slurm.py` → `warm_pool/slurm.py` (inventory,
  process fencing; the Dask worker supervisor in it is gone) and `pool/budget.py` → `warm_pool/reservation.py`
  (the Slurm-allocation memory ledger).
- `OSP_COMPUTE_ENDPOINT` accepts only `local`; the `pool` / `auto` offload went with the pool. msp's own
  `MSP_COMPUTE_ENDPOINT=dask*` endpoints are untouched — they are an optional extra generation 2 pins to `local`.
- Nothing in the package imports dask any more.

## 0.3.1 — 2026-09-17 (branch `gen2`)

The second generation (Temporal control plane, HyperQueue warm pool, durable model-turn bridge, stage programs),
developed as feature/warmpool-v2 since 2026-09-14, integrated on one branch and given a package structure. The first
generation (`eca-rsi run`, Slurm pool, batch admission) keeps its modules where 0.3.0 left them.

- Move the flat gen-2 modules into `ecarsi.control` (work_coordinator as `control.coordinator`, temporal_service, *_workflow), `ecarsi.agent`
  (agent_bridge, agent_session, agent_dispatch, …; named `agent` so that `bridge` means only the external
  agent-harness-bridge package), `ecarsi.stages` (organize/persample/crosssample/zoomin `_v2`, dataset_release),
  `ecarsi.warm_pool.budget` (operation_budget) and `ecarsi.observatory` (dev_observatory); `python -m
  ecarsi.control|agent|observatory` entry points.
- The observatory's web server is gone: Periscope (`ecarsi serve --control-plane <run dir>`) mounts the same page and
  APIs at `/_control/` (`observatory.ControlPlane`; sidebar item "Control plane"); `container/control-plane.sh`
  starts Periscope for the run directory. `ecarsi.observatory` keeps `status` / `releases` / `tokens` / `temporal-ui`.
- A dead agent session restarts once (`control.coordinator.run_agent`: new session id `-r2`, directory `restart/`,
  same evidence; `restart.json` names the superseded session and resume treats that session's requests as
  superseded, as it now does for context resets). A second death skips the sample (finalized unannotated, prior
  label `unannotated`, `skipped_samples` in the per-sample publication, needs_review kind `agent_skipped`) or the
  lineage (kept with its cross-sample labels, `skipped_lineages` in the zoom-in publication, plan reason); a
  cross-sample session only restarts. Skipped cells above 10 % of a stage's input (`SKIPPED_CELL_LIMIT`) fail it.
- Round cap default 10 → 15 (`round_policy.DEFAULT_CAP`, `--cap`); gen-2 dataset specs state `round_policy.cap` explicitly.
- Pool failures of the budget class get one automatic retry from `control.coordinator.check_pool_once`: an execution
  deadline at twice the time limit, a preferred GPU's memory budget on CPUs (`warm_pool.state.retry` grew
  `timeout_seconds` / `without_gpu`, also on `warm_pool retry`). On 2026-09-18 a node whose Lustre client stalled
  turned seven 5-second prepare steps into 180 s deadlines, and a 4 GiB GPU budget failed three 50k-cell
  integrations; each failure ended its dataset.
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

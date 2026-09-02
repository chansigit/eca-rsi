# eca-rsi

> **Main line (2026-09):** the `ecarsi` package — organize → persample (osp) → self-driving rounds of
> crosssample (msp) + zoomin (zmip) → release, with landing pages and `ecarsi.serve`. Install: [INSTALL.md](INSTALL.md);
> architecture: [diagrams/architecture.html](diagrams/architecture.html); operating notes: [CLAUDE.md](CLAUDE.md).
> The `run.sh` six-step prompt loop described below is the previous generation (branch `primitive`).

**An autonomous, iterative curation loop for single-cell RNA-seq data.**
Point it at a folder of `.h5ad` files; it cleans the cells and annotates the
cell types over multiple rounds, then releases an annotated dataset — with
every decision, every script, and every removed cell on the record.

```bash
./run.sh <h5ad-folder> <workdir> [max_rounds]
./run.sh <h5ad-folder> <workdir> [max_rounds] --force-reopen   # continue past a prior release
```

No supervision required. The loop never pauses to ask a human anything;
whatever it cannot resolve becomes an explicit flag in the final report's
*needs review* section instead of a blocker.

## Why a loop — the Peeling-the-Onion principle

In single-cell QC the embedding space is not an immutable coordinate system
but a population-dependent manifold that rescales as noisy cells are pruned:

- **Relativity of variance.** Highly variable genes are selected by relative
  variance within the *current* cell pool. While damaged cells and doublets
  are present, their technical noise monopolizes HVG selection and the top
  principal components, hiding subtler signals and subtler artifacts.
- **Variance reallocation under rank-limited embeddings.** A low-dimensional
  embedding keeps only the top-ranked axes of variance. Removing the first
  layer of salient anomalies frees the share of the variance budget they
  occupied: the eigenstructure is recomputed, formerly subordinate directions
  rise into the retained components, and secondary anomalies that were
  collapsed onto the periphery of big clusters become separable for the
  first time.

Cleaning is therefore *inherently iterative* — exclude, re-embed, expose the
next layer, exclude again — not because one pass was executed badly, but as a
mathematical consequence of high-dimensional geometry. Each round of this
pipeline re-derives HVGs, integration, clustering and evidence from scratch
on the surviving cells, and the loop runs until convergence criteria are met
(near-zero removals, stable labels, no actionable open items).

## How it works

Each round runs six steps. **Every step is a fresh headless agent session**
(`claude -p`) with full tools, working in the dataset's workspace: it reads
the state from disk, writes and runs its own analysis code, and ends by
writing a report. The report file is the only completion contract the runner
checks — everything else (what to compute, how to judge, what to write) is
the agent's call, guided by a short per-step brief in [`steps/`](steps/) and
by the governance documents.

| step | job |
|---|---|
| `explore` | probe the current state, plan the round. Round 1 surveys the input folder from scratch: what each file is, whether files share the same physical cells (merging those would double-count), which columns are batch vs biology |
| `compute` | rebuild the feature space on the current cells: HVG → PCA → integration → clustering → UMAP, plus marker tables and QC evidence. Never reuses a previous round's embedding |
| `annotate` | cell type labels with evidence: cross-sample stability over specificity over effect size over p-value; a continuum is the null hypothesis; Cell Ontology-aligned names at two granularities |
| `qc` | quality verdicts: cells are presumed biological until proven noise; every removal must name a technical cause and defeat the plausible biological alternatives; when in doubt, flag |
| `apply` | execute every decision on the checkpoint — labels written, flagged cells marked, removed cells actually dropped and logged per-barcode. A decision recorded but not executed is treated as the worst failure mode there is |
| `stop` | continue or release. Release requires near-zero removals this round, labels stable against the previous round, and no actionable open items — then it exports the annotated `.h5ad`, per-cell table, UMAP figures, and a summary with a *needs review* section |

Crash-safe by construction: re-running the same command resumes, skipping
finished steps; the working checkpoint is only ever replaced by atomic
rename.

## Governing principles

The loop is bound by a written constitution and rules layer (the `docs`
symlink points to them; they live in a companion repository). The load-bearing
articles:

- **Presumption of biological innocence.** An "anomalous" population is a
  legitimate biological entity until direct technical evidence proves
  otherwise. Low depth alone is never grounds for deletion.
- **Asymmetry of risk.** False deletions corrupt the feature space and are
  costly to recover from; residual noise is cheap to filter later. When in
  doubt, flag — don't drop.
- **Rebuttal standard for deletion.** A removal is justified not by counting
  concurring evidence channels but by defeating the biological null: the
  stated technical cause must explain the data at least as well as every
  plausible biological alternative. If any biological account survives,
  deletion is barred.
- **Granularity alignment.** A few noisy cells inside a coherent cluster do
  not condemn the cluster; whole-cluster removal requires a pervasive flaw.
- **Full audit trail.** Retained/evicted counts, causes, per-barcode removal
  logs, and every script the agents ran — a third party can reproduce every
  decision.
- **Stop-loss safeguards.** Per-round and cumulative removal budgets; past
  the budget the loop turns conservative (flags instead of removals) rather
  than stopping.

## Design stance

An earlier version of this project fixed the computation in ~1,500 lines of
pipeline code with schemas and linters around the agents' decisions. Its
failure mode was instructive: every silent bug lived in the seam between
specification and implementation — decisions that were recorded but executed
by nothing, rules that were written but implemented nowhere. This version
inverts the design: **the capability lives in the agents (which write and run
their own code, kept as the audit trail), and the only fixed machinery is the
loop skeleton and the briefs** — under 400 lines in total. The hard-won
lessons survive as blunt sentences in the briefs rather than as code.

## Upstream: eca-pp (optional)

eca-rsi pairs naturally with [eca-pp](https://github.com/chansigit/eca-pp),
which standardizes single `.h5ad` files of unknown provenance (recovers raw
counts, resolves species, harmonizes gene names, computes authoritative QC
columns) and identifies the batch and cell-type columns with
integration-trial evidence. When its outputs (`standardized.h5ad` +
`result.json`, optionally `batch.tsv`) are present near the input files, the
explore step reads them and skips re-deriving what upstream already settled —
species, counts location, batch key, prior labels — and spends its probing on
the one question eca-pp structurally cannot answer: how multiple files relate
to each other. The two are decoupled: eca-rsi runs fine on raw folders with
no provenance at all.

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI (`claude`), authenticated
- Python with `scanpy`, `anndata`, `harmonypy`, `scrublet`
  (path configurable via `PY=...`)
- Model defaults to `claude-sonnet-5`; override globally with `MODEL=<id>` or per step with `MODEL_<STEP>=<id>` (e.g. `MODEL_ANNOTATE=claude-opus-5`)

## Caveats

- Agents run with `--dangerously-skip-permissions` inside the workspace —
  run it on machines and data you trust.
- Token cost is real: a small dataset (~1,800 cells) converged in 3 rounds /
  ~100 minutes of wall time, with six full agent sessions per round.

# standissect-lite

> **Find the minor fragments hiding inside your existing single-cell clusters —
> and nothing else.**

## Why (出发点)

Single-cell annotation and quality control both lean on clustering — yet
clustering followed by subset-and-recluster often fails to cleanly separate
tiny subpopulations and low-quality subgroups from their parent cluster.

UMAP is rightly criticised against over-interpretation; still, a locally
compact, well-connected group of points on the UMAP carries a genuine
data-level clue. This package uses that signal to sharpen the detection of
tiny clusters hiding inside existing ones: it clusters the UMAP coordinates
(at a granularity matched to the RNA-side clustering) and crosses the two
partitions, so the islands a human would otherwise lasso by hand fall out as
named, ranked fragments.

Two boundaries are deliberate:

- **Detection, not interpretation.** Whether a detected tiny cluster is
  biologically meaningful (or a doublet pocket / low-quality tail) still
  requires downstream verification. This is a data-driven method whose only
  job is to replace inefficient manual lassoing with something reproducible.
- **Light by construction.** The full
  [standissect](https://github.com/chansigit/standissect) adds that
  interpretation layer (DEG, QC drift, LLM diagnosis, reports, review
  server). `standissect-lite` is the detection step alone, extracted so other
  projects can reuse it without the heavy stack: no scanpy, no DEG, no LLM,
  no server. One module, six light dependencies, pure functions.

## What it does

It crosses two partitions of the same cells:

1. your **precomputed RNA-side Leiden clustering** — read as-is from
   `adata.obs[cluster_col]`, never recomputed;
2. a **UMAP-side clustering** computed here — kNN graph on the 2-D UMAP
   coordinates + Leiden on that graph.

Their overlap table (the "cartesian product" of the two labelings) splits each
RNA cluster into fragments, ranked by size:

```
RNA cluster "3"          × UMAP clustering            fragment name
───────────────          ───────────────────          ─────────────
                     ┌── u5 (8,021 cells, largest) →  c3_0  main core
  cluster 3 ─────────┼── u9 (  412 cells)          →  c3_1  minor
  (8,800 cells)      └── u2 (  367 cells)          →  c3_2  minor
```

Fragment names are byte-compatible with standissect's
(`c{cluster}_{rank}`, rank 0 = main core), so outputs interoperate.

## Usage

```python
import anndata as ad
from standissect_lite import dissect_partition

adata = ad.read_h5ad("data.h5ad")   # needs obs['leiden'] + obsm['X_umap']
res = dissect_partition(adata, cluster_col="leiden", umap_key="X_umap")

res.fragments[res.fragments.is_minor]   # the minors: parent, size, rank, ...
res.overlap                             # RNA × UMAP cell-count crosstab
res.labels                              # per-cell: umap_cluster / subcluster / rank
res.info                                # UMAP-side resolution search diagnostics
```

`dissect_partition` is a **pure function**: it never modifies `adata`.
Write-back is the caller's explicit one-liner:

```python
adata.obs["original_cluster_split"] = res.labels["subcluster"]
```

## Granularity matching (`umap_target_k`)

The split is only meaningful when the UMAP-side clustering has roughly the
**same granularity** as the RNA-side one: too fine chops clean cores into fake
minors, too coarse swallows real minors into the core.

By default (`umap_target_k=None`) the target UMAP-side cluster count is taken
**from the RNA side** — the number of distinct labels in `cluster_col` — and
the UMAP-side Leiden resolution is binary-searched (up to 12 iterations) until
the UMAP partition lands within `umap_target_k ± umap_target_tol` clusters.
Pass an int to override the target explicitly.

## Who owns which parameter

Every tuning knob steers the **UMAP-side** clustering and is prefixed
`umap_`; the RNA side contributes nothing but its labels (and, via the default
`umap_target_k`, its cluster count). Defaults are sane — rarely worth touching.

| parameter | side | meaning |
|---|---|---|
| `umap_n_neighbors=30` | UMAP | kNN graph size on the 2-D coordinates |
| `umap_resolution=0.5` | UMAP | Leiden resolution on that graph (search start when targeting) |
| `umap_target_k=None` | UMAP (target from RNA) | target cluster count; `None` → RNA cluster count |
| `umap_target_tol=2` | UMAP | allowed deviation from the target |
| `umap_random_state=0` | UMAP | Leiden seed |
| `min_subcluster_size=50` | neither | not a clustering knob — fragments with rank > 0 and ≥ this many cells get `is_minor=True` |

The lower-level `umap_leiden_partition(umap_xy, ...)` is also exported for
callers holding bare coordinates; its parameters carry no `umap_` prefix
because the function is UMAP-side by definition.

## Install

```bash
pip install -e /path/to/standissect-lite     # or add the parent to PYTHONPATH
```

Dependencies: `numpy`, `pandas`, `scikit-learn`, `python-igraph`, `leidenalg`,
`anndata`. No scanpy.

## Relationship to standissect

The core partition function is vendored from `standissect/cluster.py`
(canonical source) and kept semantically byte-compatible, so both packages
name fragments identically. When you need diagnosis (DEG / QC drift /
LLM `likely_cause` / reports / the review server), use the full package.

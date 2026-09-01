# standissect-lite

> **Find each cluster's minor siblings — the small stray fragments carrying its
> own label — and nothing else.**

## Why (出发点)

Single-cell annotation and quality control both lean on clustering — yet
clustering followed by subset-and-recluster often fails to cleanly separate
tiny subpopulations and low-quality subgroups from their parent cluster.

In practice analysts fall back on the UMAP: spot a small island sitting apart
from its cluster's main blob, and lasso it by hand. UMAP is rightly
criticised against over-interpretation; still, local separations in the
embedding can provide useful candidate structure, because UMAP is built to
preserve aspects of the high-dimensional neighbourhood graph. Analysts
already treat UMAP islands as hypotheses — this package turns that visual
heuristic into a systematic candidate-detection procedure: it clusters the
UMAP coordinates (granularity-matched to the RNA-side clustering), crosses
the two partitions, and hands the islands back as named, ranked fragments,
while explicitly leaving validation to downstream evidence.

Two boundaries are deliberate:

- **Candidates, not conclusions.** Whether a detected tiny cluster is
  biologically meaningful (or a doublet pocket / low-quality tail) is decided
  by downstream evidence, not by this method. Its only job is to replace
  inefficient manual lassoing with something reproducible.
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

Naming happens in two steps. **Step 1 — cartesian product**: each non-empty
(RNA cluster × UMAP cluster) combination is a fragment; think of it as the
concatenated name `c3u5` = cells in RNA cluster 3 ∩ UMAP cluster u5.
**Step 2 — rank by size**: within each RNA cluster, fragments are sorted by
cell count and renamed `c{cluster}_{rank}` — the largest becomes the main
core, the rest are its minor siblings:

```
  step 1: product names                    step 2: sort by size, rename
  ─────────────────────                    ────────────────────────────
                    ┌── u2 (  412 cells) → c3u2 ─┐        ┌ c3_0 = c3u5 (8,021)  main core
  cluster 3 ────────┼── u5 (8,021 cells) → c3u5 ─┼───────▶┤ c3_1 = c3u2 (  412)  minor sibling
  (8,800 cells)     └── u9 (  367 cells) → c3u9 ─┘        └ c3_2 = c3u9 (  367)  minor sibling
```

A **minor sibling** is a small fragment carrying the *same* RNA label as its
main core — "sibling" stresses that it belongs to the same parent cluster,
not that it is some other small cluster elsewhere on the map.

The `fragments` table keeps both identities per row (`subcluster` = the
ranked name — THE headline identifier, rank 0 = main core, strictly
descending by size within each parent; `_umap_partition` = the raw UMAP-side
partition this fragment came from). Ranked names are byte-compatible with
standissect's (`c{cluster}_{rank}`), so outputs interoperate.

**Only use `subcluster`/`rank` for anything ranking-related.**
`_umap_partition` (`u0`, `u1`, …) is ranked *globally* across the whole
embedding, not within any one RNA cluster — `u0` is not generally each
parent's largest fragment, and two different parents can share a
`_umap_partition` id. It's underscore-prefixed and kept only for
traceability (it's what the `overlap` crosstab's columns are); building a
combined per-cell label from it directly (e.g. `f"{parent}_{u}"`) does *not*
have the rank-0-is-largest property people expect from `c{parent}_{rank}`
naming — that's exactly what `subcluster` already gives you for free.

## Usage

```python
import anndata as ad
from standissect_lite import dissect_partition

adata = ad.read_h5ad("data.h5ad")   # needs obs['leiden'] + obsm['X_umap']
res = dissect_partition(adata, cluster_col="leiden", umap_key="X_umap")

# or hand a precomputed UMAP matrix directly (obsm not consulted;
# one row per cell in adata order, >=2 columns):
res = dissect_partition(adata, cluster_col="leiden", umap_Nx2_mat=my_umap)

res.fragments[res.fragments.is_minor_sibling]   # minor siblings: parent, size, rank, ...
res.overlap                             # RNA × UMAP cell-count crosstab
res.labels                              # per-cell: subcluster / rank / is_main (+ _umap_partition, raw)
res.info                                # UMAP-side resolution search diagnostics
```

`dissect_partition` is a **pure function**: it never modifies `adata`.
Write-back is the caller's explicit one-liner:

```python
adata.obs["original_cluster_split"] = res.labels["subcluster"]
```

The lower-level `umap_leiden_partition(umap_xy, ...)` is also exported for
callers holding bare coordinates. All tuning knobs are documented in the
`dissect_partition` docstring; defaults are sane and rarely worth touching.

## Install

```bash
pip install -e /path/to/standissect-lite     # or add the parent to PYTHONPATH
```

Dependencies: `numpy`, `pandas`, `scikit-learn`, `python-igraph`, `leidenalg`,
`anndata`. No scanpy.

# CPU and GPU computation

`osp.cluster.cluster_and_deg(adata, compute_backend="rapids", ...)` runs PCA,
neighbors and UMAP with RAPIDS on one visible NVIDIA GPU. The default is `cpu`.
Normalization, HVG selection, QC covariates, CPU Leiden, Wilcoxon DEG and output
contracts are shared. `adata.uns["osp_compute_backend"]` records the backend.
This is a Python API; the standalone CLI does not yet expose a backend option.

CPU use does not import or require RAPIDS. For GPU use, install a compatible
CuPy, RAPIDS and rapids-singlecell stack following the
[installation guide](https://rapids-singlecell.readthedocs.io/en/latest/installation.html).
The tested RSI image uses Python 3.12, CUDA 12.4, CuPy 13.6.0, RAPIDS 25.12,
rapids-singlecell-cu12 0.17.0 and Dask 2025.9.1, with NumPy 2.2.6, pandas 2.3.3
and Scanpy 1.12.4. Dask must be present even when only single-GPU operations are
used. No Dask scheduler is involved in this computation.

The caller or scheduler must reserve the GPU and set `CUDA_VISIBLE_DEVICES`.
OSP requires exactly one visible device; it does not allocate devices or decide
when to fall back to CPU. RSI Warm Pool owns those decisions and keeps a whole
sample's computation in one task. GPU mode can produce different clusters and
embeddings, so annotate its own output rather than reuse CPU cluster labels.

Run `python tests/check_rapids_backend.py` with a reserved GPU to check both
backends, including the three-cell minimum, finite embeddings, preserved raw
counts and cell identities. A separate full OSP trial on 318 real cells retained
the same 235 cells and recorded the same 83 removals with both backends; cluster
ARI was 0.730. This verifies contracts, not equivalence of clusters or a
production-scale performance claim.

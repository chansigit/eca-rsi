"""Optional hardware check: run with one reserved NVIDIA GPU and RAPIDS installed.

    python tests/check_rapids_backend.py
"""
import anndata as ad
import numpy as np
import pandas as pd

from osp.cluster import cluster_and_deg


for n_cells in (3, 80):
    rng = np.random.default_rng(7)
    counts = rng.poisson(3, (n_cells, 100)).astype(np.int32)
    counts[:n_cells // 2, :20] += 10
    data = ad.AnnData(counts.copy(), obs=pd.DataFrame(
        {"sample": "sample-a", "doublet_score": rng.random(n_cells)},
        index=[f"cell-{i}" for i in range(n_cells)]))
    data.layers["counts"] = counts.copy()
    for backend in ("cpu", "rapids"):
        result, deg, *_ = cluster_and_deg(
            data, compute_backend=backend, resolutions=(1.0,),
            primary_resolution=1.0, make_plots=False)
        assert result.obs_names.equals(data.obs_names)
        np.testing.assert_array_equal(result.layers["counts"], counts)
        assert result.uns["osp_compute_backend"] == backend
        assert result.obsm["X_pca"].dtype == np.float32
        assert np.isfinite(result.obsm["X_pca"]).all()
        assert result.obsm["X_umap"].shape == (n_cells, 2)
        assert np.isfinite(result.obsm["X_umap"]).all()
        assert result.obs["leiden_r1.0"].notna().all()
        assert {"names", "group", "pvals_adj"} <= set(deg)
        print(f"PASS: {backend}, {n_cells} cells", flush=True)

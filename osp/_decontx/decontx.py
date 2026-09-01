"""The :func:`decontx` main entry point and the :class:`DecontXResult`
result object.

Vendored from ``pydecontx`` (Apache-2.0; see ../../THIRD_PARTY_NOTICES.md),
itself a pure-Python port of the Bioconductor ``decontX`` function (Yang
et al., *Genome Biology* 2020). DecontX models each cell's observed UMI
counts as a two-component multinomial mixture of a *native* distribution
``phi`` (the cell population's own genes) and a *contamination*
distribution ``eta`` (a weighted blend of every other population), and
estimates -- by variational EM -- a per-cell contamination fraction and
a decontaminated count matrix.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import scipy.sparse as sp

from ._core import (
    calculate_native_matrix,
    decontx_em,
    decontx_initialize,
    decontx_loglik,
)

__all__ = ["decontx", "DecontXResult"]


@dataclass
class DecontXResult:
    """Result of a :func:`decontx` run.

    Attributes
    ----------
    contamination : (C,) ndarray
        Estimated contamination fraction for each cell (0-1).
    decontx_counts : scipy.sparse matrix
        Decontaminated gene-by-cell count matrix. Values may be
        non-integer; round for integer counts.
    z : (C,) ndarray
        The (possibly batch-prefixed) cluster label used for each cell.
    estimates : dict
        Per-batch estimated parameters (``phi``, ``eta``, ``theta``,
        ``delta``, ``contamination``, ``log_likelihood``, ``z``,
        ``iteration``).
    run_params : dict
        The arguments the function was called with.
    gene_names, cell_names : list or None
        Names carried through from the input.
    """

    contamination: np.ndarray
    decontx_counts: Any
    z: np.ndarray
    estimates: dict = field(default_factory=dict)
    run_params: dict = field(default_factory=dict)
    gene_names: list | None = None
    cell_names: list | None = None

    @property
    def log_likelihood(self) -> list:
        """Log-likelihood trace of the (first) batch."""
        first = next(iter(self.estimates.values()))
        return first["log_likelihood"]

    def decontaminated_counts(self, round: bool = True):
        """Return the decontaminated matrix, integer-rounded by default."""
        m = self.decontx_counts
        if round:
            m = m.copy()
            m.data = np.round(m.data)
        return m

    def to_dataframe(self) -> pd.DataFrame:
        """Per-cell summary table (contamination + cluster label)."""
        return pd.DataFrame(
            {"decontX_contamination": self.contamination,
             "decontX_clusters": self.z},
            index=self.cell_names,
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        n = len(self.contamination)
        mean = float(np.mean(self.contamination))
        return (f"DecontXResult(cells={n}, "
                f"mean_contamination={mean:.4f}, "
                f"batches={list(self.estimates)})")


def _process_cell_labels(z, n_cells: int) -> np.ndarray:
    """Validate and remap cluster labels to contiguous 1-based ints."""
    z = np.asarray(z)
    if len(z) != n_cells:
        raise ValueError("'z' must have one label per cell.")
    uniq = pd.unique(z)
    if len(uniq) < 2:
        raise ValueError("DecontX needs at least 2 clusters; only "
                         f"{len(uniq)} provided.")
    remap = {val: i + 1 for i, val in enumerate(uniq)}
    return np.array([remap[v] for v in z], dtype=int)


def _extract_counts(x, layer=None):
    """Return (counts G x C, gene_names, cell_names) from x.

    ``x`` may be an AnnData (cells x genes -> transposed), a pandas
    DataFrame (genes x cells), a numpy array or a scipy sparse matrix
    (genes x cells).
    """
    # AnnData (duck-typed to avoid a hard import).
    if hasattr(x, "obs") and hasattr(x, "var") and hasattr(x, "X"):
        mat = x.layers[layer] if layer is not None else x.X
        mat = mat.T  # AnnData is cells x genes -> genes x cells
        gene_names = list(x.var_names)
        cell_names = list(x.obs_names)
        return mat, gene_names, cell_names
    if isinstance(x, pd.DataFrame):
        return x.values, list(x.index), list(x.columns)
    gene_names = None
    cell_names = None
    return x, gene_names, cell_names


def decontx(x, z=None, batch=None, background=None, max_iter: int = 500,
            delta=(10.0, 10.0), estimate_delta: bool = True,
            convergence: float = 0.001, iter_log_lik: int = 10,
            seed: int | None = 12345, layer=None, copy: bool = False,
            verbose: bool = False) -> DecontXResult:
    """Estimate and remove ambient-RNA contamination with DecontX.

    Parameters
    ----------
    x : AnnData, DataFrame, ndarray or sparse matrix
        Raw UMI counts of *filtered* cells. AnnData is cells-by-genes;
        a DataFrame / array / sparse matrix is genes-by-cells. Empty
        droplets are not required.
    z : array-like
        Cell cluster / population labels (one per cell). Required --
        DecontX needs a clustering of broad cell types. When ``x`` is an
        AnnData, ``z`` may be the name of a column in ``x.obs``.
    batch : array-like, optional
        Per-cell batch labels; DecontX runs separately within each
        batch. When ``x`` is an AnnData this may be an ``obs`` column.
    background : DataFrame, ndarray or sparse matrix, optional
        Raw empty-droplet counts (genes-by-cells). When supplied, the
        empirical transcript distribution of the empty droplets anchors
        the contamination distribution ``eta`` (it is not re-estimated).
    max_iter : int
        Maximum EM iterations. Default 500.
    delta : (float, float)
        Beta/Dirichlet concentration prior for the per-cell native
        proportion ``theta`` (native pseudo-count, contamination
        pseudo-count). Used as the initial value when
        ``estimate_delta`` is True, otherwise fixed.
    estimate_delta : bool
        Whether to re-estimate ``delta`` each iteration via a Dirichlet
        MLE. Default True.
    convergence : float
        Stop when the maximum change in ``theta`` falls below this.
    iter_log_lik : int
        Record the log-likelihood every this many iterations.
    seed : int or None
        Seed for the random initialisation of ``theta``.
    layer : str, optional
        AnnData layer to use instead of ``.X``.
    copy : bool
        When ``x`` is an AnnData and ``copy`` is True, return a new
        AnnData with results written into ``.obs`` / ``.layers``
        instead of a :class:`DecontXResult`.
    verbose : bool
        Print per-iteration progress.

    Returns
    -------
    DecontXResult, or AnnData when ``x`` is an AnnData and ``copy`` is
    True (results in ``obs['decontX_contamination']``,
    ``obs['decontX_clusters']`` and ``layers['decontX_counts']``).
    """
    is_anndata = hasattr(x, "obs") and hasattr(x, "var") and hasattr(x, "X")

    # z / batch may name an AnnData obs column.
    if is_anndata:
        if isinstance(z, str):
            z = x.obs[z].values
        if isinstance(batch, str):
            batch = x.obs[batch].values

    counts, gene_names, cell_names = _extract_counts(x, layer=layer)
    counts = _to_genes_by_cells_sparse(counts)
    G, C = counts.shape

    if z is None:
        raise ValueError(
            "'z' (cell cluster labels) is required. Cluster the cells "
            "first, e.g. with Leiden, and pass the labels.")
    z = np.asarray(z)
    if len(z) != C:
        raise ValueError("'z' must have one label per cell.")

    if background is not None:
        background = _to_genes_by_cells_sparse(background)
        if background.shape[0] != G:
            raise ValueError(
                "'background' must have the same number of genes as 'x'.")

    if batch is None:
        batch = np.array(["all_cells"] * C)
    else:
        batch = np.asarray(batch)
    batch_index = pd.unique(batch)

    run_params = {
        "max_iter": max_iter, "delta": tuple(delta),
        "estimate_delta": estimate_delta, "convergence": convergence,
        "iter_log_lik": iter_log_lik, "seed": seed, "batch": batch,
    }

    contamination = np.full(C, np.nan)
    return_z = np.empty(C, dtype=object)
    # Accumulate the decontaminated matrix as COO triplets across batches.
    dec_rows: list = []
    dec_cols: list = []
    dec_vals: list = []
    estimates: dict = {}

    for bat in batch_index:
        sel = np.where(batch == bat)[0]
        counts_bat = counts[:, sel].tocsc()
        z_bat = z[sel]
        bg_bat = None
        if background is not None:
            bg_bat = background

        if verbose:
            print(f"DecontX: analysing batch '{bat}' "
                  f"({len(sel)} cells)")

        res = _decontx_one_batch(
            counts_bat, z_bat, bg_bat, max_iter=max_iter, delta=delta,
            estimate_delta=estimate_delta, convergence=convergence,
            iter_log_lik=iter_log_lik, seed=seed, verbose=verbose)

        native = calculate_native_matrix(
            counts_bat, res["theta"], res["eta"], res["phi"],
            res["z"], pseudocount=1e-20).tocoo()
        dec_rows.append(native.row)
        dec_cols.append(sel[native.col])  # map local -> global column index
        dec_vals.append(native.data)

        contamination[sel] = res["contamination"]
        if len(batch_index) > 1:
            return_z[sel] = [f"{bat}-{zz}" for zz in res["z"]]
        else:
            return_z[sel] = res["z"]
        estimates[bat] = res

    decontx_counts = sp.coo_matrix(
        (np.concatenate(dec_vals) if dec_vals else np.array([]),
         (np.concatenate(dec_rows) if dec_rows else np.array([], dtype=int),
          np.concatenate(dec_cols) if dec_cols else np.array([], dtype=int))),
        shape=(G, C)).tocsc()

    result = DecontXResult(
        contamination=contamination,
        decontx_counts=decontx_counts,
        z=return_z,
        estimates=estimates,
        run_params=run_params,
        gene_names=gene_names,
        cell_names=cell_names,
    )

    if is_anndata and copy:
        adata = x.copy()
        adata.obs["decontX_contamination"] = contamination
        adata.obs["decontX_clusters"] = pd.Categorical(return_z)
        # AnnData layer is cells x genes -> transpose back.
        adata.layers["decontX_counts"] = result.decontx_counts.T.tocsr()
        adata.uns["decontX"] = {"run_params": run_params,
                                "estimates": estimates}
        return adata

    return result


def _to_genes_by_cells_sparse(counts) -> sp.csc_matrix:
    """Coerce ``counts`` to a float CSC sparse matrix."""
    if sp.issparse(counts):
        return counts.tocsc().astype(float)
    return sp.csc_matrix(np.asarray(counts, dtype=float))


def _decontx_one_batch(counts, z, background, max_iter, delta,
                       estimate_delta, convergence, iter_log_lik,
                       seed, verbose):
    """Run the variational-EM loop for a single batch of cells."""
    counts = counts.tocsc().astype(float)
    G, C = counts.shape
    z = _process_cell_labels(z, C)

    rng = np.random.default_rng(seed)
    delta = np.asarray(delta, dtype=float)

    # Initial per-cell native proportion theta ~ Beta(delta).
    theta = rng.beta(delta[0], delta[1], size=C)

    init = decontx_initialize(counts, theta, z, pseudocount=1e-20)
    phi = init["phi"]
    eta = init["eta"]

    estimate_eta = True
    if background is not None:
        # Anchor eta with the empirical empty-droplet distribution.
        bg = background.tocsc().astype(float)
        eta_tilda = np.asarray(bg.sum(axis=1)).ravel() + 1e-20
        eta_vec = eta_tilda / eta_tilda.sum()
        eta = np.tile(eta_vec[:, None], (1, phi.shape[1]))
        estimate_eta = False

    counts_colsums = np.asarray(counts.sum(axis=0)).ravel()
    ll = [decontx_loglik(counts, theta, eta, phi, z, pseudocount=1e-20)]

    theta_prev = theta.copy()
    converged = False
    iter_count = 0
    next_decon = {"theta": theta, "phi": phi, "eta": eta,
                  "delta": delta, "contamination": np.zeros(C)}

    while iter_count < max_iter and not converged:
        next_decon = decontx_em(
            counts, counts_colsums, theta, eta, phi, z,
            estimate_eta=estimate_eta, estimate_delta=estimate_delta,
            delta=delta, pseudocount=1e-20)
        theta = next_decon["theta"]
        phi = next_decon["phi"]
        eta = next_decon["eta"]
        delta = next_decon["delta"]

        max_div = float(np.max(np.abs(theta_prev - theta)))
        if max_div < convergence:
            converged = True
        theta_prev = theta.copy()
        iter_count += 1

        if iter_count % iter_log_lik == 0 or converged:
            ll.append(decontx_loglik(counts, theta, eta, phi, z,
                                     pseudocount=1e-20))
            if verbose:
                print(f"  iter {iter_count} | converge: {max_div:.4g}")

    return {
        "z": z,
        "phi": phi,
        "eta": eta,
        "theta": theta,
        "delta": delta,
        "contamination": next_decon["contamination"],
        "log_likelihood": ll,
        "iteration": iter_count,
    }

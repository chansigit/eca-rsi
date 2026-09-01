"""Core variational-EM routines for DecontX.

Vendored from ``pydecontx`` (Apache-2.0; see ../../THIRD_PARTY_NOTICES.md),
itself a pure-Python / numpy / scipy port of the C++ inner loops of the
Bioconductor ``decontX`` package (``src/DecontX.cpp``):

* :func:`decontx_initialize` -- initial native (``phi``) and contamination
  (``eta``) gene distributions from a random ``theta``.
* :func:`decontx_em` -- one variational-EM step updating ``phi``, ``eta``,
  ``theta`` and (optionally) the Dirichlet hyper-parameter ``delta``.
* :func:`decontx_loglik` -- the two-component multinomial log-likelihood.
* :func:`calculate_native_matrix` -- the decontaminated count matrix.

All matrices follow the R convention: ``counts`` is genes-by-cells
(rows = genes, columns = cells); ``phi`` / ``eta`` are genes-by-clusters.
``z`` holds 1-based integer cluster labels (one per cell).
"""
from __future__ import annotations

import numpy as np
import scipy.sparse as sp

from ._dirichlet import fit_dirichlet

__all__ = [
    "decontx_initialize",
    "decontx_em",
    "decontx_loglik",
    "calculate_native_matrix",
]


def _as_csc(counts) -> sp.csc_matrix:
    """Return ``counts`` as a float CSC sparse matrix (genes x cells)."""
    if sp.issparse(counts):
        return counts.tocsc().astype(float)
    return sp.csc_matrix(np.asarray(counts, dtype=float))


def decontx_initialize(counts, theta, z, pseudocount: float = 1e-20):
    """Initialise the native (``phi``) and contamination (``eta``) matrices.

    Port of the C++ ``decontXInitialize``. ``phi[:, k]`` accumulates
    ``theta_j * counts`` over all cells ``j`` assigned to cluster ``k``;
    ``eta`` is the row-sum complement (every *other* cluster's signal),
    and both are column-normalised to proportions.

    Parameters
    ----------
    counts : (G, C) array or sparse matrix
        Gene-by-cell UMI counts.
    theta : (C,) array
        Initial native proportion for each cell.
    z : (C,) int array
        1-based cluster label for each cell.
    pseudocount : float
        Added to every cell of ``phi``/``eta`` before normalising.

    Returns
    -------
    dict with keys ``phi`` and ``eta`` -- (G, K) numpy arrays.
    """
    counts = _as_csc(counts)
    theta = np.asarray(theta, dtype=float)
    z = np.asarray(z, dtype=int)
    G, C = counts.shape
    K = int(z.max())

    phi = np.full((G, K), pseudocount, dtype=float)
    indptr, indices, data = counts.indptr, counts.indices, counts.data
    for j in range(C):
        k = z[j] - 1
        start, end = indptr[j], indptr[j + 1]
        rows = indices[start:end]
        vals = data[start:end] * theta[j]
        np.add.at(phi[:, k], rows, vals)

    phi_rowsum = phi.sum(axis=1)
    eta = phi_rowsum[:, None] - phi

    phi = phi / phi.sum(axis=0, keepdims=True)
    eta = eta / eta.sum(axis=0, keepdims=True)
    return {"phi": phi, "eta": eta}


def decontx_em(counts, counts_colsums, theta, eta, phi, z,
               estimate_eta: bool = True, estimate_delta: bool = True,
               delta=(10.0, 10.0), pseudocount: float = 1e-20):
    """One variational-EM update of the DecontX model.

    Port of the C++ ``decontXEM``. For every observed transcript the
    variational native/contaminant responsibility is

    ``p_native  = (phi[i,k] + pc) * (theta[j] + pc)``
    ``p_contam  = (eta[i,k] + pc) * (1 - theta[j] + pc)``
    ``normp     = p_native / (p_native + p_contam)``

    (the non-log form -- exact for a two-component mixture and what the
    C++ code uses). The native mass ``normp * x`` is accumulated into the
    new ``phi`` by cluster; ``eta`` is the row-sum complement. ``theta``
    is then the posterior mean of a Beta/Dirichlet with concentration
    ``delta``, which is itself re-estimated by :func:`fit_dirichlet`.

    Returns a dict with the updated ``phi``, ``eta``, ``theta``,
    ``delta`` and the per-cell ``contamination`` fraction.
    """
    counts = _as_csc(counts)
    theta = np.asarray(theta, dtype=float)
    counts_colsums = np.asarray(counts_colsums, dtype=float)
    phi = np.asarray(phi, dtype=float)
    eta = np.asarray(eta, dtype=float)
    z = np.asarray(z, dtype=int)
    delta = np.asarray(delta, dtype=float)

    G, C = counts.shape
    K = phi.shape[1]

    new_phi = np.zeros((G, K), dtype=float)
    native_total = np.zeros(C, dtype=float)

    indptr, indices, data = counts.indptr, counts.indices, counts.data
    for j in range(C):
        k = z[j] - 1
        start, end = indptr[j], indptr[j + 1]
        if start == end:
            continue
        rows = indices[start:end]
        x = data[start:end]
        p_native = (phi[rows, k] + pseudocount) * (theta[j] + pseudocount)
        p_contam = (eta[rows, k] + pseudocount) * (1.0 - theta[j] + pseudocount)
        normp = p_native / (p_native + p_contam)
        px = normp * x
        np.add.at(new_phi[:, k], rows, px)
        native_total[j] = px.sum()

    if estimate_eta:
        phi_rowsum = new_phi.sum(axis=1)
        new_eta = phi_rowsum[:, None] - new_phi
    else:
        new_eta = eta

    new_phi = new_phi / new_phi.sum(axis=0, keepdims=True)
    if estimate_eta:
        new_eta = new_eta / new_eta.sum(axis=0, keepdims=True)

    # Update theta (and optionally its Dirichlet hyper-parameter delta).
    contamination_prop = (counts_colsums - native_total) / counts_colsums
    native_prop = 1.0 - contamination_prop
    new_delta = delta
    if estimate_delta:
        theta_raw = np.column_stack([native_prop, contamination_prop])
        new_delta = fit_dirichlet(theta_raw)["alpha"]

    new_theta = (native_total + new_delta[0]) / (counts_colsums
                                                 + np.sum(new_delta))

    return {
        "phi": new_phi,
        "eta": new_eta,
        "theta": new_theta,
        "delta": new_delta,
        "contamination": contamination_prop,
    }


def decontx_loglik(counts, theta, eta, phi, z, pseudocount: float = 1e-20):
    """Two-component multinomial log-likelihood of the DecontX model.

    Port of the C++ ``decontXLogLik``:
    ``ll = sum_{i,j} x_{ij} * log(phi*theta + eta*(1-theta) + pc)``.
    """
    counts = _as_csc(counts)
    theta = np.asarray(theta, dtype=float)
    phi = np.asarray(phi, dtype=float)
    eta = np.asarray(eta, dtype=float)
    z = np.asarray(z, dtype=int)

    loglik = 0.0
    indptr, indices, data = counts.indptr, counts.indices, counts.data
    for j in range(counts.shape[1]):
        k = z[j] - 1
        start, end = indptr[j], indptr[j + 1]
        if start == end:
            continue
        rows = indices[start:end]
        x = data[start:end]
        mix = (phi[rows, k] * theta[j]
               + eta[rows, k] * (1.0 - theta[j]) + pseudocount)
        loglik += float(np.sum(x * np.log(mix)))
    return loglik


def calculate_native_matrix(counts, theta, eta, phi, z,
                            pseudocount: float = 1e-20) -> sp.csc_matrix:
    """Return the decontaminated (native) count matrix.

    Port of the C++ ``calculateNativeMatrix``: each observed entry is
    scaled by its variational native responsibility ``normp``. Values
    may be non-integer; round for integer counts.
    """
    counts = _as_csc(counts).copy()
    theta = np.asarray(theta, dtype=float)
    phi = np.asarray(phi, dtype=float)
    eta = np.asarray(eta, dtype=float)
    z = np.asarray(z, dtype=int)

    indptr, indices, data = counts.indptr, counts.indices, counts.data
    out = data.copy()
    for j in range(counts.shape[1]):
        k = z[j] - 1
        start, end = indptr[j], indptr[j + 1]
        if start == end:
            continue
        rows = indices[start:end]
        p_native = np.log(phi[rows, k] + pseudocount) + np.log(theta[j]
                                                               + pseudocount)
        p_contam = np.log(eta[rows, k] + pseudocount) + np.log(
            1.0 - theta[j] + pseudocount)
        normp = np.exp(p_native) / (np.exp(p_contam) + np.exp(p_native))
        out[start:end] = data[start:end] * normp

    native = sp.csc_matrix((out, indices.copy(), indptr.copy()),
                           shape=counts.shape)
    return native

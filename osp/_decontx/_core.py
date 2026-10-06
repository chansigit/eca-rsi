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
from numba import get_num_threads, njit, prange

from ._dirichlet import fit_dirichlet

__all__ = [
    "calculate_native_matrix",
    "decontx_em",
    "decontx_initialize",
    "decontx_loglik",
]


def _as_csc(counts) -> sp.csc_matrix:
    """Return ``counts`` as a float64 CSC sparse matrix (genes x cells);
    already-conforming input is returned as is (no copy per EM iteration)."""
    if sp.isspmatrix_csc(counts) and counts.dtype == np.float64:
        return counts
    if sp.issparse(counts):
        return counts.tocsc().astype(np.float64)
    return sp.csc_matrix(np.asarray(counts, dtype=np.float64))


# The per-cell loops below are the whole cost of DecontX (a 19k-cell sample
# spent 76 of its 86 s in decontx_em's Python loop). They are plain sparse
# column sweeps, so numba compiles them to native loops with identical
# arithmetic; the numpy versions stay as the reference the tests compare to.
@njit(cache=True, parallel=True)
def _em_sweep(indptr, indices, data, phi, eta, theta, z, pseudocount, new_phi, native_total, n_chunks):
    # cells are split into contiguous chunks, one per thread; each chunk
    # accumulates into its own phi slab so no two threads touch the same
    # entry, then the slabs are summed in a fixed order (deterministic)
    C = theta.shape[0]
    G, K = new_phi.shape
    slabs = np.zeros((n_chunks, G, K))
    step = (C + n_chunks - 1) // n_chunks
    for c in prange(n_chunks):
        for j in range(c * step, min((c + 1) * step, C)):
            k = z[j] - 1
            tj = theta[j] + pseudocount
            cj = 1.0 - theta[j] + pseudocount
            acc = 0.0
            for p in range(indptr[j], indptr[j + 1]):
                i = indices[p]
                p_native = (phi[i, k] + pseudocount) * tj
                p_contam = (eta[i, k] + pseudocount) * cj
                px = p_native / (p_native + p_contam) * data[p]
                slabs[c, i, k] += px
                acc += px
            native_total[j] = acc
    for c in range(n_chunks):
        new_phi += slabs[c]


@njit(cache=True, parallel=True)
def _loglik_sweep(indptr, indices, data, phi, eta, theta, z, pseudocount, n_chunks):
    C = theta.shape[0]
    partial = np.zeros(n_chunks)
    step = (C + n_chunks - 1) // n_chunks
    for c in prange(n_chunks):
        acc = 0.0
        for j in range(c * step, min((c + 1) * step, C)):
            k = z[j] - 1
            tj = theta[j]
            cj = 1.0 - theta[j]
            for p in range(indptr[j], indptr[j + 1]):
                i = indices[p]
                acc += data[p] * np.log(phi[i, k] * tj + eta[i, k] * cj + pseudocount)
        partial[c] = acc
    return partial.sum()


@njit(cache=True, parallel=True)
def _native_sweep(indptr, indices, data, phi, eta, theta, z, pseudocount, out):
    for j in prange(theta.shape[0]):
        k = z[j] - 1
        lt = np.log(theta[j] + pseudocount)
        lc = np.log(1.0 - theta[j] + pseudocount)
        for p in range(indptr[j], indptr[j + 1]):
            i = indices[p]
            pn = np.exp(np.log(phi[i, k] + pseudocount) + lt)
            pc = np.exp(np.log(eta[i, k] + pseudocount) + lc)
            out[p] = data[p] * (pn / (pc + pn))


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


def decontx_em(
    counts,
    counts_colsums,
    theta,
    eta,
    phi,
    z,
    estimate_eta: bool = True,
    estimate_delta: bool = True,
    delta=(10.0, 10.0),
    pseudocount: float = 1e-20,
):
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
    _em_sweep(counts.indptr, counts.indices, counts.data, np.ascontiguousarray(phi), np.ascontiguousarray(eta),
              theta, z, float(pseudocount), new_phi, native_total, get_num_threads())

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

    new_theta = (native_total + new_delta[0]) / (counts_colsums + np.sum(new_delta))

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

    return float(_loglik_sweep(counts.indptr, counts.indices, counts.data, np.ascontiguousarray(phi),
                               np.ascontiguousarray(eta), theta, z, float(pseudocount), get_num_threads()))


def calculate_native_matrix(counts, theta, eta, phi, z, pseudocount: float = 1e-20) -> sp.csc_matrix:
    """Return the decontaminated (native) count matrix.

    Port of the C++ ``calculateNativeMatrix``: each observed entry is
    scaled by its variational native responsibility ``normp``. Values
    may be non-integer; round for integer counts.
    """
    counts = _as_csc(counts)
    theta = np.asarray(theta, dtype=float)
    phi = np.asarray(phi, dtype=float)
    eta = np.asarray(eta, dtype=float)
    z = np.asarray(z, dtype=int)

    indptr, indices, data = counts.indptr, counts.indices, counts.data
    out = data.copy()
    _native_sweep(indptr, indices, data, np.ascontiguousarray(phi), np.ascontiguousarray(eta), theta, z,
                  float(pseudocount), out)
    return sp.csc_matrix((out, indices.copy(), indptr.copy()), shape=counts.shape)

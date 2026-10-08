"""Summarize only Scanpy's undefined log-fold-change warnings per DEG call, and rank Scanpy's one-vs-rest
Wilcoxon test on sparse data from the stored values alone (every msp DEG call comes through here)."""

import logging
import warnings

import numba
import numpy as np
import scanpy as sc
from scanpy.tools import _rank_genes_groups as _scanpy_rgg
from scipy import sparse
from scipy import stats

from .resources import available_cpus

log = logging.getLogger(__name__)

SCANPY_WILCOXON = _scanpy_rgg._RankGenes.wilcoxon
RANK_BLOCK_NNZ = 1 << 26  # stored values per CSC block of genes: ~0.5 GB, whatever the input's size


@numba.njit(parallel=True, cache=True)  # the pool's NUMBA_CACHE_DIR, like osp's decontx kernels
def _rank_sums(indptr, indices, data, codes, sizes, n_cells):
    """Per gene of a CSC block, per group g (codes, the cells of no group are len(sizes)), the sum of the
    average ranks scanpy's rankdata gives the gene's values over all n_cells cells. Only the stored values
    are sorted: the n_zero cells without one tie at zero, between the negative and the positive values
    (NaN sorts last, untied, as in a dense sort). Ranks are halves of integers, so every sum is exact in
    float64 whatever its order: the sums equal Scanpy's to the bit."""
    k = len(sizes)
    out = np.zeros((k, len(indptr) - 1))
    for j in numba.prange(len(indptr) - 1):
        lo, n = indptr[j], indptr[j + 1] - indptr[j]
        values = data[lo:lo + n]
        order = np.argsort(values)
        n_zero, n_neg = n_cells - n, 0
        total, stored = np.zeros(k + 1), np.zeros(k + 1)
        a = 0
        while a < n:
            b = a + 1
            while b < n and values[order[b]] == values[order[a]]:
                b += 1
            if values[order[a]] < 0:
                rank, n_neg = 0.5 * (a + b + 1), b
            else:
                rank = 0.5 * (a + b + 1) + n_zero
            for t in range(a, b):
                g = codes[indices[lo + order[t]]]
                total[g] += rank
                stored[g] += 1
            a = b
        zero_rank = n_neg + 0.5 * (n_zero + 1)
        for g in range(k):
            out[g, j] = total[g] + (sizes[g] - stored[g]) * zero_rank
    return out


def _wilcoxon(self, *, tie_correct):
    """Scanpy 1.12.4's _RankGenes.wilcoxon for reference "rest" without tie correction on sparse X, the
    only form msp and zmip run, with the rank sums of _rank_sums in place of a dense rankdata of every gene
    chunk (single-threaded under Scanpy's n_jobs=1: 15-31 min of a 230k-cell parent-core DEG). Everything
    after the rank sums is Scanpy's code. Other forms run Scanpy's own method. Installed process-wide when
    msp is imported."""
    if tie_correct or self.ireference is not None or not sparse.issparse(self.X):
        yield from SCANPY_WILCOXON(self, tie_correct=tie_correct)
        return
    self._basic_stats()
    n_groups, (n_cells, n_genes) = self.groups_masks_obs.shape[0], self.X.shape
    codes = np.full(n_cells, n_groups)
    for group_index, mask_obs in enumerate(self.groups_masks_obs):
        codes[mask_obs] = group_index
    sizes = np.count_nonzero(self.groups_masks_obs, axis=1)
    X = self.X.tocsr()
    ends = np.cumsum(np.bincount(X.indices, minlength=n_genes))
    scores = np.empty((n_groups, n_genes))
    threads = numba.get_num_threads()  # Scanpy holds numba to settings.n_jobs here; use the task's CPUs
    numba.set_num_threads(max(1, min(available_cpus(), numba.config.NUMBA_NUM_THREADS)))
    try:
        left = 0
        while left < n_genes:  # column blocks bound the CSC copy (the pool's RSS watchdog counts it)
            start = ends[left - 1] if left else 0
            right = min(n_genes, max(left + 1, int(np.searchsorted(ends, start + RANK_BLOCK_NNZ, side="right"))))
            block = X[:, left:right].tocsc()
            scores[:, left:right] = _rank_sums(block.indptr, block.indices, block.data, codes, sizes, n_cells)
            left = right
    finally:
        numba.set_num_threads(threads)
    # From here on, Scanpy's code for reference "rest" verbatim (coef 1: no tie correction).
    for group_index, mask_obs in enumerate(self.groups_masks_obs):
        n_active = np.count_nonzero(mask_obs)
        std_dev = np.sqrt(1 * n_active * (n_cells - n_active) * (n_cells + 1) / 12.0)
        scores[group_index, :] = (scores[group_index, :] - (n_active * (n_cells + 1) / 2.0)) / std_dev
        scores[np.isnan(scores)] = 0
        pvals = 2 * stats.distributions.norm.sf(np.abs(scores[group_index, :]))
        yield group_index, scores[group_index], pvals


_scanpy_rgg._RankGenes.wilcoxon = _wilcoxon


def rank_genes_groups(*args, **kwargs):
    """Run Scanpy's test (Wilcoxon ranked by _wilcoxon); preserve other warnings and all errors.

    Zero expression can produce infinite/NaN log-fold changes. These values
    remain in the results; the repeated warning is summarized, not repaired.
    """
    caught = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.filterwarnings(
                "always",
                message=r"(divide by zero|invalid value) encountered in log2$",
                category=RuntimeWarning,
                module=r"scanpy\.tools\._rank_genes_groups$",
            )
            return sc.tl.rank_genes_groups(*args, **kwargs)
    finally:
        count = 0
        for warning in caught:
            is_logfc = (
                issubclass(warning.category, RuntimeWarning)
                and warning.filename.replace("\\", "/").endswith("/scanpy/tools/_rank_genes_groups.py")
                and str(warning.message)
                in {
                    "divide by zero encountered in log2",
                    "invalid value encountered in log2",
                }
            )
            if is_logfc:
                count += 1
            else:
                warnings.warn_explicit(
                    warning.message,
                    warning.category,
                    warning.filename,
                    warning.lineno,
                )
        if count:
            log.warning(
                "== DEG: %d Scanpy log-fold-change warnings (zero/invalid expression); "
                "non-finite values remain in the DEG results",
                count,
            )

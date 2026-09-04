"""Dirichlet maximum-likelihood estimation.

Vendored from ``pydecontx`` (Apache-2.0; see ../../THIRD_PARTY_NOTICES.md).
A pure-Python port of ``MCMCprecision::fit_dirichlet`` -- the fixed-point
iteration of Minka (2000) used by R ``decontX`` to update the per-cell
``delta`` (theta) concentration parameters at every EM iteration.
"""
from __future__ import annotations

import numpy as np
from scipy.special import digamma, polygamma

# Euler-Mascheroni constant ( -psi(1) ).
_EULER_GAMMA = -digamma(1.0)


def inv_digamma(y: np.ndarray, iter: int = 5) -> np.ndarray:
    """Inverse of the digamma function (Minka, 2000).

    Mirrors ``MCMCprecision::inv_digamma``: starts from ``exp(y) + 0.5``
    (or ``-1/(y + gamma)`` when ``y < -2.22``) and applies a few Newton
    steps using the trigamma function.
    """
    y = np.asarray(y, dtype=float)
    x = np.exp(y) + 0.5
    mask = y < -2.22
    x[mask] = -1.0 / (y[mask] + _EULER_GAMMA)
    for _ in range(iter):
        x = x - (digamma(x) - y) / polygamma(1, x)
    return x


def dirichlet_fp(alpha: np.ndarray, logx_mean: np.ndarray,
                 maxit: int = 100000, abstol: float = 1e-5) -> np.ndarray:
    """Fixed-point iteration for Dirichlet MLE (Minka, 2000).

    Port of ``MCMCprecision::dirichlet_fp``.
    """
    alpha = np.asarray(alpha, dtype=float).copy()
    logx_mean = np.asarray(logx_mean, dtype=float)
    cnt = 0
    diff = 1.0
    while (diff > abstol) and (cnt < maxit):
        alpha0 = alpha
        alpha = inv_digamma(digamma(np.sum(alpha0)) + logx_mean)
        cnt += 1
        diff = np.max(np.abs(alpha0 - alpha))
    return alpha


def fit_dirichlet(x: np.ndarray, const: float | None = None,
                  maxit: int = 100000, abstol: float = 0.1) -> dict:
    """Estimate the parameters of a Dirichlet distribution.

    Pure-Python port of ``MCMCprecision::fit_dirichlet`` (Minka 2000
    fixed-point algorithm). ``x`` is a 2-D array of Dirichlet samples,
    one observation per row.

    Returns a dict with keys ``alpha`` (the estimated concentration
    vector) and ``sum`` (its sum). The default ``abstol`` of 0.1 matches
    the R default used inside ``decontX``.
    """
    x = np.asarray(x, dtype=float)
    if x.ndim != 2:
        raise ValueError("'x' must be a 2-D array of Dirichlet samples.")

    if x.min() == 0:
        if const is None:
            const = x[x > 0].min() * 0.01
        x = (x + const) / (1.0 + 2.0 * const)

    x = x / x.sum(axis=1, keepdims=True)
    logx_mean = np.mean(np.log(x), axis=0)

    # Heuristic starting values (method of moments).
    x_mean = np.mean(x, axis=0)
    x_squares = np.mean(x ** 2, axis=0)
    denom = x_squares - x_mean ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = (x_mean - x_squares) / denom
    alpha0 = xi * x_mean

    alpha = None
    try:
        cand = dirichlet_fp(np.maximum(0.01, alpha0), logx_mean,
                            maxit=maxit, abstol=abstol)
        if np.isfinite(cand).all() and (cand > 0).all():
            alpha = cand
    except Exception:  # noqa: BLE001  # pragma: no cover - retry from a safer start
        alpha = None

    if alpha is None:
        # A fixed asymmetric fallback preserves decontx(seed=...) reproducibility.
        # The previous unseeded random start made rare numerical fallbacks vary
        # across identical runs even when the caller supplied a seed.
        fallback = np.linspace(0.5, 1.0, num=len(alpha0), endpoint=False)
        alpha = dirichlet_fp(fallback, logx_mean, maxit=maxit, abstol=abstol)
        if not np.isfinite(alpha).all() or (alpha <= 0).any():
            raise FloatingPointError("Dirichlet fitting did not produce finite positive parameters")

    return {"alpha": alpha, "sum": float(np.sum(alpha))}

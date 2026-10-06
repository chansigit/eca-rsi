"""The numba sweeps must reproduce the original numpy loops bit-for-bit-ish."""
import numpy as np
import scipy.sparse as sp

from osp._decontx._core import calculate_native_matrix, decontx_em, decontx_loglik


def _reference_em(counts, colsums, theta, eta, phi, z, pc=1e-20):
    G, C = counts.shape; K = phi.shape[1]
    new_phi = np.zeros((G, K)); native = np.zeros(C)
    for j in range(C):
        k = z[j] - 1; s, e = counts.indptr[j], counts.indptr[j + 1]
        rows, x = counts.indices[s:e], counts.data[s:e]
        pn = (phi[rows, k] + pc) * (theta[j] + pc); pcn = (eta[rows, k] + pc) * (1 - theta[j] + pc)
        px = pn / (pn + pcn) * x
        np.add.at(new_phi[:, k], rows, px); native[j] = px.sum()
    return new_phi / new_phi.sum(0, keepdims=True), native


def _case(seed=0, G=300, C=500, K=4):
    rng = np.random.default_rng(seed)
    counts = sp.random(G, C, density=0.08, random_state=rng, data_rvs=lambda n: rng.poisson(3, n) + 1).tocsc().astype(float)
    z = rng.integers(1, K + 1, C); theta = rng.beta(10, 10, C)
    phi = rng.dirichlet(np.ones(G), K).T; eta = rng.dirichlet(np.ones(G), K).T
    return counts, z, theta, phi, eta


def test_em_matches_numpy_reference():
    counts, z, theta, phi, eta = _case()
    colsums = np.asarray(counts.sum(0)).ravel()
    ref_phi, ref_native = _reference_em(counts, colsums, theta, eta, phi, z)
    out = decontx_em(counts, colsums, theta, eta, phi, z, estimate_delta=False)
    assert np.allclose(out["phi"], ref_phi, rtol=1e-12, atol=1e-15)
    assert np.allclose(1 - out["contamination"], ref_native / colsums, rtol=1e-12)


def test_loglik_and_native_match_numpy_reference():
    counts, z, theta, phi, eta = _case(seed=1)
    ref = 0.0
    for j in range(counts.shape[1]):
        k = z[j] - 1; s, e = counts.indptr[j], counts.indptr[j + 1]; rows = counts.indices[s:e]
        ref += float(np.sum(counts.data[s:e] * np.log(phi[rows, k] * theta[j] + eta[rows, k] * (1 - theta[j]) + 1e-20)))
    assert np.isclose(decontx_loglik(counts, theta, eta, phi, z), ref, rtol=1e-12)
    nat = calculate_native_matrix(counts, theta, eta, phi, z)
    j = 7; k = z[j] - 1; s, e = counts.indptr[j], counts.indptr[j + 1]; rows = counts.indices[s:e]
    pn = np.exp(np.log(phi[rows, k] + 1e-20) + np.log(theta[j] + 1e-20)); pc = np.exp(np.log(eta[rows, k] + 1e-20) + np.log(1 - theta[j] + 1e-20))
    assert np.allclose(nat.data[s:e], counts.data[s:e] * pn / (pc + pn), rtol=1e-12)
    assert (nat.data <= counts.data + 1e-12).all()

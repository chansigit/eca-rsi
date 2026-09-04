import importlib
from types import SimpleNamespace

import anndata as ad
import numpy as np
import pandas as pd
import pytest
import scipy.sparse as sp

from osp._decontx.decontx import decontx
from osp.cluster import run_one_sample_pipeline
from osp.qc import (
    _log_bins,
    _remove_stale_sample_qc_outputs,
    _safe_expm1,
    assert_single_sample,
    qc_one_sample,
)


def test_single_sample_contract_rejects_empty_missing_and_multiple_samples():
    with pytest.raises(ValueError, match="0 cells"):
        assert_single_sample(ad.AnnData(np.empty((0, 2))))

    missing = ad.AnnData(np.ones((2, 2)), obs=pd.DataFrame({"sample": ["A", None]}))
    with pytest.raises(ValueError, match="missing"):
        assert_single_sample(missing)

    multiple = ad.AnnData(np.ones((2, 2)), obs=pd.DataFrame({"sample": ["A", "B"]}))
    with pytest.raises(ValueError, match="single sample"):
        assert_single_sample(multiple)


def test_qc_supports_panels_with_fewer_than_twenty_genes():
    data = ad.AnnData(
        np.array([[2, 0, 1], [0, 3, 1], [1, 1, 1]], dtype=float),
        obs=pd.DataFrame({"sample": ["A", "A", "A"]}),
        var=pd.DataFrame(index=["G1", "G2", "G3"]),
    )
    result, summary = qc_one_sample(
        data,
        run_scrublet=False,
        run_decontx=False,
        run_dissociation_score=False,
        make_plots=False,
    )
    assert "pct_counts_in_top_20_genes" in result.obs
    assert "median_pct_top20" in summary


def test_qc_rejects_non_finite_thresholds_and_counts():
    data = ad.AnnData(np.ones((3, 3)), obs=pd.DataFrame({"sample": ["A"] * 3}))
    with pytest.raises(ValueError, match="invalid: nmads"):
        qc_one_sample(data, nmads=float("nan"), make_plots=False)

    data.X[0, 0] = -1
    with pytest.raises(ValueError, match="finite, non-negative"):
        qc_one_sample(data, run_scrublet=False, run_decontx=False, make_plots=False)


def test_qc_decontx_kwargs_can_override_seed_and_verbosity(monkeypatch):
    data = ad.AnnData(
        np.array([[2, 0, 1], [1, 1, 1], [0, 2, 1], [1, 0, 2]], dtype=float),
        obs=pd.DataFrame({"sample": ["A"] * 4}),
        var=pd.DataFrame(index=["G1", "G2", "G3"]),
    )

    def fake_decontx(input_data, **kwargs):
        assert kwargs["seed"] == 23
        assert kwargs["verbose"] is True
        return SimpleNamespace(
            contamination=np.full(input_data.n_obs, 0.1),
            z=np.asarray(kwargs["z"]),
            decontx_counts=sp.csc_matrix(input_data.X.T),
        )

    monkeypatch.setattr("osp._decontx.decontx", fake_decontx)
    result, summary = qc_one_sample(
        data,
        run_scrublet=False,
        run_dissociation_score=False,
        make_plots=False,
        decontx_kwargs={"z": [0, 0, 1, 1], "seed": 23, "verbose": True},
    )
    assert summary["decontx_z_source"] == "user"
    assert np.allclose(result.obs["decontX_contamination"], 0.1)


def test_log_bins_are_finite_and_increasing_for_zero_counts():
    bins = _log_bins(np.zeros(4))
    assert np.isfinite(bins).all()
    assert (np.diff(bins) > 0).all()
    assert _safe_expm1(1e9) is None


def test_qc_rerun_removes_only_same_sample_generated_files(tmp_path):
    for name in (
        "A_qc_total_counts.png",
        "A_qc_overview.json",
        "A_decontx_top_genes.csv",
        "B_qc_total_counts.png",
        "keep.txt",
    ):
        (tmp_path / name).touch()
    _remove_stale_sample_qc_outputs(tmp_path, "A")
    assert not list(tmp_path.glob("A_*"))
    assert (tmp_path / "B_qc_total_counts.png").exists()
    assert (tmp_path / "keep.txt").exists()


def test_decontx_rejects_invalid_per_cell_vectors_before_fitting():
    counts = np.array([[2, 1, 0], [0, 1, 3]], dtype=float)
    z = np.array([0, 0, 1])

    with pytest.raises(ValueError, match="batch.*one label per cell"):
        decontx(counts, z=z, batch=["A", "A"])
    with pytest.raises(ValueError, match="missing"):
        decontx(counts, z=[0, None, 1])
    with pytest.raises(ValueError, match="negative"):
        decontx(np.array([[1, -1], [2, 3]]), z=[0, 1])
    with pytest.raises(ValueError, match="zero total counts"):
        decontx(np.array([[1, 0], [2, 0]]), z=[0, 1])


def test_decontx_valid_small_input_retains_shape():
    counts = np.array([[4, 3, 0, 0], [0, 1, 3, 4], [1, 1, 1, 1]], dtype=float)
    result = decontx(
        counts,
        z=[0, 0, 1, 1],
        max_iter=2,
        estimate_delta=False,
        seed=4,
    )
    assert result.contamination.shape == (4,)
    assert result.decontx_counts.shape == counts.shape
    assert np.isfinite(result.contamination).all()


def test_decontx_rejects_non_finite_numerical_results(monkeypatch):
    module = importlib.import_module("osp._decontx.decontx")

    def bad_em(counts, counts_colsums, theta, eta, phi, z, **kwargs):
        return {
            "phi": phi,
            "eta": eta,
            "theta": np.full_like(theta, np.nan),
            "delta": np.array([10.0, 10.0]),
            "contamination": np.full_like(theta, np.nan),
        }

    monkeypatch.setattr(module, "decontx_em", bad_em)
    counts = np.array([[4, 3, 0, 0], [0, 1, 3, 4], [1, 1, 1, 1]], dtype=float)
    with pytest.raises(FloatingPointError, match="numerical fit failed"):
        module.decontx(
            counts,
            z=[0, 0, 1, 1],
            max_iter=1,
            estimate_delta=False,
        )


def test_failed_pipeline_rerun_invalidates_old_completion_markers(tmp_path):
    (tmp_path / "report.html").touch()
    (tmp_path / "annotation_proposal.json").touch()
    data = ad.AnnData(
        np.ones((2, 3)),
        obs=pd.DataFrame({"sample": ["A", "A"]}),
        var=pd.DataFrame(index=["G1", "G2", "G3"]),
    )
    with pytest.raises(ValueError, match="at least 3"):
        run_one_sample_pipeline(
            data,
            sample_label="A",
            outdir=tmp_path,
            qc_kwargs={
                "run_scrublet": False,
                "run_decontx": False,
                "run_dissociation_score": False,
                "make_plots": False,
                "hard_min_genes": 0,
                "hard_min_counts": 0,
            },
        )
    assert not (tmp_path / "report.html").exists()
    assert not (tmp_path / "annotation_proposal.json").exists()
    assert (tmp_path / "qc_summary.csv").exists()

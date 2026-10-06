"""Exercise invalidation, interrupted reruns, and report-only recovery."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import anndata as ad
import numpy as np
import pandas as pd
import pytest

import msp.integrate as integrate
import msp.integrate.pipeline as pipeline
import msp.steps as steps
from msp.report import generate_report
from msp.steps import begin_step, step_pending


def integrated_data():
    data = ad.AnnData(
        np.ones((20, 3)),
        obs=pd.DataFrame(
            {
                "batch": ["A"] * 10 + ["B"] * 10,
                "msp_leiden_r1.0": pd.Categorical(["0"] * 20),
                "msp_leiden_r2.0": pd.Categorical(["0"] * 10 + ["1"] * 10),
                "_msp_action": pd.Categorical(["keep"] * 20),
                "_msp_verdict": pd.Categorical(["real"] * 20),
            },
            index=[f"cell{i}" for i in range(20)],
        ),
    )
    data.layers["counts"] = data.X.copy()
    data.obsm["X_umap"] = np.arange(40).reshape(20, 2).astype(float)
    data.uns["msp"] = {
        "batch_col": "batch",
        "species": "",
        "resolutions": [0.3, 1.0, 2.0],
        "n_top_genes": 2000,
        "n_pcs_requested": 50,
        "n_neighbors": 15,
        "harmony": {},
        "inputs": ["input.h5ad"],
        "n_batches": 2,
    }
    return data


def inspection_proposal():
    return {
        "cluster_key": "msp_leiden_r1.0",
        "clusters": [{"cluster": "0", "action": "keep", "verdict": "real"}],
    }


def annotation_proposal(label):
    return {
        "cluster_key": "msp_leiden_r2.0",
        "merged_groups": [],
        "overall": label,
        "clusters": [
            {
                "cluster_id": c,
                "coarse_label": label,
                "fine_label": f"{label}-{c}",
                "action": "keep",
                "merge_target": None,
                "evidence": {},
            }
            for c in ("0", "1")
        ],
    }


def write_json(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def completed_run(tmp_path):
    data = integrated_data()
    data.write_h5ad(tmp_path / "integrated.h5ad")
    data.write_h5ad(tmp_path / "annotated.h5ad")
    write_json(tmp_path / "inspection_proposal.json", inspection_proposal())
    write_json(tmp_path / "annotation_proposal.json", annotation_proposal("OLD_LABEL"))
    (tmp_path / "inspection_notes.md").write_text("old inspection notes")
    (tmp_path / "annotation_notes.md").write_text("old annotation notes")
    (tmp_path / "annotation_removed.csv").write_text("cell,annotate_remove\n")
    (tmp_path / "integration_summary.csv").write_text("n_cells,20\n")
    (tmp_path / "deg_global_msp_leiden_r1.0.csv").write_text("group,names,logfoldchanges\n")
    (tmp_path / "report_context.txt").write_text("test context")
    (tmp_path / "sample_decisions.csv").write_text("sample,decision\nA,include\n")
    (tmp_path / "caller-input.txt").write_text("leave intact")
    figures = tmp_path / "figures"
    figures.mkdir()
    for name in ("inspect_umap_action.png", "annotation_umap_coarse.png", "umap_batch.png"):
        (figures / name).write_bytes(b"test image bytes")
    generate_report(tmp_path)
    return tmp_path


def test_interrupted_archive_preserves_files_and_blocks_resume(completed_run, monkeypatch):
    original = {
        name: (completed_run / name).read_bytes() for name in ("report.html", "integrated.h5ad", "annotated.h5ad")
    }
    replace = steps.os.replace
    moves = []

    def fail_second_move(src, dst):
        moves.append(src)
        if len(moves) == 2:
            raise OSError("archive interrupted")
        replace(src, dst)

    monkeypatch.setattr(steps.os, "replace", fail_second_move)
    with pytest.raises(OSError, match="archive interrupted"):
        begin_step(completed_run, "integrate")
    assert moves[0].name == "report.html"
    assert step_pending(completed_run, "annotate")
    monkeypatch.setattr(steps.os, "replace", replace)
    begin_step(completed_run, "integrate")
    for name, contents in original.items():
        assert not (completed_run / name).exists()
        archived = list((completed_run / ".msp-history").glob(f"*/{name}"))
        assert len(archived) == 1 and archived[0].read_bytes() == contents


def test_completed_integration_allows_external_annotation_report(completed_run, monkeypatch):
    """Exercise the real integration entry/exit and the ZMIP-style report path."""
    data = integrated_data()
    # A new upstream run supersedes an older interrupted annotation, too.
    begin_step(completed_run, "annotate")

    monkeypatch.setattr(pipeline.sc.pp, "normalize_total", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.sc.pp, "log1p", lambda *args, **kwargs: None)

    def hvg(subset, **kwargs):
        subset.var["highly_variable"] = True

    def leiden(subset, key_added, **kwargs):
        subset.obs[key_added] = pd.Categorical(["0"] * subset.n_obs)

    def umap(subset):
        subset.obsm["X_umap"] = np.arange(subset.n_obs * 2).reshape(-1, 2).astype(float)

    monkeypatch.setattr(pipeline.sc.pp, "highly_variable_genes", hvg)
    monkeypatch.setattr(pipeline.sc.pp, "scale", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.sc.pp, "neighbors", lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline.sc.tl, "leiden", leiden)
    monkeypatch.setattr(pipeline.sc.tl, "umap", umap)
    monkeypatch.setattr(
        pipeline,
        "PCA",
        lambda **kwargs: SimpleNamespace(fit_transform=lambda x: np.zeros((len(x), kwargs["n_components"]))),
    )
    data.obs["batch"] = "A"  # One batch takes the existing Harmony skip branch.
    # Isolate numerical libraries from this filesystem recovery test.
    monkeypatch.setitem(sys.modules, "harmonypy", SimpleNamespace())
    partition = SimpleNamespace(
        labels=pd.DataFrame({"subcluster": ["c0_0"] * data.n_obs}, index=data.obs_names),
        fragments=pd.DataFrame({"subcluster": ["c0_0"]}),
        overlap=pd.DataFrame(),
    )
    monkeypatch.setitem(
        sys.modules, "standissect_lite", SimpleNamespace(dissect_partition=lambda *args, **kwargs: partition)
    )
    for name in (
        "_minor_sibling_qc",
        "_cell_level_outliers",
        "_leiden_cluster_qc_violins",
        "_preannotation_removal_umap",
        "_cluster_annotations",
        "save_single_umap",
        "_qc_outputs",
        "_fractal_marker_heatmap",
    ):
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "_build_removal_mask", lambda *args: np.zeros(data.n_obs, dtype=bool))

    result, summary = integrate.integrate_adata(data, "batch", completed_run)
    assert summary["n_cells"] == data.n_obs
    assert not step_pending(completed_run, "integrate")
    assert not step_pending(completed_run, "annotate")
    assert not (completed_run / "annotation_proposal.json").exists()
    assert "_msp_action" not in ad.read_h5ad(completed_run / "integrated.h5ad").obs

    # External callers already write these public files; no new API is required.
    write_json(completed_run / "annotation_proposal.json", annotation_proposal("EXTERNAL_LABEL"))
    result.write_h5ad(completed_run / "annotated.h5ad")
    assert "EXTERNAL_LABEL" in Path(generate_report(completed_run)).read_text()

"""Explicit sample-map options: derive_from_cell_id and missing_as."""
import numpy as np
import pandas as pd
import pytest

from ecarsi.sample_mapping import SAMPLE_KEY, build_mapping


def _h5ad(tmp_path):
    import anndata as ad
    ids = ["AdultBrain_1.AAA", "AdultBrain_1.CCC", "AdultBrain_2.GGG", "FetalBrain_1.TTT"]
    obs = pd.DataFrame({"plate": ["P1", "P1", "missing", ""]}, index=ids)
    p = tmp_path / "in.h5ad"
    ad.AnnData(np.ones((4, 2), dtype=np.float32), obs=obs).write_h5ad(p)
    return p


def _spec(**src):
    return {"sources": {"input": {"rationale": "test", **src}}}


def test_derive_from_cell_id(tmp_path):
    table, decision = build_mapping(_h5ad(tmp_path), None, _spec(derive_from_cell_id=r"^([^.]+)\."), None)
    assert list(table["source_value"]) == ["AdultBrain_1", "AdultBrain_1", "AdultBrain_2", "FetalBrain_1"]
    assert table[SAMPLE_KEY].nunique() == 3
    assert decision["sources"]["input"]["sample_column"] is None


def test_derive_rejects_unmatched_and_bad_regex(tmp_path):
    with pytest.raises(ValueError, match="does not match"):
        build_mapping(_h5ad(tmp_path), None, _spec(derive_from_cell_id=r"^(Fetal[^.]+)\."), None)
    with pytest.raises(ValueError, match="one capture group"):
        build_mapping(_h5ad(tmp_path), None, _spec(derive_from_cell_id=r"^[^.]+\."), None)


def test_missing_as_keeps_unlabelled_cells(tmp_path):
    with pytest.raises(ValueError, match="leaves 2 cells NA"):
        build_mapping(_h5ad(tmp_path), None, _spec(sample_column="plate"), None)
    table, _ = build_mapping(_h5ad(tmp_path), None, _spec(sample_column="plate", missing_as="missing"), None)
    assert list(table["source_value"]) == ["P1", "P1", "missing", "missing"]
    with pytest.raises(ValueError, match="non-empty label"):
        build_mapping(_h5ad(tmp_path), None, _spec(sample_column="plate", missing_as=" "), None)

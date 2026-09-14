"""Scientific handoff guards: complete experiments, not just cell totals."""
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from ecarsi.execute import _experiment_audit
from ecarsi.plan import validate_sample_mapping


def test_complete_experiment_cannot_be_split_between_analysis_units(tmp_path: Path):
    obs = pd.DataFrame({"experiment": ["S1", "S1", "S2", "S2"],
                        "tissue": ["left", "right", "left", "right"]},
                       index=["001", "002", "003", "004"])
    data = ad.AnnData(sparse.csr_matrix(np.eye(4)), obs=obs)
    source = tmp_path / "source.h5ad"
    data.write_h5ad(source)
    plan = {"sample_mapping": {"source": {"sample_column": "experiment",
                                           "rationale": "source experiment ID"}},
            "analysis_units": [{"name": tissue, "members": [
                {"source": "source", "obs_filter": {"column": "tissue", "values": [tissue]}}]}
                for tissue in ("left", "right")]}
    with pytest.raises(ValueError, match="split a complete experiment"):
        _experiment_audit({"source": {"h5ad": str(source)}}, plan)
    plan["analysis_units"] = [{"name": "together", "members": [
        {"source": "source", "obs_filter": None}]}]
    assert _experiment_audit({"source": {"h5ad": str(source)}}, plan)["source"]["experiments"] == 2


def test_unknown_source_mapping_is_rejected():
    profile = {"name": "source", "obs_columns": {"experiment": {
        "n_unique": 2, "n_na": 0}}}
    with pytest.raises(ValueError, match="every source"):
        validate_sample_mapping({"sample_mapping": {}}, [profile])
    with pytest.raises(ValueError, match="leaves 2 cells NA"):
        validate_sample_mapping({"sample_mapping": {"source": {
            "sample_column": "experiment", "rationale": "metadata"}}},
            [{**profile, "obs_columns": {"experiment": {"n_unique": 2, "n_na": 2}}}])
    with pytest.raises(ValueError, match="conflicts with multiple explicit"):
        validate_sample_mapping({"sample_mapping": {"source": {
            "sample_column": None, "confirmed_single": True,
            "rationale": "one donor"}}},
            [{**profile, "obs_columns": {"sample_id": {"n_unique": 3, "n_na": 0}}}])

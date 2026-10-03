"""Exercise the actual worker entry point with deterministic kernel submissions."""
from __future__ import annotations

import pandas as pd

from ecarsi.stages.osp_contract import INPUT_CELLS
from ecarsi.run_state import file_identity, write_json
from tests.test_front_integration import matrix


def request(tmp_path, annotate=True):
    a = matrix(7)
    a.obs_names = [f"cell{i}" for i in range(6)] + ["removed"]
    a.obs["eca_sample_id"] = "A"
    a.write_h5ad(tmp_path / "subset.h5ad")
    pd.DataFrame({"cell_id": a.obs_names}).to_csv(tmp_path / INPUT_CELLS, index=False)
    req = {"identity": "test", "value": "A", "n_cells": 7, "runtime": {},
           "subset_identity": file_identity(tmp_path / "subset.h5ad"),
           "config": {"annotate": annotate, "scrublet": False, "decontx": False,
                      "resolution": 0.7, "species": "mouse", "tissue": "spleen",
                      "language": "Chinese", "model": "test", "effort": "high"}}
    path = tmp_path / "request.json"
    write_json(path, req)
    return path

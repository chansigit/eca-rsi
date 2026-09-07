"""A sample whose OSP QC removed every cell is finished-and-empty: fully
accounted for in qc_removed.csv, excluded from integration, never failed."""
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import layout as L
from ecarsi.ledger import _persample_frames
from ecarsi.osp_contract import is_done, is_empty, is_finished


def _empty_sample(d, cells, identity="id-1", kind="qc_zero_survivors", reason="hard_threshold"):
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"cell_id": cells}).to_csv(d / "input_cells.csv.gz", index=False)
    pd.DataFrame({"cell": cells, "qc_reason": [reason] * len(cells)}).to_csv(d / "qc_removed.csv", index=False)
    pd.Series({"n_cells": len(cells), "n_low_quality": len(cells)}, name="0").to_csv(d / "qc_summary.csv")
    (d / L.RUN_STATE).write_text(json.dumps({"state": "failed", "exit_code": 1, "failure_kind": kind,
                                             "identity": identity, "annotate": True}))


def test_is_empty_requires_every_cell_removed_with_a_reason(tmp_path):
    d = tmp_path / "s"
    _empty_sample(d, ["a", "b", "c"])
    assert is_empty(d) and is_empty(d, "id-1") and is_finished(d, True, "id-1")
    assert not is_done(d, True, "id-1")
    assert not is_empty(d, "other-identity")
    _empty_sample(d, ["a", "b", "c"], kind="qc_too_few_survivors")  # 1-2 survivors are not "empty"
    assert not is_empty(d)
    _empty_sample(d, ["a", "b", "c"], reason="")  # a removal without a reason is not accounted for
    assert not is_empty(d)
    _empty_sample(d, ["a", "b", "c"])
    pd.DataFrame({"cell": ["a", "b"], "qc_reason": ["x", "x"]}).to_csv(d / "qc_removed.csv", index=False)
    assert not is_empty(d)  # ledger shorter than the input


def test_ledger_accounts_for_an_empty_sample(tmp_path):
    unit = tmp_path / "unit"
    full = L.persample_root(unit) / "S1-aaaa"
    full.mkdir(parents=True)
    obs = pd.DataFrame({"_ann_coarse": ["A", "A"], "_ann_fine": ["a", "a"]}, index=pd.Index(["c1", "c2"], name="cell"))
    ad.AnnData(np.ones((2, 2)), obs=obs).write_h5ad(full / "clustered.h5ad")
    pd.DataFrame(columns=["cell", "qc_reason"]).to_csv(full / "qc_removed.csv", index=False)
    pd.DataFrame({"cell_id": ["c1", "c2"]}).to_csv(full / "input_cells.csv.gz", index=False)
    empty = L.persample_root(unit) / "S2-bbbb"
    _empty_sample(empty, ["d1", "d2", "d3"], identity="id-2")
    L.persample_manifest(unit).write_text(json.dumps({"samples": [
        {"value": "S1", "dir": str(full), "n_cells": 2, "identity": "id-1"},
        {"value": "S2", "dir": str(empty), "n_cells": 3, "identity": "id-2"}], "empty_samples": ["S2"]}))
    L.input_h5ad(unit).parent.mkdir(parents=True, exist_ok=True)
    ad.AnnData(np.ones((5, 2)), obs=pd.DataFrame(index=pd.Index(["c1", "c2", "d1", "d2", "d3"], name="cell"))).write_h5ad(L.input_h5ad(unit))
    frames = pd.concat(_persample_frames(unit))
    assert sorted(frames.index) == ["c1", "c2", "d1", "d2", "d3"]
    assert (frames.loc[["d1", "d2", "d3"], "osp_status"] == "removed:hard_threshold").all()
    assert (frames.loc[["c1", "c2"], "osp_status"] == "kept").all()


def test_crosssample_prerequisite_accepts_an_empty_sample(tmp_path):
    """load_persample gates the loop on every sample being finished; an
    empty sample has no annotation outputs but is finished (Fat v2 plate
    MAA000873 failed the loop entry here)."""
    from ecarsi.crosssample import load_persample

    unit = tmp_path / "unit"
    empty = L.persample_root(unit) / "S2-bbbb"
    _empty_sample(empty, ["d1", "d2", "d3"], identity="id-2")
    L.persample_manifest(unit).write_text(json.dumps({
        "schema_version": 2, "state": "complete", "failed_samples": [], "empty_samples": ["S2"],
        "samples": [{"value": "S2", "dir": str(empty), "n_cells": 3, "identity": "id-2"}]}))
    man = load_persample(unit)
    assert [s["value"] for s in man["samples"]] == ["S2"]
    _empty_sample(empty, ["d1", "d2", "d3"], identity="stale")  # identity mismatch is still incomplete
    with pytest.raises(SystemExit, match="incomplete samples: S2"):
        load_persample(unit)

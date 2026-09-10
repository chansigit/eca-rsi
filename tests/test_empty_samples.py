"""A sample that hands no cells on is finished-and-empty rather than failed,
provided every input cell is accounted for in qc_removed.csv with a reason.
Two ways to get there: QC removed everything, or one or two cells passed QC
and OSP booked them as `too_few_survivors` because clustering needs three."""
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


def _too_few_sample(d, removed_by_qc, survivors, identity="id-1", complete_ledger=True):
    """A sample where `survivors` passed QC but are below the clustering
    minimum. `complete_ledger=False` reproduces osp < 0.1.5, which left those
    cells in no ledger at all."""
    d.mkdir(parents=True, exist_ok=True)
    cells = list(removed_by_qc) + list(survivors)
    pd.DataFrame({"cell_id": cells}).to_csv(d / "input_cells.csv.gz", index=False)
    ledger = [(c, "hard_threshold") for c in removed_by_qc]
    if complete_ledger:
        ledger += [(c, "too_few_survivors") for c in survivors]
    pd.DataFrame(ledger, columns=["cell", "qc_reason"]).to_csv(d / "qc_removed.csv", index=False)
    # the QC summary stays honest: the survivors are not low quality
    pd.Series({"n_cells": len(cells), "n_low_quality": len(removed_by_qc)}, name="0").to_csv(d / "qc_summary.csv")
    (d / L.RUN_STATE).write_text(json.dumps({"state": "failed", "exit_code": 1,
                                             "failure_kind": "qc_too_few_survivors",
                                             "identity": identity, "annotate": True}))


def test_a_sample_below_the_clustering_minimum_is_empty_once_its_survivors_are_booked(tmp_path):
    d = tmp_path / "s"
    _too_few_sample(d, ["a", "b"], ["c"])
    assert is_empty(d) and is_finished(d, True, "id-1")
    assert not is_done(d, True, "id-1")
    assert not is_empty(d, "other-identity")


def test_an_unbooked_survivor_keeps_the_sample_failed(tmp_path):
    """osp < 0.1.5 wrote no ledger row for the survivors. Those cells are then
    unaccounted for — neither in a clustered.h5ad nor in qc_removed.csv — so
    the sample must stay a failure rather than silently lose them."""
    d = tmp_path / "s"
    _too_few_sample(d, ["a", "b"], ["c"], complete_ledger=False)
    assert not is_empty(d) and not is_finished(d, True, "id-1")


def test_the_ledger_reports_the_survivors_own_reason(tmp_path):
    unit = tmp_path / "unit"
    d = L.persample_root(unit) / "S1-aaaa"
    _too_few_sample(d, ["a", "b"], ["c"], identity="id-1")
    L.persample_manifest(unit).write_text(json.dumps({"samples": [
        {"value": "S1", "dir": str(d), "n_cells": 3, "identity": "id-1"}], "empty_samples": ["S1"]}))
    L.input_h5ad(unit).parent.mkdir(parents=True, exist_ok=True)
    ad.AnnData(np.ones((3, 2)), obs=pd.DataFrame(index=pd.Index(["a", "b", "c"], name="cell"))).write_h5ad(L.input_h5ad(unit))
    frames = pd.concat(_persample_frames(unit))
    assert frames.loc["c", "osp_status"] == "removed:too_few_survivors"
    assert (frames.loc[["a", "b"], "osp_status"] == "removed:hard_threshold").all()

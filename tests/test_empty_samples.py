"""A sample that hands no cells on is finished-and-empty rather than failed,
provided every input cell is accounted for in qc_removed.csv with a reason.
Two ways to get there: QC removed everything, or one or two cells passed QC
and OSP booked them as `too_few_survivors` because clustering needs three."""
import json

import pandas as pd

from ecarsi import layout as L
from ecarsi.stages.osp_contract import is_empty


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
    assert is_empty(d) and is_empty(d, "id-1")
    assert not is_empty(d, "other-identity")
    _empty_sample(d, ["a", "b", "c"], reason="")  # a removal without a reason is not accounted for
    assert not is_empty(d)
    _empty_sample(d, ["a", "b", "c"])
    pd.DataFrame({"cell": ["a", "b"], "qc_reason": ["x", "x"]}).to_csv(d / "qc_removed.csv", index=False)
    assert not is_empty(d)  # ledger shorter than the input


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
    assert is_empty(d)
    assert not is_empty(d, "other-identity")


def test_an_unbooked_survivor_keeps_the_sample_failed(tmp_path):
    """osp < 0.1.5 wrote no ledger row for the survivors. Those cells are then
    unaccounted for — neither in a clustered.h5ad nor in qc_removed.csv — so
    the sample must stay a failure rather than silently lose them."""
    d = tmp_path / "s"
    _too_few_sample(d, ["a", "b"], ["c"], complete_ledger=False)
    assert not is_empty(d)

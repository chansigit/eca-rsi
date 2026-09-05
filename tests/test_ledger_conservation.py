"""Real H5AD cell-set contracts for the cross-round identity ledger."""
import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import layout as L
from ecarsi.ledger import build_ledger, sankey_data, stage_list


def h5(path, cells, **columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    obs = pd.DataFrame(columns, index=pd.Index(cells, name="cell"))
    ad.AnnData(np.ones((len(cells), 2)), obs=obs).write_h5ad(path)


def csv(path, columns, rows=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)


@pytest.fixture
def case(tmp_path):
    unit = tmp_path / "unit"
    samples = []
    for value, directory, cells in [("S1", "S1-a19hash", ["001", "NA"]), ("S2", "S2-b20hash", ["c", "d"])]:
        d = L.persample_root(unit) / directory
        h5(d / "clustered.h5ad", cells, _ann_coarse=["A"] * 2, _ann_fine=["a"] * 2)
        csv(d / "qc_removed.csv", ["cell", "qc_reason"])
        csv(d / "input_cells.csv.gz", ["cell_id"], [[c] for c in cells])
        samples.append({"value": value, "dir": str(d), "n_cells": 2})
    L.persample_manifest(unit).write_text(json.dumps({"samples": samples}))
    h5(L.input_h5ad(unit), ["001", "NA", "c", "d"])
    rdir = L.round_dir(unit, 1)
    write_round(rdir, ["001", "NA"], ["001"], [], decisions=True)
    return unit, rdir


def write_round(rdir, entering, msp_kept, zmip_removed, decisions=False):
    idir, zdir = L.crosssample_dir(rdir), L.zoomin_dir(rdir)
    h5(idir / "integrated.h5ad", entering)
    if decisions:
        csv(idir / "sample_decisions.csv", ["sample", "decision"], [["S1", "include"], ["S2", "exclude"]])
    gone = set(entering) - set(msp_kept)
    csv(idir / "annotation_removed.csv", ["cell", "annotate_remove", "inspect_drop", "remove_reason"],
        [[cell, "False", "True", ""] for cell in sorted(gone)])
    h5(idir / "annotated.h5ad", msp_kept, msp_ann_coarse=["A"] * len(msp_kept), msp_ann_fine=["a"] * len(msp_kept))
    csv(zdir / "zmip_removed.csv", ["cell", "annotate_remove", "remove_reason"],
        [[cell, "True", "noise"] for cell in zmip_removed])
    kept = [cell for cell in msp_kept if cell not in zmip_removed]
    h5(zdir / "annotated_zmip.h5ad", kept, zmip_lineage=["A"] * len(kept),
       zmip_ann_coarse=["A"] * len(kept), zmip_ann_fine=["a"] * len(kept))
    (zdir / "zmip_plan.json").write_text(json.dumps({"lineages": [{"name": "A", "coarse_labels": ["A"], "zoom": False}]}))


def test_hashed_sample_paths_and_literal_cell_ids_and_removal_counts(case):
    unit, rdir = case
    ledger = build_ledger(unit, [rdir])
    assert ledger.loc["001", "sample"] == "S1"
    assert ledger.loc["NA", "r01_msp_status"] == "removed:inspect"
    assert ledger.loc[["c", "d"], "r01_msp_status"].tolist() == ["excluded-sample"] * 2
    assert ledger.loc["001", "r01_zmip_status"] == "not-zoomed"
    graph = sankey_data(ledger, stage_list(1))
    assert sum(n["count"] for n in graph["nodes"] if n["stage"] == 1 and n["removed"]) == 3


@pytest.mark.parametrize("name", ["crosssample/annotation_removed.csv", "zoomin/zmip_removed.csv"])
def test_missing_deletion_ledger_is_not_success(case, name):
    unit, rdir = case
    (rdir / name).unlink()
    with pytest.raises(ValueError, match="missing ledger"):
        build_ledger(unit, [rdir])


@pytest.mark.parametrize("ids", [["001", "001"], ["001", "invented"]])
def test_invalid_survivors_rejected(case, ids):
    unit, rdir = case
    h5(L.crosssample_dir(rdir) / "annotated.h5ad", ids)
    with pytest.raises(ValueError, match="cell|duplicate"):
        build_ledger(unit, [rdir])


def test_invalid_boolean_string_rejected(case):
    unit, rdir = case
    csv(L.crosssample_dir(rdir) / "annotation_removed.csv", ["cell", "annotate_remove"], [["NA", "perhaps"]])
    with pytest.raises(ValueError, match="invalid boolean"):
        build_ledger(unit, [rdir])


def test_zero_removals_and_not_zoomed_survive_next_round(case):
    unit, rdir = case
    r2 = L.round_dir(unit, 2)
    h5(r2 / L.ROUND_INPUT, ["001"])
    write_round(r2, ["001"], ["001"], [])
    ledger = build_ledger(unit, [rdir, r2])
    assert ledger.loc["001", "r02_msp_status"] == "kept"
    assert ledger.loc["001", "r02_zmip_status"] == "not-zoomed"
    assert ledger.loc["NA", "r02_msp_status"] == ""


def test_pruned_obs_sidecars_preserve_ids_and_support_browsing(case):
    unit, rdir = case
    for path in [*L.persample_root(unit).glob("*/clustered.h5ad"),
                 L.crosssample_dir(rdir) / "annotated.h5ad", L.zoomin_dir(rdir) / "annotated_zmip.h5ad"]:
        obj = ad.read_h5ad(path)
        obj.obs.to_csv(path.with_name(path.name + ".obs.csv.gz"), compression="gzip")
        path.unlink()
        path.with_name(path.name + L.PRUNED_SUFFIX).write_text("{}")
    integrated = L.crosssample_dir(rdir) / "integrated.h5ad"
    integrated.unlink()
    integrated.with_name(integrated.name + L.PRUNED_SUFFIX).write_text("{}")
    assert build_ledger(unit, [rdir]).loc["001", "r01_zmip_status"] == "not-zoomed"


def test_missing_input_or_unaccounted_removal_is_not_kept(case):
    unit, rdir = case
    csv(L.crosssample_dir(rdir) / "annotation_removed.csv", ["cell", "annotate_remove"])
    with pytest.raises(ValueError, match="conservation"):
        build_ledger(unit, [rdir])


def test_duplicate_deletion_ids_rejected(case):
    unit, rdir = case
    csv(L.crosssample_dir(rdir) / "annotation_removed.csv", ["cell", "annotate_remove"],
        [["NA", "False"], ["NA", "False"]])
    with pytest.raises(ValueError, match="duplicate cell IDs"):
        build_ledger(unit, [rdir])


def test_integrated_input_cannot_lose_cells(case):
    unit, rdir = case
    h5(L.crosssample_dir(rdir) / "integrated.h5ad", ["001"])
    with pytest.raises(ValueError, match="input cell set differs"):
        build_ledger(unit, [rdir])


def test_zmip_deletion_counts_and_reason(case):
    unit, rdir = case
    write_round(rdir, ["001", "NA"], ["001", "NA"], ["NA"], decisions=True)
    plan = L.zoomin_dir(rdir) / "zmip_plan.json"
    plan.write_text(json.dumps({"lineages": [{"name": "A", "coarse_labels": ["A"], "zoom": True}]}))
    ledger = build_ledger(unit, [rdir])
    assert ledger.loc["NA", "r01_zmip_status"] == "removed:agent:noise"
    assert ledger.loc["001", "r01_zmip_status"] == "kept"


def test_missing_osp_deletion_ledger_is_not_success(case):
    unit, rdir = case
    (L.sample_dirs(unit)[0] / "qc_removed.csv").unlink()
    with pytest.raises(ValueError, match="missing ledger"):
        build_ledger(unit, [rdir])

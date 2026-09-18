import base64
import json
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
from scipy import sparse

from ecarsi.agent.session import reference, verified
from ecarsi.stages.persample import partition, sealed, tool
from ecarsi.run_state import file_identity
from ecarsi.sample_mapping import mapping_identity
from ecarsi.warm_pool.state import read, save


def test_partition_preserves_confirmed_string_ids_and_batch_bound(tmp_path, monkeypatch):
    from ecarsi import upstream
    monkeypatch.setattr(upstream, "verify_snapshots", lambda *a: None)
    folder = tmp_path / "unit/input"
    folder.mkdir(parents=True)
    obs = pd.DataFrame({"source_unit": ["source"] * 4, "eca_source_cell_id": ["001", "002", "003", "004"]},
                       index=["001", "002", "003", "004"])
    data = ad.AnnData(sparse.eye(4, format="csr"), obs=obs)
    data.layers["counts"] = data.X.copy()
    data.write_h5ad(folder / "organized.h5ad")
    table = pd.DataFrame({"eca_sample_id": ["a", "a", "b", "b"], "excluded_reason": [""] * 4}, index=obs.index)
    table.to_csv(folder / "mapping.csv.gz")
    save(folder / "manifest.json", {"identity": file_identity(folder / "organized.h5ad"),
        "sample_mapping": {"path": "mapping.csv.gz", "identity": file_identity(folder / "mapping.csv.gz"),
                           "mapping_identity": mapping_identity(table), "decision": {}}})
    spec = {"unit": str(folder.parent), "input_manifest": reference(folder / "manifest.json"), "run_id": "test",
            "batch_size": 1, "max_batch_bytes": 2**20, "config": {}}
    found = []
    for offset in (0, 1):
        dest = tmp_path / str(offset)
        dest.mkdir()
        partition(spec, offset, dest)
        result = read(dest / "partition.json")
        assert result["total_samples"] == 2 and result["next_offset"] == offset + 1
        assert len(result["entries"]) == 1
        bundle = verified(result["entries"][0]["bundle"])
        ids = pd.read_csv(bundle["files"]["input_cells.csv.gz"]["path"], dtype=str)
        found.extend(ids.cell_id)
        assert list(ids.source_cell_id) == list(ids.cell_id)
    assert found == ["001", "002", "003", "004"]


def test_annotation_evidence_and_refinement_are_immutable_versions(tmp_path, monkeypatch):
    from osp import annotate
    folder = tmp_path / "computed"
    folder.mkdir()
    obs = pd.DataFrame({"leiden_r1": pd.Categorical(["0"] * 6), "total_counts": [10.] * 6},
                       index=[f"00{i}" for i in range(6)])
    data = ad.AnnData(sparse.csr_matrix(np.ones((6, 3))), obs=obs,
                     var=pd.DataFrame(index=["CD3D", "MS4A1", "LYZ"]))
    data.write_h5ad(folder / "clustered.h5ad")
    (folder / "figures").mkdir()
    (folder / "figures/umap_clusters_leiden_r1.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMZkAAAAASUVORK5CYII="))
    for index in range(7):
        (folder / f"figures/qc_violin_{index}.png").write_bytes((folder / "figures/umap_clusters_leiden_r1.png").read_bytes())
    (folder / "de_top_genes_leiden_r1.csv").write_text("cluster,gene\n0,CD3D\n")
    bundle = sealed(folder, tmp_path / "bundle.json", sample="a")
    original = reference(folder / "clustered.h5ad")
    state = {"bundle": bundle, "data": original, "key": "leiden_r1", "version": 0,
             "seen": {"figures": [], "tables": [], "genes": False, "qc": False}}
    counter = 0

    def invoke(name, arguments):
        nonlocal state, counter
        dest = tmp_path / ("tool-" + str(counter))
        counter += 1
        dest.mkdir()
        save(dest / "before.json", state)
        save(dest / "arguments.json", arguments)
        tool(name, dest / "before.json", dest / "arguments.json", dest)
        result = read(dest / "result.json")
        state = verified(result["state"])
        return result

    assert "Read all required" in invoke("submit_annotation", {"proposal_json": "{}", "version": 0})["error"]
    figures = invoke("read_evidence", {"kind": "figures", "offset": 0})
    assert len(figures["images"]) == 8 and figures["next_offset"] is None
    assert figures["images"][0].startswith("data:image/png;base64,")
    invoke("read_evidence", {"kind": "tables", "offset": 0})
    invoke("check_genes", {"genes": ["CD3D"]})
    invoke("check_qc_scores", {})

    def invalid_refinement(data, key, cluster, resolution, new_key):
        data.obs[new_key] = ["singleton"] * len(data)
        raise ValueError("DEG cannot compare singleton groups")
    monkeypatch.setattr(annotate, "_subcluster_once", invalid_refinement)
    assert "singleton" in invoke("subcluster", {"cluster": "0", "resolution": 1.0})["error"]
    assert state["version"] == 0 and state["key"] == "leiden_r1"
    assert reference(folder / "clustered.h5ad") == original

    def refine(data, key, cluster, resolution, new_key):
        data.obs[new_key] = pd.Categorical(["0,0"] * 3 + ["0,1"] * 3)
        return 2, "two subclusters"
    monkeypatch.setattr(annotate, "_subcluster_once", refine)
    invoke("subcluster", {"cluster": "0", "resolution": 1.0})
    assert state["version"] == 1 and state["key"] == "ann_sub1"
    assert not state["seen"]["genes"] and not state["seen"]["qc"]
    assert reference(folder / "clustered.h5ad") == original
    assert "obsolete" in invoke("submit_annotation", {"proposal_json": "{}", "version": 0})["error"]
    invoke("check_genes", {"genes": ["CD3D"]})
    invoke("check_qc_scores", {})
    proposal = {"clusters": [{"cluster": c, "label_coarse": "T", "label_fine": "T", "confidence": "low",
                              "evidence_genes": ["CD3D"], "doubts": "test"} for c in ("0,0", "0,1")], "qc_actions": []}
    result = invoke("submit_annotation", {"proposal_json": json.dumps(proposal), "version": 1})
    assert result["accepted"] and result["proposal"]["cluster_key"] == "ann_sub1"

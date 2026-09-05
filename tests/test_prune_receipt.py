"""Release receipts remain valid across interrupted and repeated pruning."""
import json
from pathlib import Path
import subprocess
import sys

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from ecarsi import downstream as D
from ecarsi import layout as L
from ecarsi import loop, prune
from ecarsi.run_state import read_json


def write_h5(path, cells=("001", "NA")):
    path.parent.mkdir(parents=True, exist_ok=True)
    obj = ad.AnnData(np.ones((len(cells), 2)), obs=pd.DataFrame(index=list(cells)))
    obj.layers["counts"] = obj.X.copy()
    obj.write_h5ad(path)


@pytest.fixture
def released(tmp_path):
    unit = tmp_path / "unit"
    write_h5(L.input_h5ad(unit))
    sample = L.persample_root(unit) / "sample-hash"
    write_h5(sample / "clustered.h5ad")
    write_h5(sample / "subset.h5ad")
    (sample / "qc_removed.csv").write_text("cell,qc_reason\n")
    L.persample_manifest(unit).write_text(json.dumps({"samples": [{"value": "S1", "dir": str(sample)}]}))
    rdir = L.round_dir(unit, 1)
    write_h5(rdir / L.ROUND_INPUT)
    for step, names in [(L.CROSSSAMPLE, ["integrated.h5ad", "annotated.h5ad"]),
                        (L.ZOOMIN, ["annotated_zmip.h5ad"])]:
        for name in names:
            write_h5(rdir / step / name)
        (rdir / step / "report.html").write_text("<html>preserved report</html>")
    (L.crosssample_dir(rdir) / "annotation_removed.csv").write_text("cell,annotate_remove\n")
    (L.zoomin_dir(rdir) / "zmip_removed.csv").write_text("cell,annotate_remove,remove_reason\n")
    (rdir / L.STATS).write_text('{"n_in":2,"n_out":2}')
    (rdir / L.DECISION).write_text("release\n")
    release = L.release_dir(unit)
    write_h5(release / "final.h5ad")
    (release / "summary.md").write_text("Released 2 cells\n")
    (release / "summary.json").write_text('{"rounds":1}')
    (release / "cell_ledger.csv").write_text("cell,sample\n001,S1\nNA,S1\n")
    D.seal_release(unit, [rdir])
    D.check_release(unit)
    return unit, rdir


@pytest.mark.parametrize("entrypoint", ["loop", "prune"])
def test_interrupted_prune_resumes_after_marker_is_written(released, monkeypatch, entrypoint):
    unit, rdir = released
    targets = prune.targets(unit)
    expected = {str(p.relative_to(unit)): p.stat().st_size for p in targets}
    failed = targets[1]
    original_unlink = Path.unlink
    calls = []

    def fail_second_target(path, *args, **kwargs):
        if path == failed:
            calls.append(path)
            assert path.with_name(path.name + L.PRUNED_SUFFIX).is_file()
            raise OSError("simulated unlink interruption")
        return original_unlink(path, *args, **kwargs)

    main = loop.main if entrypoint == "loop" else prune.main
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_second_target)
        with pytest.raises(OSError, match="simulated unlink"):
            main([str(unit)])
    assert calls == [failed]
    assert not targets[0].exists()
    assert failed.exists()
    D.check_release(unit)  # the pre-prune publication receipt still checks out
    assert main([str(unit)]) == 0
    D.check_release(unit)
    assert not prune.targets(unit)
    summary = read_json(L.release_dir(unit) / "pruned.json")
    assert {rec["file"]: rec["bytes"] for rec in summary["removed"]} == expected
    assert summary["files"] == len(expected)
    assert summary["bytes"] == sum(expected.values())
    receipt = read_json(L.release_dir(unit) / ".rsi-release.json")["files"]
    for p in targets:
        marker = str(p.relative_to(unit)) + L.PRUNED_SUFFIX
        assert marker in receipt
        if p.name in prune.LABELLED:
            saved = read_json(unit / marker)
            sidecar = p.with_name(saved["obs"])
            assert sidecar.is_file()
            assert str(sidecar.relative_to(unit)) in receipt


def test_standalone_prune_takes_writer_lock_and_refreshes_receipt(released):
    unit, _ = released
    before = read_json(L.release_dir(unit) / ".rsi-release.json")
    command = [sys.executable, "-m", "ecarsi.prune", str(unit)]
    with D.unit_lock(unit):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert "another writer holds" in result.stderr
        assert read_json(L.release_dir(unit) / ".rsi-release.json") == before
        assert prune.targets(unit)
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    D.check_release(unit)
    after = read_json(L.release_dir(unit) / ".rsi-release.json")
    assert after != before
    assert any(name.endswith(L.PRUNED_SUFFIX) for name in after["files"])
    assert not prune.targets(unit)


def test_repeated_prune_preserves_cumulative_records(released):
    unit, _ = released
    assert prune.main([str(unit)]) == 0
    summary_path = L.release_dir(unit) / "pruned.json"
    first = read_json(summary_path)
    assert first["files"] > 0
    assert prune.main([str(unit)]) == 0
    second = read_json(summary_path)
    for key in ["removed", "files", "bytes"]:
        assert second[key] == first[key]
    D.check_release(unit)
    assert L.input_h5ad(unit).is_file()
    assert ad.read_h5ad(L.release_dir(unit) / "final.h5ad").obs_names.tolist() == ["001", "NA"]


def test_dry_run_does_not_change_release_or_delete_inputs(released):
    unit, _ = released
    before = (L.release_dir(unit) / ".rsi-release.json").read_bytes()
    targets = prune.targets(unit)
    assert prune.main([str(unit), "--dry-run"]) == 0
    assert all(p.is_file() for p in targets)
    assert (L.release_dir(unit) / ".rsi-release.json").read_bytes() == before
    assert not (L.release_dir(unit) / "pruned.json").exists()

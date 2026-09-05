"""Downstream ownership, exact matrix checks, and durable completion receipts."""
from contextlib import contextmanager
import multiprocessing
import subprocess
import sys

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from ecarsi import downstream as D
from ecarsi import layout as L
from ecarsi.run_state import file_identity, write_json


def _fork_lock_attempt(unit, connection):
    try:
        with D.unit_lock(unit):
            connection.send("acquired")
    except RuntimeError:
        connection.send("blocked")
    finally:
        connection.close()


def test_unit_lock_nested_and_released_after_exception(tmp_path):
    with pytest.raises(ValueError, match="stop"):
        with D.unit_lock(tmp_path):
            with D.unit_lock(tmp_path):
                raise ValueError("stop")
    with D.unit_lock(tmp_path):
        pass


def test_unit_lock_blocks_independent_process_until_released(tmp_path):
    code = "from pathlib import Path; from ecarsi.downstream import unit_lock; import sys\nwith unit_lock(Path(sys.argv[1])): print('acquired')"
    command = [sys.executable, "-c", code, str(tmp_path)]
    with D.unit_lock(tmp_path):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode != 0
        assert "another writer holds" in result.stderr
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "acquired"


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires POSIX fork")
def test_unit_lock_inherited_python_state_does_not_authorize_fork_child(tmp_path):
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    with D.unit_lock(tmp_path):
        child = context.Process(target=_fork_lock_attempt, args=(tmp_path, send))
        child.start()
        send.close()
        try:
            assert receive.poll(10), "child did not report lock result"
            assert receive.recv() == "blocked"
        finally:
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(5)
            receive.close()
        assert child.exitcode == 0


def write_matrix(path, values, ids, storage="dense"):
    counts = values if storage == "dense" else getattr(sparse, storage + "_matrix")(values)
    obj = ad.AnnData(counts.copy(), obs=pd.DataFrame(index=ids), var=pd.DataFrame(index=["g1", "g2", "g3"]))
    obj.layers["counts"] = counts
    obj.write_h5ad(path)


@contextmanager
def matrices(*paths):
    objects = []
    try:
        objects = [D._Matrix(path) for path in paths]
        yield objects
    finally:
        for obj in objects:
            obj.file.close()


@pytest.mark.parametrize("parent_storage,child_storage", [("dense", "dense"), ("csr", "dense"), ("dense", "csr"), ("csc", "csr")])
def test_matrix_reordered_noninteger_counts_across_multiple_chunks(tmp_path, parent_storage, child_storage):
    # More than 2048 cells exercises the chunk boundary; fractions must not be rounded.
    values = (np.arange(4101 * 3, dtype=np.float64).reshape(-1, 3) % 19) / 8
    values[::7] = 0
    ids = np.array([f"cell-{i}" for i in range(len(values))])
    order = np.random.default_rng(3).permutation(len(values))
    parent, child = tmp_path / "parent.h5ad", tmp_path / "child.h5ad"
    write_matrix(parent, values, ids, parent_storage)
    write_matrix(child, values[order], ids[order], child_storage)
    with matrices(parent, child) as (a, b):
        D._same_counts(a, b)
        block = a.rows(np.array([2050, 0, 4099, 1]))
        if sparse.issparse(block):
            block = block.toarray()
        np.testing.assert_array_equal(block, values[[2050, 0, 4099, 1]])


@pytest.mark.parametrize("storage", ["dense", "csr"])
def test_matrix_tiny_fractional_count_change_is_rejected(tmp_path, storage):
    values = np.array([[0.125, 0, 2.25], [3.5, 0.25, 1.125]])
    changed = values.copy()
    changed[1, 2] += 2 ** -30
    parent, child = tmp_path / "parent.h5ad", tmp_path / "child.h5ad"
    write_matrix(parent, values, ["001", "NA"], storage)
    write_matrix(child, changed, ["001", "NA"], storage)
    with matrices(parent, child) as (a, b):
        with pytest.raises(ValueError, match="raw counts changed"):
            D._same_counts(a, b)


def test_same_counts_mask_selects_source_cells_in_combined_output(tmp_path):
    parent, child = tmp_path / "parent.h5ad", tmp_path / "child.h5ad"
    values = np.array([[0.125, 2, 3], [4, 5, 0.25]])
    write_matrix(parent, values, ["a", "b"], "csr")
    write_matrix(child, np.vstack([values[1], [90, 91, 92], values[0]]), ["b", "other", "a"])
    with matrices(parent, child) as (a, b):
        D._same_counts(a, b, b.obs_names.isin(a.obs_names))
        with pytest.raises(ValueError, match="invented cell IDs"):
            D._same_counts(a, b)


@pytest.fixture
def stable_runtime(monkeypatch):
    monkeypatch.setattr(D, "kernel_runtime", lambda py, kernel: {"kernel": kernel, "source": "fixed"})
    for kernel in ("MSP", "ZMIP"):
        for key in ("N_TOP_GENES", "N_PCS", "N_NEIGHBORS", "LANGUAGE", "EFFORT", "MAX_TURNS", "RESOLUTIONS", "HARMONY", "MIN_CELLS"):
            monkeypatch.delenv(f"{kernel}_{key}", raising=False)


def complete_stage(stage, input_path, kernel):
    stage.mkdir(parents=True, exist_ok=True)
    identity = D.prepare(sys.executable, kernel, [input_path], stage, {"options": D.options(kernel)})
    report = stage / "report.html"
    report.write_text("<html>verified evidence</html>")
    write_json(stage / D.STATE, {"identity": identity, "state": "complete", "validation": {
        "outputs": {"report.html": file_identity(report)}}})
    return identity


def test_prepare_refuses_modified_completed_output_without_rewriting_receipt(tmp_path, stable_runtime):
    input_path = tmp_path / "input.h5ad"
    input_path.write_bytes(b"input")
    stage = tmp_path / "crosssample"
    complete_stage(stage, input_path, "msp")
    before = (stage / D.STATE).read_bytes()
    (stage / "report.html").write_text("<html>modified evidence</html>")
    with pytest.raises(ValueError, match="completed output changed"):
        D.prepare(sys.executable, "msp", [input_path], stage, {"options": []})
    assert (stage / D.STATE).read_bytes() == before


@pytest.fixture
def sealed_round(tmp_path, stable_runtime):
    unit = tmp_path / "unit"
    rdir = L.round_dir(unit, 1)
    rdir.mkdir(parents=True)
    source = unit / "source.h5ad"
    source.write_bytes(b"original input")
    for kernel, stage in [("msp", L.crosssample_dir(rdir)), ("zmip", L.zoomin_dir(rdir))]:
        complete_stage(stage, source, kernel)
    (rdir / L.STATS).write_text('{"n_in": 2, "n_out": 1}')
    (rdir / L.DECISION).write_text("release")
    D.seal_round(rdir)
    D.check_round(rdir)
    return unit, rdir, source


def test_check_round_detects_source_input_change(sealed_round):
    _, rdir, source = sealed_round
    source.write_bytes(b"changed input")
    with pytest.raises(ValueError, match="input changed"):
        D.check_round(rdir)


@pytest.mark.parametrize("kernel", ["MSP", "ZMIP"])
def test_check_round_detects_effective_configuration_change(sealed_round, monkeypatch, kernel):
    _, rdir, _ = sealed_round
    monkeypatch.setenv(f"{kernel}_N_PCS", "17")
    with pytest.raises(ValueError, match="runtime/config changed"):
        D.check_round(rdir)


@pytest.mark.parametrize("relative", ["crosssample/.msp-state/integrate.pending", "crosssample/.msp-state/inspect.pending", "crosssample/.msp-state/annotate.pending", "zoomin/.zmip-publish.json"])
def test_check_round_rejects_unfinished_kernel_marker(sealed_round, relative):
    _, rdir, _ = sealed_round
    marker = rdir / relative
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    with pytest.raises(ValueError, match="pending"):
        D.check_round(rdir)


@pytest.mark.parametrize("target", ["release/summary.md", "release/final.h5ad", "rounds/round01/stats.txt", "rounds/round01/crosssample/annotated.h5ad.obs.csv.gz"])
def test_release_receipt_rejects_changed_retained_evidence(sealed_round, target):
    unit, rdir, _ = sealed_round
    release = L.release_dir(unit)
    release.mkdir()
    (release / "summary.md").write_text("original summary")
    (release / "final.h5ad").write_bytes(b"verified final matrix")
    sidecar = L.crosssample_dir(rdir) / "annotated.h5ad.obs.csv.gz"
    sidecar.write_bytes(b"retained obs evidence")
    D.seal_release(unit, [rdir])
    D.check_release(unit)
    path = unit / target
    assert path.is_file()
    path.write_bytes(path.read_bytes() + b"altered")
    with pytest.raises(ValueError, match="completed output changed"):
        D.check_release(unit)


def test_release_without_receipt_is_not_verified(tmp_path):
    with pytest.raises(ValueError, match="legacy release"):
        D.check_release(tmp_path)

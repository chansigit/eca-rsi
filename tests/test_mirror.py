"""--mirror DIR: light copies after every landing page, the whole unit at release, never a failed step."""

import json
import os
import time
from pathlib import Path

import pytest

from ecarsi import layout as L
from ecarsi import mirror
from ecarsi.index import render_root, render_unit, write_all

BIG = b"0" * (mirror.LIGHT_CSV_MAX + 1)


def make_root(tmp_path):
    root, unit = tmp_path / "root", L.unit_dir(tmp_path / "root", "u1")
    L.organize_manifest(root).parent.mkdir(parents=True)
    L.organize_manifest(root).write_text(
        json.dumps(
            {"state": "complete", "input_units": [], "plan": {"analysis_units": []}}
        )
    )
    L.input_h5ad(unit).parent.mkdir(parents=True)
    L.input_h5ad(unit).write_bytes(b"h5ad" * 100)
    L.input_manifest(unit).write_text(json.dumps({"n_cells": 3, "species": "human"}))
    (unit / L.PROGRESS).write_text("2026-09-06 10:00:00 organize: 3 cells\n")
    s = L.persample_root(unit) / "S1"
    (s / "figures").mkdir(parents=True)
    (s / "figures" / "umap.png").write_bytes(b"png")
    (s / "report.html").write_text("<html>")
    (s / "clustered.h5ad").write_bytes(b"x" * 10)
    (s / "qc_summary.csv").write_text("a,b\n")
    (s / ".cache").mkdir()
    (s / ".cache" / "deg.json").write_text("{}")
    (s / ".driver.lock").write_text("")
    c = L.crosssample_dir(L.round_dir(unit, 1))
    c.mkdir(parents=True)
    (c / "integrated.h5ad").write_bytes(b"y" * 10)
    (c / "de_parent_core_vs_core.csv").write_bytes(BIG)
    (c / "annotated.h5ad.obs.parquet").write_bytes(b"pq")
    (c.parent / L.STATS).write_text(
        '{"n_in": 3, "n_out": 3, "removed": 0, "frac": 0.0, "decision": "continue"}'
    )
    (c.parent / L.DECISION).write_text("continue\n")
    return root, unit


def files(d: Path) -> set[str]:
    return {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()}


def test_light_set():
    yes = [
        "index.html",
        "units/u/progress.log",
        "organize/manifest.json",
        "units/u/rounds/round01/stats.txt",
        "units/u/rounds/round01/decision.txt",
        "units/u/release/summary.md",
        "units/u/release/umap.json",
        "units/u/release/sankey_coarse.png",
        "units/u/persample/S1/report.html",
        "units/u/persample/S1/figures/umap.png",
        "units/u/persample/S1/figures/paga.svg",
        "units/u/persample/S1/clustered.h5ad.pruned",
        "mirror.json",
    ]
    no = [
        "units/u/input/organized.h5ad",
        "units/u/release/final.h5ad",
        "units/u/rounds/round01/crosssample/annotated.h5ad.obs.parquet",
        "units/u/rounds/round01/crosssample/annotated.h5ad.obs.csv.gz",
        "units/u/persample/sample_mapping.csv.gz",
        "units/u/persample/S1/.cache/deg.json",
        "units/u/rounds/round01/.rsi-stage.json",
        "units/u/.rsi-downstream.lock",
        "organize/manifest.json.123.tmp",
    ]
    assert all(mirror.is_light(Path(p), 10) for p in yes)
    assert not any(mirror.is_light(Path(p), 10) for p in no)
    assert mirror.is_light(
        Path("units/u/persample/S1/qc_summary.csv"), mirror.LIGHT_CSV_MAX
    )
    assert not mirror.is_light(
        Path("units/u/release/cell_ledger.csv"), mirror.LIGHT_CSV_MAX + 1
    )


def test_configure_rejects_dir_inside_root(tmp_path):
    root, _ = make_root(tmp_path)
    with pytest.raises(SystemExit):
        mirror.configure(root, root / "copy")
    with pytest.raises(SystemExit):
        mirror.configure(root, root)
    assert mirror.sync(root) is None  # nothing configured: silent no-op


def test_light_sync_is_incremental(tmp_path):
    root, unit = make_root(tmp_path)
    dest = mirror.configure(root, tmp_path / "oak")
    r = mirror.sync(unit)
    assert files(dest) == {
        "mirror.json",
        "organize/manifest.json",
        "units/u1/input/manifest.json",
        "units/u1/progress.log",
        "units/u1/persample/S1/figures/umap.png",
        "units/u1/persample/S1/report.html",
        "units/u1/persample/S1/qc_summary.csv",
        "units/u1/rounds/round01/stats.txt",
        "units/u1/rounds/round01/decision.txt",
    }
    assert r["copied"] == len(files(dest)) and r["removed"] == 0
    log = dest / "units/u1" / L.PROGRESS
    assert (
        log.stat().st_mtime_ns == (unit / L.PROGRESS).stat().st_mtime_ns
    )  # copies keep the source mtime
    before = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
    assert mirror.sync(unit)["copied"] == 0  # nothing changed: nothing rewritten
    assert {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()} == before
    time.sleep(0.01)
    with open(unit / L.PROGRESS, "a") as f:
        f.write("2026-09-06 10:01:00 persample complete\n")
    assert mirror.sync(unit)["copied"] == 1
    assert log.read_text() == (unit / L.PROGRESS).read_text()
    assert not list(dest.rglob("*" + mirror.TMP_SUFFIX))


def test_release_full_sync_then_scoped_removal(tmp_path):
    root, unit = make_root(tmp_path)
    dest = mirror.configure(root, tmp_path / "oak")
    mirror.sync(unit)
    assert "units/u1/input/organized.h5ad" not in files(dest)
    r = mirror.sync(unit, full=True)
    assert r["removed"] == 0
    assert files(dest) == files(root) - {
        "units/u1/persample/S1/.cache/deg.json",
        "units/u1/persample/S1/.driver.lock",
    }
    # prune happens in the root; strays elsewhere in DIR are not ours to delete
    h5 = unit / "persample/S1/clustered.h5ad"
    h5.unlink()
    h5.with_name(h5.name + L.PRUNED_SUFFIX).write_text("{}")
    (dest / "notes.txt").write_text("someone else's file")
    (dest / "units/u2").mkdir()
    (dest / "units/u2/index.html").write_text("another unit's copy")
    (dest / "units/u1/rounds/round01/x.tmp-mirror").write_text("crashed copy")
    r = mirror.sync(unit, full=True)
    assert r["removed"] == 2
    assert "units/u1/persample/S1/clustered.h5ad" not in files(dest)
    assert "units/u1/persample/S1/clustered.h5ad.pruned" in files(dest)
    assert "units/u1/rounds/round01/x.tmp-mirror" not in files(dest)
    assert (dest / "notes.txt").is_file() and (dest / "units/u2/index.html").is_file()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unwritable_mirror_warns_and_the_step_goes_on(tmp_path, capsys):
    root, unit = make_root(tmp_path)
    ro = tmp_path / "ro"
    ro.mkdir()
    mirror.configure(root, ro / "oak")
    ro.chmod(0o500)
    try:
        written = write_all(unit)
    finally:
        ro.chmod(0o700)
    assert [p.name for p in written] == [L.INDEX, L.INDEX] and all(
        p.is_file() for p in written
    )
    assert "[mirror] WARNING" in capsys.readouterr().out
    assert any("mirror warning" in ev for _, ev in L.read_log(unit))
    assert not (ro / "oak").exists()


def test_write_all_mirrors_and_pages_carry_the_state_stamp(tmp_path):
    root, unit = make_root(tmp_path)
    dest = mirror.configure(root, tmp_path / "oak")
    write_all(unit)
    copy = dest / L.UNITS / "u1"
    assert (dest / L.INDEX).is_file() and (copy / L.INDEX).is_file()
    fmt = "%Y-%m-%d %H:%M:%S"
    stamp = time.strftime(fmt, time.localtime((unit / L.PROGRESS).stat().st_mtime))
    assert f"run state updated {stamp}" in (unit / L.INDEX).read_text()
    assert f"run state updated {stamp}" in (copy / L.INDEX).read_text()
    # served from the copy (ecarsi.serve renders from disk): same stamp, and it says it is a copy
    live = render_unit(copy)
    assert f"run state updated {stamp}" in live and f"a mirror copy of {root}" in live
    assert f"a mirror copy of {root}" in render_root(dest)
    assert "from the run directory" in render_unit(
        unit
    ) and "mirror copy" not in render_unit(unit)
    # a newer event moves the stamp on both sides after the next page write
    time.sleep(1.1)
    L.log_event(unit, "round 1 start", echo=False)
    write_all(unit)
    newer = time.strftime(fmt, time.localtime((unit / L.PROGRESS).stat().st_mtime))
    assert newer != stamp and f"run state updated {newer}" in render_unit(copy)

"""#25: a run keeps its display zone (what Periscope shows) apart from its work tree, and archives the latter."""
import asyncio
import json
import os
import re
import shutil
import tarfile
from pathlib import Path

import pytest

from ecarsi import display
from ecarsi.stages import archive
from ecarsi.ui import index
from .test_gen2_pages import gen2_run

STORAGE = {"display_root": "/oak/eca/display", "archive_root": "/oak/eca/work"}


def test_zone_places_a_run_by_its_input_and_names_it_by_its_directory(tmp_path):
    spec = dict(storage=STORAGE, run_id="r1", output_root="/scratch/runs/b/3ca-Durante",
                input_root="/oak/sc/3ca/eca-pp/Durante/standardize")
    z = display.zone(spec)
    assert z["dest"] == "/oak/eca/display/3ca/Durante/r1"
    assert z["record"] == dict(name="3ca-Durante", collection="3ca", dataset="Durante", run="r1",
                               source="/scratch/runs/b/3ca-Durante", work="/oak/eca/work/3ca/Durante/r1.tar.gz")
    assert display.zone({**spec, "input_root": "/elsewhere/x"})["record"]["collection"] == "other"
    assert display.zone({k: v for k, v in spec.items() if k != "storage"}) is None


def test_sync_copies_only_what_the_pages_need_and_only_what_changed(tmp_path):
    root, dest = gen2_run(tmp_path / "run"), tmp_path / "display" / "c" / "d" / "r"
    (root / "units" / "u" / "rounds" / "round01" / "turn-3.continuation.json").write_text("{}")  # a session record
    record = dict(name="run", collection="c", dataset="d", run="r", source=str(root), work="/w.tar.zst")
    first = display.sync(root, dest, record)
    copied = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file()}
    assert "display.json" in copied and "units/u/release/receipt.json" in copied
    assert not any("continuation" in p for p in copied) and first["copied"] == first["files"] == len(copied) - 1
    assert json.loads((dest / "display.json").read_text())["name"] == "run"
    # the copy renders as the run does (paths and the render time aside)
    norm = lambda html, r: re.sub(r"rendered .*? by ecarsi\.index", "", html.replace(str(r), "<ROOT>"))
    assert norm(index.render_unit(dest / "units" / "u", "run"), dest) == norm(index.render_unit(root / "units" / "u", "run"), root)
    assert display.sync(root, dest, record)["copied"] == 0
    receipt = root / "units" / "u" / "release" / "receipt.json"
    receipt.write_text(json.dumps({"state": "complete", "changed": True}))
    os.utime(receipt, (receipt.stat().st_atime, receipt.stat().st_mtime + 10))
    assert display.sync(root, dest, record)["copied"] == 1


def test_broken_links_names_what_a_zone_lacks(tmp_path):
    """#41: the gate fails on a link of the display zone that reaches nothing."""
    root, dest = gen2_run(tmp_path / "run"), tmp_path / "display" / "c" / "d" / "r"
    record = dict(name="run", collection="c", dataset="d", run="r", source=str(root), work="/w.tar.zst")
    display.sync(root, dest, record)
    assert display.broken_links(dest, "run") == {}
    report = root / "units" / "u" / "rounds" / "round01" / "02-cross-sample" / "report.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('<a href="removed.csv">removals</a> <a href="/scratch/pool/requests/r1/outputs/x.json">x</a>')
    display.sync(root, dest, record)
    assert display.broken_links(dest, "run") == {
        "units/u/rounds/round01/02-cross-sample/removed.csv": "units/u/rounds/round01/02-cross-sample/report.html",
        "/scratch/pool/requests/r1/outputs/x.json": "units/u/rounds/round01/02-cross-sample/report.html"}


def test_a_unit_page_links_from_its_unit(tmp_path):
    """With two units the run page lists them instead of showing one inline, so a report reached only from a unit
    page must be read from that unit: the files it links to were left out of the zone."""
    root = gen2_run(tmp_path / "run")
    shutil.copytree(root / "units" / "u", root / "units" / "v")
    report = root / "units" / "v" / "rounds" / "round01" / "02-cross-sample" / "report.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text('<a href="table.csv">table</a>')
    (report.parent / "table.csv").write_text("a,b\n")
    assert Path("units/v/rounds/round01/02-cross-sample/table.csv") in display.closure(root, "run")


def test_overlapping_syncs_of_one_zone_do_not_trip_over_each_other(tmp_path, monkeypatch):
    """2026-10-02: four syncs of test1002-shi began together and the final one failed."""
    root, dest = gen2_run(tmp_path / "run"), tmp_path / "display" / "c" / "d" / "r"
    record = dict(name="run", collection="c", dataset="d", run="r", source=str(root), work="/w.tar.gz")
    real = shutil.copy2
    def copy_then_overlap(source, target):
        real(source, target)
        monkeypatch.setattr(shutil, "copy2", real)
        display.sync(root, dest, record)  # another sync runs between this copy and its rename
    monkeypatch.setattr(shutil, "copy2", copy_then_overlap)
    display.sync(root, dest, record)
    assert (dest / "display.json").is_file() and not list(dest.rglob("*.partial"))


def test_archive_round_trips_and_keeps_an_existing_one(tmp_path):
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "x.json").write_text("x" * 1000)
    (src / "empty.lock").write_text("")
    os.link(src / "a" / "x.json", src / "hard.json")
    (src / "sym").symlink_to("a/x.json")
    out = tmp_path / "w" / "run.tar.gz"
    assert archive.pack(src, out)["state"] == "packed" and archive.listing(out) == archive.inventory(src)
    with tarfile.open(out) as t:
        t.extractall(tmp_path / "back", filter="tar")
    assert (tmp_path / "back" / "hard.json").read_text() == "x" * 1000 and os.readlink(tmp_path / "back" / "sym") == "a/x.json"
    assert archive.pack(src, out)["state"] == "exists" and not list(out.parent.glob("*.partial"))


def unit_run(spec, fail_display=False):
    """One analysis unit on the Temporal test server; returns its result, the display syncs it submitted
    and the Pool requests it waited for."""
    from .test_dataset_workflow import run_unit, unit_step
    from tests.temporal_env import fakes
    actions, syncs, awaited = [], [], []
    def display_(args):
        syncs.append((args[1], args[2]))
        if fail_display:
            raise RuntimeError("pool unreachable")
        return {"id": "sync-" + args[1], "output": "synced.json"}
    def pool(pool_root, request_id, output):
        awaited.append(request_id)
        return {"state": "ready", "path": "result.json"}
    activities = fakes(dataset_step=unit_step(actions, {"display": display_}), check_pool=pool)
    out = asyncio.run(run_unit(activities, spec, {"name": "u"}, None))
    return out, syncs, awaited


def test_every_stage_of_a_unit_syncs_its_display_zone_without_waiting_for_it():
    spec = {"storage": STORAGE, "pool_root": "pool", "zoom_in": {"merge_budget": {"timeout_seconds": 60}}}
    out, syncs, awaited = unit_run(spec)
    assert out == "unit.json" and [label for label, _ in syncs] == ["u/per-sample", "u/round01/cross-sample",
                                                                   "u/round01/zoom-in", "u/round01/decided", "u/release"]
    assert not any(final for _, final in syncs) and not any(r.startswith("sync-") for r in awaited)  # never awaited


def test_a_failing_sync_never_fails_the_run_and_no_storage_means_no_sync():
    spec = {"storage": STORAGE, "pool_root": "pool", "zoom_in": {"merge_budget": {"timeout_seconds": 60}}}
    assert unit_run(spec, fail_display=True)[0] == "unit.json"
    out, syncs, _ = unit_run({k: v for k, v in spec.items() if k != "storage"})
    assert out == "unit.json" and not syncs


def test_storage_is_optional_but_must_name_two_absolute_roots():
    from ecarsi.control.dataset import validate_spec
    required = dict.fromkeys(["run_id", "dataset_id", "input_root", "output_root", "pool_root", "bridge_root",
                              "organize", "per_sample", "cross_sample", "zoom_in", "round_policy"])
    with pytest.raises(ValueError, match="storage needs"):
        validate_spec({**required, "storage": {"display_root": "rel", "archive_root": "/a"}})
    with pytest.raises(ValueError, match="explicit services"):
        validate_spec({**required, "storage": STORAGE, "extra": 1})
    # the optional stress policy (decision 0017) passes the key check and must be remove or keep
    with pytest.raises(ValueError, match="stress_policy is remove or keep"):
        validate_spec({**required, "stress_policy": "drop"})
    with pytest.raises(ValueError, match="storage needs"):
        validate_spec({**required, "stress_policy": "keep", "storage": {"display_root": "rel", "archive_root": "/a"}})


def test_a_display_sync_rides_the_tool_priority_class():
    from ecarsi.warm_pool.backend import request_class
    assert request_class({"operation_id": "dataset.display", "request_id": "run.display-0123"}) == "tool"
    assert request_class({"operation_id": "dataset.release", "request_id": "run.release-0123"}) == "work"


def test_the_display_activity_writes_its_packet_and_submits_a_tool_class_request(tmp_path):
    """The activity itself, not a stand-in: its first call in a run must create display-sync/ (it did not, 2026-10-02)."""
    import ecarsi.control.dataset as module
    from ecarsi.files import read, save
    pool = tmp_path / "pool"
    (pool / "requests").mkdir(parents=True)
    pool.chmod(0o700)
    save(pool / "config.json", {"runtime": {}})
    out = tmp_path / "runs" / "11_Shietal"
    out.mkdir(parents=True)
    spec = dict(storage=STORAGE, run_id="t1", dataset_id="T", output_root=str(out), pool_root=str(pool),
                input_root="/oak/sc/chondroatlas/eca-pp/11_Shietal/standardize")
    result = module.dataset_step("display", [spec, "organize", False])
    folder, = (pool / "requests").iterdir()
    request = read(folder / "request.json")["spec"]
    assert result == {"id": request["request_id"], "output": "synced.json"} and ".display-" in request["request_id"]
    packet = json.loads(Path(request["inputs"][0]["path"]).read_text())
    assert packet["dest"] == "/oak/eca/display/chondroatlas/11_Shietal/t1" and packet["root"] == str(out) and not packet["final"]
    assert request["cpus"] == 1 and module.dataset_step("display", [spec, "published", True])["id"] != result["id"]

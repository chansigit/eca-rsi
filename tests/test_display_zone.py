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


def unit_run(monkeypatch, spec, fail_display=False):
    import ecarsi.control.dataset as module
    actions, awaited = [], []
    async def call(fn, action, args):
        if action == "display":
            actions.append(("display", args[1], args[2]))
            if fail_display:
                raise RuntimeError("pool unreachable")
            return {"id": "sync-" + args[1], "output": "synced.json"}
        actions.append(action)
        return {"round": {"publication": "unit.json"}, "release": {"id": "release", "output": "r.json"},
                "round-ledger": {"id": "ledger", "output": "l.json"}, "stage": {"run_id": "s"}}.get(action)
    async def pool(spec, request):
        awaited.append(request["id"])
        return "result.json"
    async def child(*args, **kwargs):
        return "stage.json"
    monkeypatch.setattr(module, "call", call)
    monkeypatch.setattr(module, "await_pool", pool)
    monkeypatch.setattr(module.workflow, "execute_child_workflow", child)
    monkeypatch.setattr(module.workflow, "patched", lambda name: True)
    out = asyncio.run(module.AnalysisUnitWorkflow().run(spec, {"name": "u"}, None))
    return out, actions, awaited


def test_every_stage_of_a_unit_syncs_its_display_zone_without_waiting_for_it(monkeypatch):
    spec = {"storage": STORAGE, "zoom_in": {"merge_budget": {"timeout_seconds": 60}}}
    out, actions, awaited = unit_run(monkeypatch, spec)
    syncs = [a[1] for a in actions if a[0] == "display"]
    assert out == "unit.json" and syncs == ["u/per-sample", "u/round01/cross-sample", "u/round01/zoom-in",
                                             "u/round01/decided", "u/release"]
    assert not any(r.startswith("sync-") for r in awaited)  # submitted, never awaited


def test_a_failing_sync_never_fails_the_run_and_no_storage_means_no_sync(monkeypatch):
    spec = {"storage": STORAGE, "zoom_in": {"merge_budget": {"timeout_seconds": 60}}}
    assert unit_run(monkeypatch, spec, fail_display=True)[0] == "unit.json"
    out, actions, _ = unit_run(monkeypatch, {"zoom_in": spec["zoom_in"]})
    assert out == "unit.json" and not any(isinstance(a, tuple) for a in actions)


def test_storage_is_optional_but_must_name_two_absolute_roots():
    from ecarsi.control.dataset import validate_spec
    required = dict.fromkeys(["run_id", "dataset_id", "input_root", "output_root", "pool_root", "bridge_root",
                              "organize", "per_sample", "cross_sample", "zoom_in", "round_policy"])
    with pytest.raises(ValueError, match="storage needs"):
        validate_spec({**required, "storage": {"display_root": "rel", "archive_root": "/a"}})
    with pytest.raises(ValueError, match="explicit services"):
        validate_spec({**required, "storage": STORAGE, "extra": 1})


def test_a_display_sync_rides_the_tool_priority_class():
    from ecarsi.warm_pool.backend import request_class
    assert request_class({"operation_id": "dataset.display", "request_id": "run.display-0123"}) == "tool"
    assert request_class({"operation_id": "dataset.release", "request_id": "run.release-0123"}) == "work"


def test_the_display_activity_writes_its_packet_and_submits_a_tool_class_request(tmp_path, monkeypatch):
    """The activity itself, not a stand-in: its first call in a run must create display-sync/ (it did not, 2026-10-02)."""
    import ecarsi.control.dataset as module
    import ecarsi.warm_pool.state as state
    submitted = []
    monkeypatch.setattr(state, "submit", lambda root, request: submitted.append((root, request)))
    out = tmp_path / "runs" / "11_Shietal"
    out.mkdir(parents=True)
    spec = dict(storage=STORAGE, run_id="t1", dataset_id="T", output_root=str(out), pool_root=str(tmp_path / "pool"),
                input_root="/oak/sc/chondroatlas/eca-pp/11_Shietal/standardize")
    result = module.dataset_step("display", [spec, "organize", False])
    (root, request), = submitted
    assert result == {"id": request["request_id"], "output": "synced.json"} and ".display-" in request["request_id"]
    packet = json.loads(Path(request["inputs"][0]["path"]).read_text())
    assert packet["dest"] == "/oak/eca/display/chondroatlas/11_Shietal/t1" and packet["root"] == str(out) and not packet["final"]
    assert request["cpus"] == 1 and module.dataset_step("display", [spec, "published", True])["id"] != result["id"]

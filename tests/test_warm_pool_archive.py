"""Archiving a published run's requests, and the settled set that survives a restart.

The pool is three things at once: a queue, a replay cache, and the store the publications
reference by absolute path. Only the first two are disposable, and only once a run is done.
"""
import json
import os
from pathlib import Path

import pytest

from ecarsi.warm_pool.state import archive


def request(root, name, run, state="succeeded"):
    folder = root / "requests" / name
    attempt = folder / "a1"
    (attempt / "outputs").mkdir(parents=True)
    (folder / "request.json").write_text(json.dumps(dict(
        attempt_id="a1", submitted_at=0.0,
        spec=dict(request_id=name, operation_id="op", trace=dict(workflow_id="dataset/" + run)))))
    (attempt / "accepted.json").write_text(json.dumps(dict(started_at=0.0)))
    if state:
        (attempt / "receipt.json").write_text(json.dumps(dict(state=state, outputs=[])))
    return folder


def pool(tmp_path):
    root = tmp_path / "pool"
    (root / "requests").mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    (root / "config.json").write_text("{}")
    return root


def test_only_the_named_run_moves(tmp_path):
    root = pool(tmp_path)
    mine = request(root, "a-1.prepare-x", "swahn")
    theirs = request(root, "b-1.prepare-y", "yan")
    result = archive(root, ["swahn"])
    assert result["requests"] == ["a-1.prepare-x"] and result["retained"] == 1
    assert not mine.exists() and theirs.exists()
    moved = Path(result["destination"]) / "requests" / "a-1.prepare-x"
    assert (moved / "a1" / "outputs").is_dir(), "the artefacts move with the request, not away"
    assert json.loads((Path(result["destination"]) / "manifest.json").read_text())["runs"] == ["swahn"]


def test_a_live_request_stops_the_whole_archive(tmp_path):
    root = pool(tmp_path)
    done = request(root, "a-1.prepare-x", "swahn")
    request(root, "a-2.deg-y", "swahn", state=None)          # accepted, no receipt: still running
    with pytest.raises(ValueError, match="still live"):
        archive(root, ["swahn"])
    assert done.exists(), "nothing moves unless the whole run is settled"


def test_dry_run_moves_nothing(tmp_path):
    root = pool(tmp_path)
    folder = request(root, "a-1.prepare-x", "swahn")
    result = archive(root, ["swahn"], dry_run=True)
    assert result["requests"] == ["a-1.prepare-x"] and folder.exists()
    assert not (root.parent / "archived-requests").exists()


def test_naming_no_run_is_refused(tmp_path):
    with pytest.raises(ValueError, match="never guesses"):
        archive(pool(tmp_path), [])


def test_archived_requests_leave_the_settled_set(tmp_path):
    root = pool(tmp_path)
    request(root, "a-1.prepare-x", "swahn")
    (root / "settled.json").write_text(json.dumps([["a-1.prepare-x", 12345], ["b-1.prepare-y", 678]]))
    archive(root, ["swahn"])
    assert json.loads((root / "settled.json").read_text()) == [["b-1.prepare-y", 678]]


def test_settled_requests_survive_a_scheduler_restart(tmp_path, monkeypatch):
    """The set is a durable fact -- a receipt cannot be taken back -- so a fresh scheduler
    should not have to stat every folder in the pool before it can dispatch. The 2026-09-20
    handover paid 33 minutes and 1061 s of task delay for exactly that."""
    from ecarsi.warm_pool import backend
    root = pool(tmp_path)
    request(root, "a-1.prepare-x", "swahn")
    from ecarsi.warm_pool import state as pool_state
    plain = pool_state.read
    monkeypatch.setattr(backend, "read", lambda path, default=None: (
        {"hq": "hq", "executor": "py"} if Path(path).name == "config.json" else plain(path, default)))
    queue = backend.HyperQueue(root)
    assert queue.settled == set()
    queue.settled.add(("a-1.prepare-x", 12345))
    queue._persist_settled()
    assert backend.HyperQueue(root).settled == {("a-1.prepare-x", 12345)}

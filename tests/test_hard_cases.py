"""Decision 0021 (#51): the published sync freezes the evidence of hard agent steps before the pruner deletes it."""
import json
import os
import subprocess
import sys
from pathlib import Path

from ecarsi.files import reference, save
from ecarsi.stages import cases
from .test_gen2_pages import gen2_run


def evidence(pool, name):
    """state -> bundle -> files, and a JSON file among them whose own reference sits one hop past EVIDENCE_DEPTH."""
    folder = pool / "requests" / name
    folder.mkdir(parents=True)
    (folder / "data.bin").write_bytes(name.encode() * 1000)  # distinct per session: equal content is stored once
    (folder / "deep.bin").write_bytes(b"d" * 10)
    save(folder / "deeper.json", {"far": reference(folder / "deep.bin")})
    save(folder / "nested.json", {"next": reference(folder / "deeper.json")})
    save(folder / "bundle.json", {"files": {"data": reference(folder / "data.bin"), "nested": reference(folder / "nested.json")}})
    save(folder / "state.json", {"evidence": reference(folder / "bundle.json")})
    return reference(folder / "state.json")


def session(run, path, state, rejected=0, accepted=1, extra=()):
    folder = run / path
    folder.mkdir(parents=True)
    save(folder / "session.json", {"spec": {"session_id": folder.name, "tool_state": state}, "model": {"model": "m"}})
    (folder / "session.lock").write_text("")
    results = []
    for i, error in enumerate([True] * rejected + [False] * accepted):
        save(folder / f"out-{i}.json", {"is_error": error})
        results.append({"name": "submit_quality", "output": reference(folder / f"out-{i}.json")})
    save(folder / f"{folder.name}.turn-0.continuation.json", {"results": results})
    for name in extra:
        save(folder / name, {"reason": "test"})
    return folder


def run_with_sessions(tmp_path):
    run, pool = tmp_path / "run", tmp_path / "pool"
    rounds = "units/u/rounds/round01"
    session(run, f"{rounds}/03-zoom-in/zoom-a", evidence(pool, "a"), rejected=2)
    session(run, f"{rounds}/03-zoom-in/zoom-b", evidence(pool, "b"), rejected=1)
    restarted = session(run, f"{rounds}/02-cross-sample/type-c", evidence(pool, "c"), extra=["restart.json"])
    session(restarted, "restart", evidence(pool, "c2"))  # the -r2 session, inside the one it replaces
    session(run, "units/u/01-per-sample/agent-d", evidence(pool, "d"), extra=["context-reset-1.json"])
    return run, pool


def test_hard_sessions_are_the_restarted_the_reset_and_the_repeatedly_rejected(tmp_path):
    run, _ = run_with_sessions(tmp_path)
    found = [(c["session"].rsplit("/", 1)[1], c["why"]) for c in cases.hard(run)]
    assert found == [("type-c", dict(rejected=0, restarted=True, context_resets=0)),
                     ("agent-d", dict(rejected=0, restarted=False, context_resets=1)),
                     ("zoom-a", dict(rejected=2, restarted=False, context_resets=0))]


def test_freeze_keeps_each_file_once_within_the_evidence_depth(tmp_path):
    run, pool = run_with_sessions(tmp_path)
    dest = tmp_path / "cases"
    summary = cases.freeze(run, dest)
    assert summary["frozen"] == 3 and summary["skipped"] == 0
    index = json.loads((dest / "cases.json").read_text())
    assert len(index["frozen"]) == 3 and not any("zoom-b" in s for s in index["frozen"])
    case = json.loads((dest / "units/u/rounds/round01/03-zoom-in/zoom-a/case.json").read_text())
    kept = {Path(p).name for p in case["files"]}
    assert {"state.json", "bundle.json", "data.bin", "nested.json", "deeper.json"} <= kept and "deep.bin" not in kept
    for meta in case["files"].values():
        assert (dest / "files" / meta["sha256"]).stat().st_size == meta["size"]
    copied = dest / "units/u/rounds/round01/03-zoom-in/zoom-a/session"
    assert (copied / "session.json").is_file() and not (copied / "session.lock").exists()
    assert (dest / "units/u/rounds/round01/02-cross-sample/type-c/session/restart/session.json").is_file()
    assert cases.freeze(run, dest)["bytes"] == summary["bytes"]  # a second publication adds nothing new
    assert not list((dest / "files").glob("*.partial"))


def test_budgets_and_changed_files_skip_a_case_and_say_why(tmp_path, monkeypatch):
    run, pool = run_with_sessions(tmp_path)
    (pool / "requests" / "a" / "data.bin").write_bytes(b"changed")
    monkeypatch.setattr(cases, "CASE_BYTES", 10 ** 6)
    summary = cases.freeze(run, tmp_path / "cases")
    skipped = json.loads((tmp_path / "cases" / "cases.json").read_text())["skipped"]
    assert summary["frozen"] == 2 and [s["session"].rsplit("/", 1)[1] for s in skipped] == ["zoom-a"]
    assert "no longer matches its reference" in skipped[0]["reason"]
    monkeypatch.setattr(cases, "CASE_BYTES", 100)
    cases.freeze(run, tmp_path / "small")
    small = json.loads((tmp_path / "small" / "cases.json").read_text())
    assert not small["frozen"] and {s["reason"] for s in small["skipped"]} == {"case over CASE_BYTES"}
    monkeypatch.setattr(cases, "CASE_BYTES", 10 ** 6)
    monkeypatch.setattr(cases, "RUN_BYTES", 2500)
    cases.freeze(run, tmp_path / "tight")
    tight = json.loads((tmp_path / "tight" / "cases.json").read_text())
    assert len(tight["frozen"]) == 1 and {s["reason"] for s in tight["skipped"]} == {"run over RUN_BYTES"}


def test_the_published_sync_freezes_the_run_into_its_case_library(tmp_path):
    run = gen2_run(tmp_path / "run")
    session(run, "units/u/rounds/round01/03-zoom-in/zoom-a", evidence(tmp_path / "pool", "a"), rejected=3)
    record = {"name": "run", "collection": "c", "dataset": "d", "run": "r1", "source": str(run),
              "work": str(tmp_path / "work" / "c" / "d" / "r1.tar.gz")}
    packet = tmp_path / "packet.json"
    save(packet, {"root": str(run), "dest": str(tmp_path / "display" / "c" / "d" / "r1"), "record": record,
                  "final": False, "label": "published"})
    subprocess.run([sys.executable, "-m", "ecarsi.stages.display", str(packet)], cwd=tmp_path, check=True,
                   env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    synced = json.loads((tmp_path / "synced.json").read_text())
    assert synced["cases"]["frozen"] == 1
    assert cases.library(record) == tmp_path / "work" / "_cases" / "c" / "d" / "r1"
    assert (cases.library(record) / "cases.json").is_file()

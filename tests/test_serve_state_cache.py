"""Fleet pages read dataset states from the warmer's cache; rendered pages are gzip-compressed on request."""
from __future__ import annotations

import gzip
import http.server
import threading
import urllib.request
from functools import partial
from pathlib import Path

import pytest

from ecarsi.ui import serve


class _Reg:
    path = Path("/tmp/registry.json")

    def __init__(self, items):
        self._items = items

    def snapshot(self):
        return dict(self._items)

    def get(self, name):
        return self._items.get(name)

    cached_snapshot = snapshot


def test_state_cache_serves_from_refresh_and_forgets_unbound(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(serve, "_dataset_state", lambda root: calls.append(root) or {"stage": "x", "cls": "running"})
    a, b = tmp_path / "a", tmp_path / "b"
    reg = _Reg({"a": a, "b": b})
    cache = serve.StateCache(reg, ttl=60)
    assert cache.get(a)["stage"] == "Loading status" and calls == []
    cache.refresh()
    assert sorted(calls) == [a, b]
    cache.get(a); cache.get(b)
    cache._states[a] = (0, cache._states[a][1])
    assert cache.get(a)['stage'] == 'x' and cache.get(a)['cached_at'] == 0
    assert len(calls) == 2  # stale hits also never read disk on the request path
    reg._items.pop("b")
    cache.refresh()
    assert b not in cache._states and a in cache._states


def test_fleet_http_never_scans_on_cold_or_stale_cache(tmp_path, monkeypatch):
    reg = _Reg({'sample': tmp_path/'sample'/'rsi'})
    cache = serve.StateCache(reg)
    def forbidden(*args, **kwargs):
        raise AssertionError('request performed a synchronous dataset scan')
    monkeypatch.setattr(serve, '_dataset_state', forbidden)
    monkeypatch.setattr(serve.index, 'collection_of', forbidden)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(serve.Handler, registry=reg, states=cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{httpd.server_address[1]}'
        for path in ['/', '/_home', '/_history.json']:
            with urllib.request.urlopen(base+path, timeout=2) as response:
                assert response.status == 200
                body = response.read()
                if path == '/_home':
                    assert b'0 / 1 loaded' in body and b'Loading status' in body
    finally:
        httpd.shutdown(); httpd.server_close()


def test_cached_registry_and_saved_states_survive_slow_refresh(tmp_path, monkeypatch):
    path = tmp_path/'registry.json'
    root = tmp_path/'sample'
    serve.Registry.write_file(path, {'sample': root})
    registry = serve.Registry(path)
    registry.snapshot()
    cache_file = tmp_path/'cache.json'
    monkeypatch.setattr(serve, '_dataset_state', lambda _: dict(stage='released', cls='released'))
    cache = serve.StateCache(registry, cache_file=cache_file)
    cache._put(root)
    def unavailable(*args, **kwargs):
        raise AssertionError('shared storage unavailable')
    monkeypatch.setattr(registry, '_load_if_changed', unavailable)
    monkeypatch.setattr(serve, '_dataset_state', unavailable)
    assert registry.cached_snapshot() == {'sample': root}
    restored = serve.StateCache(registry, cache_file=cache_file)
    assert restored.get(root)['stage'] == 'released'
    assert restored.get(root)['cached_at'] is not None




def test_rendered_pages_are_gzipped_when_accepted(tmp_path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(serve.Handler, registry=_Reg({})))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Encoding"] == "gzip"
            assert b"Periscope" in gzip.decompress(r.read())
        with urllib.request.urlopen(url) as r:  # no Accept-Encoding: plain
            assert r.headers.get("Content-Encoding") is None and b"Periscope" in r.read()
    finally:
        httpd.shutdown(); httpd.server_close()


def test_a_quick_sweep_rereads_the_running_datasets_and_leaves_the_finished_ones(tmp_path, monkeypatch):
    """Rereading every released dataset each cycle is what pushed the few that are still running out
    to a multi-minute refresh -- the lag people see on the overview. Quick sweeps touch the unsettled
    ones; a full sweep still comes round for the rest, so a reopened or newly bound run is not lost."""
    states = {"run": {"stage": "per-sample", "cls": "running"}, "done": {"stage": "released", "cls": "released"}}
    calls = []
    monkeypatch.setattr(serve, "_dataset_state",
                        lambda root: calls.append(root) or dict(states[root.name]))
    run, done = tmp_path / "run", tmp_path / "done"
    cache = serve.StateCache(_Reg({"run": run, "done": done}), ttl=60)
    cache.refresh()                                   # first sweep is full: both read
    assert sorted(calls) == sorted([done, run])
    calls.clear()
    cache.refresh(full=False)
    assert calls == [run], "a quick sweep must skip the finished datasets and reread the running one"
    calls.clear()
    cache.refresh(full=True)
    assert sorted(calls) == sorted([done, run])


def test_a_quick_sweep_with_nothing_running_still_reads_something(tmp_path, monkeypatch):
    """Every dataset settled: skipping all of them would freeze the cache against a reopened run."""
    monkeypatch.setattr(serve, "_dataset_state", lambda root: {"stage": "released", "cls": "released"})
    done = tmp_path / "done"
    cache = serve.StateCache(_Reg({"done": done}), ttl=60)
    cache.refresh()
    seen = []
    monkeypatch.setattr(serve, "_dataset_state", lambda root: seen.append(root) or {"stage": "released", "cls": "released"})
    cache.refresh(full=False)
    assert seen == [done]


def _published(tmp_path, workflows, age=0.0):
    import json, time
    p = tmp_path / "fleet-status.json"
    p.write_text(json.dumps({"generated_at": time.time() - age, "workflows": workflows}))
    return serve.ControlVerdicts(p)


def test_the_control_plane_verdict_overrides_what_the_files_imply(tmp_path):
    """Files say per-sample running because that is the last thing that published. The control plane
    knows the run died; the row must follow the control plane and keep the last seen stage as detail."""
    v = _published(tmp_path, {"dataset/run-a": {"status": "FAILED", "started": 1.0, "closed": 2.0}})
    row = dict(name="u", stage="per-sample running", cls="running")
    out = serve.reconcile(row, v.of("run-a"))
    assert out["cls"] == "failed" and out["live"] == "failed"
    assert out["stage"] == "failed · last seen per-sample running"


def test_a_resumed_run_stops_reading_as_failed_before_it_publishes_again(tmp_path):
    """The other direction: the files still hold the old failure, the control plane has it running."""
    v = _published(tmp_path, {"dataset/run-a": {"status": "RUNNING", "started": 1.0, "closed": None}})
    out = serve.reconcile(dict(name="u", stage="failed — round 1", cls="failed"), v.of("run-a"))
    assert out["cls"] == "running" and out["live"] == "running"


def test_a_stale_or_missing_publisher_leaves_the_row_alone_and_says_so(tmp_path):
    """A monitor that stopped writing must not keep answering for the fleet."""
    stale = _published(tmp_path, {"dataset/run-a": {"status": "FAILED", "started": 1.0, "closed": 2.0}},
                       age=serve.ControlVerdicts.FRESH + 60)
    assert stale.of("run-a") is None and not stale.live() and stale.age() > serve.ControlVerdicts.FRESH
    row = dict(name="u", stage="per-sample running", cls="running")
    assert serve.reconcile(row, stale.of("run-a")) == row
    absent = serve.ControlVerdicts(tmp_path / "nope.json")
    assert absent.age() is None and absent.of("run-a") is None
    assert serve.ControlVerdicts(None).of("run-a") is None


def test_a_generation_one_run_has_no_run_id_and_is_never_reconciled(tmp_path):
    v = _published(tmp_path, {"dataset/run-a": {"status": "FAILED", "started": 1.0, "closed": 2.0}})
    assert v.of("") is None


def test_the_page_names_the_clock_its_status_column_is_on(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "_dataset_state", lambda root: dict(
        units=1, released=0, n_input=10, final_cells=None, rounds=1, species="mouse", finished=None,
        updated=1.0, events={"organize": [], "release": []}, stage="per-sample running", cls="running",
        collection="coll", trend=[], run_id="run-a",
        unit_rows=[dict(name="u", stage="per-sample running", cls="running", released=False, n_input=10,
                        final_cells=None, rounds=1, species="mouse", updated=1.0, trend=[])]))
    v = _published(tmp_path, {"dataset/run-a": {"status": "FAILED", "started": 1.0, "closed": 2.0}})
    html = serve._home_html({"coll-Organ": tmp_path}, state=serve._dataset_state, verdicts=v)
    assert "status from the control plane" in html
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "failed · last seen per-sample running" in body
    plain = serve._home_html({"coll-Organ": tmp_path}, state=serve._dataset_state)
    assert "Run status:" not in plain and "per-sample running" in plain   # no control plane, no claim


def test_a_unit_gets_the_verdict_on_that_unit_not_on_its_run(tmp_path, monkeypatch):
    """A run is still running when one of its units has already failed. Applying the run's verdict to
    every unit reopened that failed unit as running, which is the same lie in the other direction."""
    v = _published(tmp_path, {"dataset/r": {"status": "RUNNING", "started": 2.0, "closed": None},
                              "dataset/r/unit-0": {"status": "RUNNING", "started": 2.0, "closed": None},
                              "dataset/r/unit-1": {"status": "FAILED", "started": 2.0, "closed": 3.0}})
    unit = lambda i, name, cls, stage: dict(  # noqa: E731
        name=name, index=i, stage=stage, cls=cls, released=False, n_input=1, final_cells=None,
        rounds=1, species="mouse", updated=1.0, trend=[])
    monkeypatch.setattr(serve, "_dataset_state", lambda root: dict(
        units=2, released=0, n_input=2, final_cells=None, rounds=1, species="mouse", finished=None,
        updated=1.0, events={"organize": [], "release": []}, stage="2 units running", cls="running",
        collection="coll", trend=[], run_id="r",
        unit_rows=[unit(0, "alpha", "running", "per-sample running"),
                   unit(1, "beta", "running", "round 1 · cross-sample")]))
    body = serve._home_html({"coll-Organ": tmp_path}, state=serve._dataset_state,
                            verdicts=v).split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    alpha, beta = body.split("<tr ")[1], body.split("<tr ")[2]
    assert 'class="pill running"' in alpha
    assert 'class="pill failed"' in beta and "last seen round 1 · cross-sample" in beta


def test_a_run_verdict_may_close_a_unit_row_but_never_reopen_one(tmp_path):
    """Without the plan's unit order only the run's verdict is available: a finished run has no unit
    still running, but a running run cannot vouch for a unit the files call failed."""
    v = _published(tmp_path, {"dataset/r": {"status": "RUNNING", "started": 1.0, "closed": None}})
    failed = dict(name="u", stage="failed — round 1", cls="failed")
    assert serve.reconcile(failed, v.of("r"), precise=False) == failed
    done = _published(tmp_path, {"dataset/r": {"status": "COMPLETED", "started": 1.0, "closed": 2.0}})
    out = serve.reconcile(dict(name="u", stage="per-sample running", cls="running"), done.of("r"), precise=False)
    assert out["cls"] == "released" and out["live"] == "completed"


def test_a_run_the_control_plane_publishes_is_a_row_without_being_registered(tmp_path):
    """The coordinator and the monitor disagreed about what the fleet was: a dataset submitted ten
    minutes earlier was absent from the page until someone ran scan-add by hand (2026-09-22). The
    plane now publishes each run's output_root from the spec it was started with, and the registry
    merges those runs under the file and the command line.

    But "everything the plane ever owned" is not the fleet: the first cut of this put 31 dead runs
    back on the page in one refresh. A run is a row while it runs and for a day after it finishes,
    unless a later run of the same dataset has superseded it; a failure stays until it is superseded,
    however old (the owner: a failed run must never quietly leave the table); a registered name keeps
    its own path; a run whose directory is gone, and a publisher that has gone quiet, contribute nothing."""
    import json
    import time
    from ecarsi.ui.serve import ControlVerdicts, Registry

    now = time.time()
    runs = tmp_path / "runs"
    for name in ("duodenum-c", "duodenum-b", "duodenum", "adrenal", "blood", "spleen", "kim2020"):
        (runs / name).mkdir(parents=True)
    registered_dir = tmp_path / "elsewhere" / "kim2020"
    registered_dir.mkdir(parents=True)
    status = tmp_path / "fleet-status.json"
    def wf(name, state, started, closed=None, dataset=None):
        return {"kind": "DatasetWorkflow", "status": state, "started": started, "closed": closed,
                "dataset_id": dataset or name, "output_root": str(runs / name)}
    status.write_text(json.dumps({"generated_at": now, "workflows": {
        "dataset/duodenum-c": wf("duodenum-c", "RUNNING", now - 600, dataset="Duodenum"),        # running: a row
        "dataset/duodenum-b": wf("duodenum-b", "FAILED", now - 7200, now - 3600, "Duodenum"),   # superseded by -c: not a row
        "dataset/duodenum":   wf("duodenum", "FAILED", now - 90000, now - 86000, "Duodenum"),  # superseded and stale
        "dataset/adrenal":    wf("adrenal", "FAILED", now - 7200, now - 1800),                  # failed an hour ago: a row
        "dataset/blood":      wf("blood", "FAILED", now - 5 * 86400, now - 4 * 86400),          # failed four days ago, never resubmitted: still a row
        "dataset/spleen":     wf("spleen", "COMPLETED", now - 5 * 86400, now - 4 * 86400),      # finished four days ago: history
        "dataset/kim2020":    wf("kim2020", "RUNNING", now - 60),                               # registered under another path
        "dataset/gone":       {**wf("gone", "RUNNING", now - 60), "output_root": str(runs / "gone")},  # no directory
        "dataset/duodenum-c/unit-0": {"kind": "AnalysisUnitWorkflow", "status": "RUNNING",
                                      "started": now - 500, "output_root": str(runs / "duodenum-c")},  # units are not rows
    }}))
    verdicts = ControlVerdicts(status)
    assert verdicts.runs() == {"duodenum-c": runs / "duodenum-c", "adrenal": runs / "adrenal",
                               "blood": runs / "blood", "kim2020": runs / "kim2020"}

    reg_file = tmp_path / "registry.json"
    reg_file.write_text(json.dumps({"kim2020": str(registered_dir)}))
    registry = Registry(reg_file, published=verdicts.runs)
    items = registry.snapshot()
    assert items["duodenum-c"] == runs / "duodenum-c"     # on the page, nobody registered it
    assert items["kim2020"] == registered_dir             # the registry's own path wins
    assert set(items) == {"duodenum-c", "adrenal", "blood", "kim2020"}

    # A publisher that has gone quiet stops adding rows; the file's rows remain.
    status.write_text(json.dumps({"generated_at": 0.0, "workflows": {}}))
    verdicts._mtime = None
    assert set(registry.snapshot()) == {"kim2020"}

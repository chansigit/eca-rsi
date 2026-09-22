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

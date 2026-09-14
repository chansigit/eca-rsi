"""Fleet pages read dataset states from the warmer's cache; rendered pages are gzip-compressed on request."""
from __future__ import annotations

import gzip
import http.server
import threading
import urllib.request
from functools import partial
from pathlib import Path

import pytest

from ecarsi import serve


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
    monkeypatch.setattr('ecarsi.workflow_web.render', forbidden)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(serve.Handler, registry=reg, states=cache))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f'http://127.0.0.1:{httpd.server_address[1]}'
        for path in ['/', '/_home', '/_history.json', '/_workflows/status.json']:
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


def test_background_batch_monitor_never_reads_on_request(monkeypatch):
    from types import SimpleNamespace
    from ecarsi import batch
    starts = []
    monkeypatch.setattr(batch, '_monitor_background', None)
    def forbidden():
        raise AssertionError('request scanned the whole task queue')
    monkeypatch.setattr(batch, '_read_monitor', forbidden)
    monkeypatch.setattr(threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: starts.append(kw['target'])))
    ready = batch.start_monitor()
    assert batch.start_monitor() is ready and not ready.is_set()
    assert len(starts) == 1
    assert batch.monitor() == dict(datasets=[], nodes={}, by_mirror={})
    last = dict(datasets=[{'state': 'running'}], nodes={}, by_mirror={})
    batch._monitor_background = last
    assert batch.monitor() is last


def test_state_warmer_waits_for_initial_queue_snapshot(monkeypatch):
    from types import SimpleNamespace
    targets, reads = [], []
    monkeypatch.setattr(threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: targets.append(kw['target'])))
    cache = serve.StateCache(_Reg({}))
    monkeypatch.setattr(cache, 'refresh', lambda: reads.append(True))
    class PendingSnapshot(Exception):
        pass
    def wait():
        raise PendingSnapshot
    cache.start(SimpleNamespace(wait=wait))
    for target in targets:
        with pytest.raises(PendingSnapshot):
            target()
    assert not reads


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

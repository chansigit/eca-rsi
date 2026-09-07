"""Fleet pages read dataset states from the warmer's cache; rendered pages are gzip-compressed on request."""
from __future__ import annotations

import gzip
import http.server
import threading
import urllib.request
from functools import partial
from pathlib import Path

from ecarsi import serve


class _Reg:
    path = Path("/tmp/registry.json")

    def __init__(self, items):
        self._items = items

    def snapshot(self):
        return dict(self._items)

    def get(self, name):
        return self._items.get(name)


def test_state_cache_serves_from_refresh_and_forgets_unbound(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(serve, "_dataset_state", lambda root: calls.append(root) or {"stage": "x", "cls": "running"})
    a, b = tmp_path / "a", tmp_path / "b"
    reg = _Reg({"a": a, "b": b})
    cache = serve.StateCache(reg, ttl=60)
    assert cache.get(a)["stage"] == "x" and calls == [a]  # miss: computed live
    cache.refresh()
    assert sorted(calls[1:]) == [a, b]
    cache.get(a); cache.get(b)
    assert len(calls) == 3  # hits: no disk read
    reg._items.pop("b")
    cache.refresh()
    assert b not in cache._states and a in cache._states


def test_rendered_pages_are_gzipped_when_accepted(tmp_path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(serve.Handler, registry=_Reg({})))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Encoding"] == "gzip"
            assert b"ECA-RSI runs" in gzip.decompress(r.read())
        with urllib.request.urlopen(url) as r:  # no Accept-Encoding: plain
            assert r.headers.get("Content-Encoding") is None and b"ECA-RSI runs" in r.read()
    finally:
        httpd.shutdown(); httpd.server_close()

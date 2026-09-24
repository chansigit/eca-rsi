"""Periscope mounts the control-plane monitor at /_control/ when started with --control-plane."""
import http.server
import json
import threading
import time
import urllib.error
import urllib.request
from functools import partial

from ecarsi.ui import serve
from ecarsi.observatory import MAX_WINDOW, ControlPlane
from ecarsi.warm_pool.state import save


def run_dir(tmp_path):
    base = tmp_path / 'run'
    for name, config in (('pool', {'protocol': 1}), ('bridge', {'concurrency': 1})):
        (base / name / 'requests').mkdir(parents=True)
        save(base / name / 'config.json', config)
    return base


def get(server, path):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}{path}', timeout=10) as r:
            return r.status, r.headers.get('Content-Type', ''), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get('Content-Type', ''), e.read()


def serving(tmp_path, control):
    registry = serve.Registry(tmp_path / 'registry.json')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(serve.Handler, registry=registry, control=control))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_control_page_and_apis_are_served_under_control(tmp_path):
    control = ControlPlane(run_dir(tmp_path), temporal_port=0)
    server = serving(tmp_path, control)
    try:
        status, kind, body = get(server, '/_control/')
        assert status == 200 and kind.startswith('text/html') and b'Compute Warm Pool' in body
        assert b"fetch('api/status'" in body and b"fetch('/api/" not in body  # relative: the page lives under /_control/
        status, kind, body = get(server, '/_control/api/status')
        data = json.loads(body)
        assert status == 200 and kind.startswith('application/json') and data['workers'] == [] and data['pool_total'] == 0
        assert data['earliest_activity'] is None and data['recent_failures'] == []
        now = int(time.time())
        status, _, body = get(server, f'/_control/api/timeline?since={now - 600}&until={now}')
        assert status == 200
        # The page is a live monitor, not an archive: a window wider than MAX_WINDOW is refused and
        # the caller is pointed at the durable journal instead of being served a multi-minute walk.
        status, _, body = get(server, f'/_control/api/timeline?since={now - MAX_WINDOW - 60}&until={now}')
        assert status == 400 and b'4 hours' in body and b'tasks-<day>.jsonl' in body
        assert get(server, '/_control/api/other')[0] == 404
        assert get(server, '/_control')[0] == 200  # redirected to /_control/
        status, _, body = get(server, '/')
        assert status == 200 and b'id="control-item"' in body
    finally:
        server.shutdown()


def test_without_a_control_plane_the_mount_says_how_to_get_one(tmp_path):
    server = serving(tmp_path, None)
    try:
        status, _, body = get(server, '/_control/api/status')
        assert status == 404 and b'--control-plane' in body
        assert b'id="control-item"' not in get(server, '/')[2]
    finally:
        server.shutdown()


def test_observatory_cli_no_longer_serves():
    import argparse
    from ecarsi import observatory
    parser = argparse.ArgumentParser()
    assert 'ControlPlane' in dir(observatory) and not hasattr(observatory, 'serve')
    assert 'serve' not in observatory.main.__doc__ if observatory.main.__doc__ else True


def test_the_monitor_needs_no_index_because_it_reads_published_records(tmp_path):
    """There is no index to grow or forget any more. Settled rows used to be kept for the life of
    the process -- so a one-hour question walked every request ever seen -- and the cure for that was
    not a smaller cache but a record the workers write about themselves, keyed by day."""
    from ecarsi.ui.control import snapshot
    root = run_dir(tmp_path)
    cache = {}
    snapshot(root, temporal_port=0, cache=cache)
    assert "pool_done" not in cache and "pool_stale" not in cache
    assert set(cache) <= {"journals", "resource_files", "worker_tails", "indexed_since"}


def test_a_published_run_already_bound_under_another_name_is_one_row(tmp_path):
    # The plane publishes a run by its directory name; the registry file may bind the same
    # directory under a qualified name. One path, one row, and the file's name is the one shown.
    swahn, yan = tmp_path / 'chondro' / '07_Swahnetal', tmp_path / 'chondro' / '08_Yanetal'
    registry = serve.Registry(tmp_path / 'registry.json', published=lambda: {'07_Swahnetal': swahn, '08_Yanetal': yan})
    serve.Registry.write_file(registry.path, {'chondroatlas-g2-07_Swahnetal': swahn})
    assert registry.snapshot() == {'chondroatlas-g2-07_Swahnetal': swahn, '08_Yanetal': yan}
    assert registry.cached_snapshot() == registry.snapshot()

"""Periscope mounts the control-plane monitor at /_control/ when started with --control-plane."""
import http.server
import json
import threading
import time
import urllib.error
import urllib.request
from functools import partial

from ecarsi import serve
from ecarsi.observatory import ControlPlane
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
        assert status == 200 and json.loads(body)['indexing'] is False  # inside the index: no widening walk
        assert json.loads(get(server, '/_control/api/timeline?since=1000&until=4600')[2])['indexing'] is True
        status, _, body = get(server, '/_control/api/timeline?since=0&until=99999999')
        assert status == 400 and b'7 days' in body
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

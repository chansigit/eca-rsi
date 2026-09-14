import json

from ecarsi import model_web


def test_agent_bridge_activity_snapshot_and_escaped_recent_calls(tmp_path, monkeypatch):
    from harness_bridge import telemetry
    monkeypatch.setenv('AGENT_BRIDGE_TELEMETRY_DIR', str(tmp_path / 'telemetry'))
    telemetry.record('start', 'call1', label='<img src=x onerror=alert(1)>', cwd='/data/sample')
    telemetry.record('attempt', 'call1', harness='openai', model='model<unsafe>')
    telemetry.record('model_start', 'call1')
    catalog = tmp_path / 'models.json'
    catalog.write_text(json.dumps({'models': []}))
    data = model_web.snapshot({'ECA_MODEL_CATALOG': str(catalog)})
    assert len(data['activity']['active']) == 1
    page = model_web.render(data)
    assert 'Waiting for model' in page and 'Active turns' in page and 'By model' in page
    assert '&lt;img' in page and '<img' not in page and 'model&lt;unsafe&gt;' in page
    telemetry.record('success', 'call1', harness='openai', model='model<unsafe>', tokens_in=42, tokens_out=7)
    data = model_web.snapshot({'ECA_MODEL_CATALOG': str(catalog)})
    assert data['activity']['active'] == [] and data['activity']['tokens_in'] == 42
    assert '42 in / 7 out' in model_web.render(data)


def test_model_order_validation_and_public_fields(tmp_path):
    catalog = tmp_path / 'models.json'
    catalog.write_text(json.dumps({'source': '<img src=x onerror=alert(1)>',
        'candidates': 'openai:primary,openai@openrouter:vendor/fallback:free',
        'api_key': 'never-public',
        'validation': [{'candidate': 'openai:alternative', 'checked_at': '2026-09-12',
                        'scope': '<script>bad()</script>', 'api_key': 'never-public'}]}))
    env = {'ECA_MODEL_CATALOG': str(catalog), 'ARK_API_KEY': 'never-public'}
    data = model_web.snapshot(env)
    assert [x['model'] for x in data['chain']] == ['primary', 'vendor/fallback:free']
    assert [x['position'] for x in data['chain']] == [1, 2]
    assert data['live_availability'] == 'not_probed'
    assert data['alternatives'][0]['model'] == 'alternative'
    page = model_web.render(data)
    assert 'Primary' in page and 'Fallback 1' in page
    assert '<script>' not in page and '<img' not in page
    assert 'never-public' not in page + json.dumps(data)
    override = model_web.snapshot({**env, 'AGENT_MODEL_POOL': 'claude:first,openai:second'})
    assert [x['model'] for x in override['chain']] == ['primary', 'vendor/fallback:free']
    default = model_web.snapshot({'ECA_MODEL_CATALOG': str(tmp_path / 'missing')})
    assert default['chain'] == [] and default['validated_count'] == 0
    assert 'No models configured.' in model_web.render(default)


def test_settings_atomic_revision_urls_and_key_presence(tmp_path):
    import pytest
    from ecarsi.run_state import write_json
    path = tmp_path / 'models.json'
    env = {'ECA_MODEL_CATALOG': str(path), 'ARK_API_KEY': 'real-secret-not-for-output'}
    write_json(path, {'models': [{'harness': 'openai', 'model': 'first', 'url': 'https://example.test/v1'}]})
    data = model_web.snapshot(env)
    models = [{'harness': 'openai', 'model': 'second', 'url': 'https://example.test/v1'},
              {'harness': 'openai', 'model': 'first', 'url': 'https://example.test/v1'}]
    model_web.save_models(models, data['revision'], env)
    assert [m['model'] for m in model_web.snapshot(env)['chain']] == ['second', 'first']
    with pytest.raises(FileExistsError):
        model_web.save_models([], data['revision'], env)
    for url in ('file:///etc/passwd', 'https://user:secret@example.test/v1', 'https://example.test?api_key=secret'):
        with pytest.raises(ValueError):
            model_web.normalized_models({'models': [{'harness': 'openai', 'model': 'a', 'url': url}]})
    with pytest.raises(ValueError):
        model_web.normalized_models({'models': [models[0], {**models[1], 'url': 'https://different.test/v1'}]})
    bashrc = tmp_path / 'bashrc'
    bashrc.write_text("export ARK_API_KEY='secret-never-display'\n")
    result = model_web.key_presence(env, bashrc)
    assert result['keys'] == [{'variable': 'ARK_API_KEY', 'bashrc': 'present_literal', 'process': True}]
    assert 'secret' not in json.dumps(result)
    marker = tmp_path / 'must-not-exist'
    bashrc.write_text('export ARK_API_KEY="$(touch ' + str(marker) + ')"\n')
    assert model_web.key_presence(env, bashrc)['keys'][0]['bashrc'] == 'indirect'
    assert not marker.exists()
    bashrc.write_text("export ARK_API_KEY='PASTE_REAL_KEY_HERE'\n")
    assert model_web.key_presence(env, bashrc)['keys'][0]['bashrc'] == 'empty_or_placeholder'
    assert 'DOUBAO_BASE_URL=https://example.test/v1' in model_web.snapshot(env)['exports']


def test_settings_http_admin_gate_and_no_secret_echo(tmp_path, monkeypatch):
    import http.server
    import threading
    import urllib.request
    import urllib.error
    from functools import partial
    from ecarsi import serve
    catalog = tmp_path / 'models.json'
    catalog.write_text(json.dumps({'models': []}))
    token = 't' * 40
    (tmp_path / 'model-admin-token').write_text(token)
    monkeypatch.setenv('ECA_MODEL_CATALOG', str(catalog))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), partial(serve.Handler, registry=serve.Registry(tmp_path / 'registry.json')))
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    def post(action, body, management=None):
        headers = {'X-Forwarded-For': '203.0.113.1', 'Content-Type': 'application/json'}
        if management:headers['X-Model-Admin'] = management
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/_models/{action}',data=json.dumps(body).encode(),headers=headers)
        try:r=urllib.request.urlopen(req,timeout=5)
        except urllib.error.HTTPError as e:r=e
        with r:return r.status,json.load(r)
    try:
        for action in ('access','save','keys'):
            assert post(action, {})[0] == 403
            assert post(action, {}, 'wrong')[0] == 403
        assert post('access', {}, token)[0] == 200
        revision=model_web.snapshot()['revision']
        assert post('save', {'revision':revision,'models':[{'harness':'openai','model':'new','url':'https://example.test/v1'}]}, token)[0] == 200
        assert post('save', {'revision':revision,'models':[]}, token)[0] == 409
        assert post('save', {'revision':model_web.snapshot()['revision'],'models':[{'harness':'openai','model':'new','url':'https://secret-value@example.test'}]}, token)[0] == 400
        assert model_web.snapshot()['chain'][0]['model'] == 'new'
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)

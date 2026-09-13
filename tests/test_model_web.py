import json

from ecarsi import model_web


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
    assert [x['model'] for x in override['chain']] == ['first', 'second']
    default = model_web.snapshot({'ECA_MODEL_CATALOG': str(tmp_path / 'missing')})
    assert len(default['chain']) == 1 and default['validated_count'] == 0

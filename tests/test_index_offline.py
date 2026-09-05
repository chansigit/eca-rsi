"""Saved UMAP pages carry their data safely without local-file fetches."""
import json
from html.parser import HTMLParser

from ecarsi.index import render_unit


class Scripts(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.scripts = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.current = [dict(attrs), '']
            self.scripts.append(self.current)

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data

    def handle_endtag(self, tag):
        if tag == 'script':
            self.current = None


def test_umap_embeds_exact_data_without_script_injection(tmp_path):
    release = tmp_path / 'release'
    release.mkdir()
    data = {'n': 2, 'cell_id': ['001', '</script><script>alert(1)</script>'],
            'labels': ['stress < high & low', '中文\u2028label'], 'x': [0, 65535]}
    source = json.dumps(data, ensure_ascii=False)
    (release / 'umap.json').write_text(source)
    scripts = Scripts(render_unit(tmp_path)).scripts
    embedded = [body for attrs, body in scripts if attrs.get('id') == 'umap-data']
    assert len(embedded) == 1
    assert json.loads(embedded[0]) == data
    executable = [body for attrs, body in scripts if attrs.get('type') != 'application/json']
    assert len(executable) == 1
    assert 'fetch(' not in executable[0]
    assert 'alert(1)' not in executable[0]
    assert (release / 'umap.json').read_text() == source


def test_unreleased_page_has_no_umap(tmp_path):
    html = render_unit(tmp_path)
    assert 'id="umap-vis"' not in html
    assert 'id="umap-data"' not in html

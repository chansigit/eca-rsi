import shutil
import subprocess
from pathlib import Path

import pytest

from ecarsi.serve import NAV_JS, _home_html, _navigator_html


def test_home_html_renders_stats_and_no_dataset_frame():
    html = _home_html({})
    assert "<title>Periscope — overview</title>" in html
    assert '>0</span><span class="k">datasets</span>' in html
    assert "No dataset is bound yet" in html
    assert "iframe" not in html


def test_navigator_includes_home_item_and_sort_control():
    html = _navigator_html({}, Path("/tmp/registry.json"))
    assert 'id="home-item"' in html
    assert 'id="nav-sort"' in html
    assert 'id="sb-resizer"' in html


@pytest.mark.skipif(not shutil.which("node"), reason="node not on PATH")
def test_nav_js_is_syntactically_valid(tmp_path):
    """A JS SyntaxError anywhere in NAV_JS silently kills the whole IIFE —
    no click handlers, no sort/resize, no updated counts — with zero signal
    in the page's HTTP response. Catch that class of bug here."""
    script = tmp_path / "nav.js"
    script.write_text(NAV_JS)
    subprocess.run(["node", "--check", str(script)], check=True)


def test_access_log_names_the_tunnel_visitor(capsys):
    """Behind ngrok the socket peer is 127.0.0.1; the log must show X-Forwarded-For and the UA."""
    from ecarsi.serve import Handler

    class Fake:
        headers = {"X-Forwarded-For": "203.0.113.9, 127.0.0.1", "User-Agent": "TestBrowser/1.0"}

        def address_string(self):
            return "127.0.0.1"

    Handler.log_message(Fake(), '"GET /Aorta/ HTTP/1.1" %s -', 200)
    line = capsys.readouterr().err
    assert "203.0.113.9" in line and '"TestBrowser/1.0"' in line and "127.0.0.1" not in line

import shutil
import subprocess
from pathlib import Path

import pytest

from ecarsi.serve import NAV_JS, _home_html, _navigator_html


@pytest.mark.parametrize('count,display', [(9_999_999, '9,999,999'), (10_000_000, '10.00 M'), (12_345_678, '12.35 M')])
def test_cell_row_compacts_inputs_but_keeps_releases_exact(monkeypatch, count, display):
    from ecarsi import serve
    monkeypatch.setattr(serve, 'fleet_totals', lambda _: dict(cells_in=count, cells_queued=count,
        cells_released=count, kept=None, undated_input=0))
    html = _home_html({})
    row = html.split('aria-label="Cell counts">', 1)[1].split('<section', 1)[0]
    assert 'data-stat="datasets"' not in row
    for key in ('cells-in', 'cells-queued'):
        assert f'data-stat="{key}"><span class="v">{display}</span>' in row
    assert f'data-stat="cells-released"><span class="v">{count:,}</span>' in row


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


def test_cells_cards_use_history_not_queued_inputs_or_partial_output():
    from ecarsi.serve import fleet_history, fleet_totals, history_at
    def state(cls, n, final, org=(), rel=()):
        return dict(cls=cls, n_input=n, final_cells=final, species="mouse",
                    events=dict(organize=list(org), release=list(rel)))
    states = {
        "queued": (state("queued", 10000, None), Path("/queued")),
        "multi": (state("running", 300, 240, [(1,100),(2,200)], [(3,80)]), Path("/multi")),
        "done": (state("released", 100, 70, [(1,100)], [(4,70)]), Path("/done")),
        "undated": (state("released", 50, 40, [], [(5,40)]), Path("/undated")),
    }
    hist = fleet_history(states)
    totals = fleet_totals(hist)
    assert totals["cells_in"] == 400  # excludes queued and missing timestamps
    assert totals["cells_queued"] == 10000 and totals["undated_input"] == 50
    assert totals["cells_released"] == 190  # includes the released unit of an active dataset
    assert totals["kept"] == pytest.approx(100*110/150)  # completed cohort only
    assert history_at(hist, 6)["datasets_started"] == 2  # units are not datasets
    assert totals["cells_in"] == history_at(hist, 6)["cells_in"]


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

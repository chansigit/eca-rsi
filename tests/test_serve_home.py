import inspect
import shutil
import subprocess
from pathlib import Path

import pytest

from ecarsi.ui import serve
from ecarsi.ui.serve import NAV_JS, _home_html, _navigator_html


@pytest.mark.parametrize('count,display', [(9_999_999, '9,999,999'), (10_000_000, '10.00 M'), (12_345_678, '12.35 M')])
def test_cell_row_compacts_inputs_but_keeps_releases_exact(monkeypatch, count, display):
    from ecarsi.ui import serve
    monkeypatch.setattr(serve, 'fleet_totals', lambda _: dict(cells_in=count, cells_queued=count,
        cells_released=count, kept=None, undated_input=0))
    html = _home_html({})
    row = html.split('aria-label="Cell counts">', 1)[1].split('<section', 1)[0]
    assert 'data-stat="datasets"' not in row
    for key in ('cells-in', 'cells-queued'):
        assert f'data-stat="{key}"><span class="v">{display}</span>' in row
    assert f'data-stat="cells-released"><span class="v">{count:,}</span>' in row


def test_the_overview_gives_every_unit_its_own_row_and_its_own_curve(tmp_path):
    """A unit is what runs rounds, so it is the row. The dataset aggregate used to stand in for all
    of them: the sparkline was the longest unit's curve and the status was a count of the running
    ones, which says nothing about the unit that is still removing."""
    state = lambda _root: dict(  # noqa: E731 - the page's only disk access, stubbed
        units=2, released=0, n_input=300, final_cells=None, rounds=3, species="mouse", finished=None,
        updated=200.0, events={"organize": [], "release": []}, stage="1/2 units running", cls="running",
        collection="coll", trend=[{"n": 1, "frac": 0.2, "settled": True}], unit_rows=[
            dict(name="liver", stage="round 1 · cross-sample", cls="running", released=False, n_input=100,
                 final_cells=None, rounds=1, species="mouse", updated=100.0,
                 trend=[{"n": 1, "frac": 0.02, "settled": True}]),
            dict(name="spleen", stage="round 3 · zoom-in", cls="running", released=False, n_input=200,
                 final_cells=None, rounds=3, species="mouse", updated=200.0,
                 trend=[{"n": i, "frac": 0.2, "settled": True} for i in (1, 2, 3)])])
    html = _home_html({"coll-Organ": tmp_path}, state=state)
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert body.count("<tr ") == 2 and "2 units" in html
    assert '<td class="nw unit">liver</td>' in body and '<td class="nw unit">spleen</td>' in body
    liver, spleen = body.split('<tr ')[1], body.split('<tr ')[2]
    assert "round 1 · cross-sample" in liver and "round 3 · zoom-in" in spleen
    # two circles per round (dot plus its halo): one round for liver, three for spleen
    assert liver.count("<circle") == 2 and spleen.count("<circle") == 6
    assert '1/2 units running' not in body                                # no aggregate row survives
    assert liver.count('href="/coll-Organ/"') == 1                        # the dataset is still the link


def test_a_dataset_with_no_unit_yet_still_gets_a_row(tmp_path):
    """Organize has not run, or the cached summary predates unit rows: the dataset aggregate is all
    there is, and an empty fleet page would be worse than one row with a blank unit."""
    state = lambda _root: dict(  # noqa: E731
        units=0, released=0, n_input=None, final_cells=None, rounds=0, species="", finished=None,
        updated=None, events={"organize": [], "release": []}, stage="Not started", cls="neutral",
        collection="coll", trend=[])
    body = _home_html({"coll-Organ": tmp_path}, state=state).split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert body.count("<tr ") == 1 and '<td class="nw unit"></td>' in body and "Not started" in body


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
    from ecarsi.ui.serve import fleet_history, fleet_totals, history_at
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
    from ecarsi.ui.serve import Handler

    class Fake:
        headers = {"X-Forwarded-For": "203.0.113.9, 127.0.0.1", "User-Agent": "TestBrowser/1.0"}

        def address_string(self):
            return "127.0.0.1"

    Handler.log_message(Fake(), '"GET /Aorta/ HTTP/1.1" %s -', 200)
    line = capsys.readouterr().err
    assert "203.0.113.9" in line and '"TestBrowser/1.0"' in line and "127.0.0.1" not in line


def test_the_overview_table_fits_without_a_horizontal_scrollbar(tmp_path):
    """Ten columns of run state: the counts are scaled once (as in the sidebar) and the three
    free-text columns are capped, so the numbers, the sparkline and the status share one screen."""
    css = serve.HOME_CSS
    assert "#ds-table td:first-child{max-width:24ch}" in css          # dataset name
    assert "#ds-table td.unit{max-width:16ch" in css                  # unit name
    assert "#ds-table td:nth-child(3){max-width:14ch" in css          # collection
    assert "#ds-table td:nth-child(4){max-width:8ch}" in css          # species
    assert "#ds-table .spark{width:84px}" in css and "max-width:18ch" in css
    assert 'index._n(u["n_input"])' not in inspect.getsource(serve._home_html)   # thousands, not raw
    assert 'index._k(u["n_input"])' in inspect.getsource(serve._home_html)


def test_a_wrapped_status_is_a_label_not_a_capsule():
    """A 999 px radius suits one line. The status column wraps to two or three, and at that
    height the curve cuts into the words it is meant to hold, with the dot floating off the
    line it belongs to."""
    css = serve.HOME_CSS
    assert "#ds-table .pill{white-space:normal;line-height:1.35;max-width:18ch;" in css
    assert "border-radius:var(--r);padding:.25em .6em;align-items:flex-start}" in css
    assert "#ds-table .pill::before{margin-top:.42em}" in css
    from ecarsi.ui import index
    assert "border-radius:999px" in index.CSS   # the capsule stays right everywhere it fits


def test_the_curve_is_shorter_says_less_and_can_be_spaced_by_age():
    """Four sentences of caveat above a 280 px chart buried the one thing it shows, so the detail
    moved behind a disclosure. The switchable axis is TIME: weeks of history in which the
    interesting part is the last few hours, which a clock axis renders as a sliver."""
    html = _home_html({})
    assert 'id="hist-more">What is counted?</a>' in html
    assert '<p class="lede" id="hist-detail" hidden>' in html
    assert "queued inputs are shown separately as Cells awaiting start" not in html.split('id="hist-detail"')[0]
    assert 'id="hist-log" aria-pressed="false" title="space by age instead of by clock' in html
    assert ">log time</button>" in html
    # cells in climbs far faster than cells released and used to flatten it on a shared axis;
    # hidden unless the viewer asks, and the axis only scales to whatever curve is on screen
    assert 'id="hist-show-in" aria-pressed="false"' in html
    assert ">show cells in</button>" in html
    js = serve.HISTORY_JS
    assert 'showIn ? `<path class="ser in"' in js                        # curve gated on the toggle
    assert 'const yraw = Math.max(...(showIn ? ["in", "rel"] : ["rel"]).map(seriesMax), 1);' in js
    assert "const W = 960, H = 200," in js and "H = 280" not in js       # shorter
    assert "let logT = false, showIn = false;" in js and "logY" not in js  # time, not cells
    # age from the right edge, and log(1 + age) so that "now" is a position rather than a pole
    assert "const pos = t => logT ? 1 - Math.log(1 + Math.max(t1 - t, 0)) / lgT" in js
    assert "const un = f => logT ? t1 + 1 - Math.exp((1 - f) * lgT)" in js
    assert "return un((px - L) / (W - L - R));" in js                    # hover inverts the same map
    assert 'xl = t => ageLabel(Math.round(t1 - t));' in js               # ticks read "6h", "2d"
    assert 'logBtn.setAttribute("aria-pressed", String(logT))' in js


def test_log_time_maps_both_ends_exactly_and_round_trips():
    """A hover reads a moment back out of a pixel, so the forward and inverse maps have to agree;
    and the two ends must land on the frame, not near it."""
    import math
    span = 30 * 86400
    t1 = 1_000_000_000.0
    lgT = math.log(1 + span)
    pos = lambda t: 1 - math.log(1 + max(t1 - t, 0)) / lgT
    un = lambda f: t1 + 1 - math.exp((1 - f) * lgT)
    assert pos(t1) == pytest.approx(1) and pos(t1 - span) == pytest.approx(0)
    for age in (0, 3600, 86400, 7 * 86400, span):
        assert un(pos(t1 - age)) == pytest.approx(t1 - age, abs=1)
    assert pos(t1 - 3600) > 0.4     # the last hour takes most of the right half


def test_a_silent_run_the_plane_calls_running_is_not_failed():
    from ecarsi.ui.serve import unstale, reconcile
    class V:
        def of(self, run_id, unit=""): return {"status": "RUNNING"} if run_id == "r" else None
    stale = {"run_id": "r", "cls": "failed", "stage": "stopped · round 3 · cross-sample"}
    assert unstale(stale, V()) == {**stale, "cls": "running", "stage": "no progress 12h+ · round 3 · cross-sample"}
    assert unstale({**stale, "run_id": "gone"}, V())["cls"] == "failed"            # no verdict: the files stand
    assert unstale({**stale, "stage": "failed — boom"}, V())["cls"] == "failed"    # a real failure stays
    row = reconcile({"cls": "failed", "stage": "stopped · round 2 · zoom-in"}, {"status": "RUNNING"}, precise=False)
    assert row["cls"] == "running" and row["stage"].startswith("no progress 12h+")

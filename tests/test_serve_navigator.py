"""Sidebar of the Periscope navigator: collapsed groups with distinct state
tallies, a species filter, and the fields the filter JS reads."""
from pathlib import Path

from ecarsi import index, serve


def _state(p: Path) -> dict:
    name = p.name
    cls = {"a": "released", "b": "running", "c": "failed", "d": "neutral"}[name[0]]
    return {"units": 1, "released": int(cls == "released"), "n_input": 10, "final_cells": 9, "rounds": 2,
            "species": "mm" if name < "c" else "hs", "finished": None, "updated": None, "stage": cls, "cls": cls}


def test_groups_collapsed_with_tallies_and_species(tmp_path):
    items = {f"x-{k}": tmp_path / "x" / k for k in ("a1", "a2", "b1", "c1", "d1")}
    html = serve._navigator_html(items, tmp_path / "reg.json", state=_state)
    assert '<details class="group">' in html and '<details class="group" open>' not in html
    assert serve.group_tally({"released": 2, "running": 1, "neutral": 1, "failed": 1}) in html
    assert 'data-species="mm"' in html and 'data-species="hs"' in html
    assert '<select id="nav-sp"><option value="">all</option>' in html
    assert '<a id="brand" href="/_home"' in html and 'brand.addEventListener("click"' in serve.NAV_JS
    assert '<option value="running">Running</option>' in html
    assert '<option value="queued">Queued</option>' in html
    assert '<option value="working">' not in html
    assert "<option value=\"hs\">hs (2)</option>" in html and "<option value=\"mm\">mm (3)</option>" in html


def test_group_tally_drops_zero_parts():
    dot = '<i class="dot"></i>'
    assert serve.group_tally({"released": 3}) == f'<span class="st released" title="Completed">{dot}3</span>'
    assert serve.group_tally({"running": 1, "neutral": 1}) == (f'<span class="st running" title="Running">{dot}1</span> '
                                                               f'<span class="st neutral" title="Not started">{dot}1</span>')
    assert serve.group_tally({}) == ""
    # the state's colour comes from the page palette (index.CSS .released/.running/\u2026), not from a glyph
    assert ".released,.include{--st:var(--ok)" in index.CSS and ".pill::before,.dot{" in index.CSS


def test_sidebar_counts_read_in_thousands_and_say_whether_they_are_final(tmp_path):
    """A column of counts is read for its size, so it is scaled once, in thousands. The colour
    says how much the number is worth: a released run's is its result, a running one's is only
    what the last finished round left, and a failed run has no count to report at all."""
    assert (index._k(1017), index._k(201278), index._k(None)) == ('1.02k', '201.28k', '')
    items = {f"x-{k}": tmp_path / "x" / k for k in ("a1", "b1", "c1", "d1")}
    html = serve._navigator_html(items, tmp_path / "reg.json", state=_state)
    assert '<span class="cells released"' in html and '<span class="cells running"' in html
    assert '<span class="cells failed"' not in html and '<span class="cells neutral"' not in html
    assert '9,' not in html and '0.01k' in html          # _state gives 9 cells: scaled, not raw
    assert '.item .cells{color:var(--st,var(--muted))' in serve.NAV_CSS   # the status palette, once


def test_clicking_a_dataset_answers_before_the_server_has_rendered_it(tmp_path):
    """A dataset page is built from disk on every request, seconds on a cold run directory. The
    click is answered from what the sidebar already holds -- name, status, species, cells -- and
    the rest is drawn as bars until the real page loads, instead of holding the previous dataset."""
    html = serve._navigator_html({f"x-{k}": tmp_path / "x" / k for k in ("a1", "b1")},
                                 tmp_path / "reg.json", state=_state)
    assert '<div id="pending" hidden aria-live="polite">' in html and 'class="ph-bars"' in html
    js = serve.NAV_JS
    assert "function placeholder(path)" in js
    assert 'frame.style.display = "none"; pending.hidden = false;' in js   # the old page is not held
    assert "placeholder(path); frame.src = path;" in js                    # shown before the request
    assert "if (pending) pending.hidden = true;" in js                     # and retired on load
    # the crumb and the sidebar selection move on click, not on load
    assert 'crumb.textContent = path === "/_home"' in js
    assert "@media (prefers-reduced-motion:reduce){#pending .ph-bars i{animation:none}}" in serve.NAV_CSS

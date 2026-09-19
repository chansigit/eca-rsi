"""Sidebar of the Periscope navigator: collapsed groups with distinct state
tallies, a species filter, and the fields the filter JS reads."""
from pathlib import Path

from ecarsi import serve


def _state(p: Path) -> dict:
    name = p.name
    cls = {"a": "released", "b": "running", "c": "failed", "d": "neutral"}[name[0]]
    return {"units": 1, "released": int(cls == "released"), "n_input": 10, "final_cells": 9, "rounds": 2,
            "species": "mm" if name < "c" else "hs", "finished": None, "updated": None, "stage": cls, "cls": cls}


def test_groups_collapsed_with_tallies_and_species(tmp_path):
    items = {f"x-{k}": tmp_path / "x" / k for k in ("a1", "a2", "b1", "c1", "d1")}
    html = serve._navigator_html(items, tmp_path / "reg.json", state=_state)
    assert '<details class="group">' in html and '<details class="group" open>' not in html
    assert ('<span class="st released" title="Completed">\u27052</span> <span class="st running" title="Running">\U0001F5041</span> '
            '<span class="st neutral" title="Not started">\u26AA1</span> <span class="st failed" title="Failed">\u274C1</span>') in html
    assert 'data-species="mm"' in html and 'data-species="hs"' in html
    assert '<select id="nav-sp"><option value="">all</option>' in html
    assert '<a id="brand" href="/_home"' in html and 'brand.addEventListener("click"' in serve.NAV_JS
    assert '<option value="running">Running</option>' in html
    assert '<option value="queued">Queued</option>' in html
    assert '<option value="working">' not in html
    assert "<option value=\"hs\">hs (2)</option>" in html and "<option value=\"mm\">mm (3)</option>" in html


def test_group_tally_drops_zero_parts():
    assert serve.group_tally({"released": 3}) == '<span class="st released" title="Completed">\u27053</span>'
    assert serve.group_tally({"running": 1, "neutral": 1}) == ('<span class="st running" title="Running">\U0001F5041</span> '
                                                               '<span class="st neutral" title="Not started">\u26AA1</span>')
    assert serve.group_tally({}) == ""

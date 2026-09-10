"""Landing pages after the redesign: the at-a-glance strip, one-sentence section
explanations, the files card, a one-unit root shown inline with rebased links,
the overview page and the grouped navigator — and every inline script parses."""
import json
import shutil
import subprocess

import pytest

from ecarsi import layout as L
from ecarsi.index import (CSS, SANKEY_JS, UMAP_JS, collection_of, dataset_state, display_name, render_root,
                          render_unit, write_all)
from ecarsi.serve import HOME_JS, NAV_JS, _home_html, _navigator_html

UMAP = {"n": 2, "n_total": 2, "x": [0, 65535], "y": [0, 65535], "extra": {},
        "layers": {"coarse": {"column": "zmip_ann_coarse", "labels": ["T", "B"], "colors": ["#111", "#222"], "counts": [1, 1], "idx": [0, 1]},
                   "fine": {"column": "zmip_ann_fine", "labels": ["T1", "T2", "B1"], "colors": ["#1", "#2", "#3"], "counts": [1, 0, 1], "idx": [0, 2]}}}
SANKEY = {"stages": ["per-sample (osp)", "round 1 · msp"], "total": 2,
          "nodes": [{"name": "T", "stage": 0, "count": 2}, {"name": "T", "stage": 1, "count": 1}, {"name": "removed", "stage": 1, "count": 1, "removed": True}],
          "flows": [{"src": 0, "dst": 1, "count": 1}, {"src": 0, "dst": 2, "count": 1}]}


def make_run(tmp_path, released=True):
    """<tmp>/coll/eca-pp/Organ/rsi with one unit, one sample, one finished round;
    released=False adds a second round still in zoomin."""
    root = tmp_path / "coll" / "eca-pp" / "Organ" / "rsi"
    unit = L.unit_dir(root, "organ")
    L.organize_manifest(root).parent.mkdir(parents=True)
    L.organize_manifest(root).write_text(json.dumps({"state": "complete", "input_units": [{"name": "Organ"}], "plan": {"analysis_units": []}}))
    L.input_manifest(unit).parent.mkdir(parents=True)
    L.input_manifest(unit).write_text(json.dumps({"n_cells": 1000, "species": "mouse"}))
    log = ["2026-09-06 10:00:00 organize: 1000 cells", "2026-09-06 12:00:00 round 1 stats removed=100/900 (11.11%) decision=continue [x]"]
    sdir = L.persample_root(unit) / "S1"
    sdir.mkdir(parents=True)
    for f in ("report.html", "qc_summary.csv", "qc_removed.csv", "annotation_proposal.json"):
        (sdir / f).write_text("x")
    L.persample_manifest(unit).write_text(json.dumps({"sample_column": "sample", "species": "mouse", "annotate": True,
                                                      "samples": [{"value": "S1", "n_cells": 1000, "dir": str(sdir)}]}))
    r1 = L.round_dir(unit, 1)
    for d in (L.crosssample_dir(r1), L.zoomin_dir(r1)):
        d.mkdir(parents=True)
        (d / "report.html").write_text("<html>")
    (L.crosssample_dir(r1) / "sample_decisions.csv").write_text("sample,decision,reason\nS1,include,\n")
    L.ledger_dir(r1).mkdir()
    (L.ledger_dir(r1) / "sankey_coarse.png").write_bytes(b"png")
    (L.ledger_dir(r1) / "sankey_coarse.json").write_text(json.dumps(SANKEY))
    (r1 / L.STATS).write_text(json.dumps({"n_in": 900, "n_out": 800, "removed": 100, "frac": 100 / 900, "elapsed_s": 120,
                                          "decision": "release" if released else "continue", "reason": "removed 100 cells"}))
    (r1 / L.DECISION).write_text("release\n" if released else "continue\n")
    if released:
        rel = L.release_dir(unit)
        rel.mkdir()
        for f, body in (("summary.md", "# done"), ("needs_review.md", "# none"), ("needs_review.json", "[]"),
                        ("cell_ledger.csv", "cell\n"), ("umap.json", json.dumps(UMAP))):
            (rel / f).write_text(body)
        (rel / "final.h5ad").write_bytes(b"h5ad")
        log.append("2026-09-06 12:30:00 release rounds=1 final_cells=800 forced=False")
    else:
        r2 = L.round_dir(unit, 2)
        L.crosssample_dir(r2).mkdir(parents=True)
        for f in L.MSP_LIGHT + L.MSP_INTEGRATED_LIGHT:
            (L.crosssample_dir(r2) / f).write_text("x")
        L.zoomin_dir(r2).mkdir()
        (L.zoomin_dir(r2) / "zmip_plan.json").write_text(json.dumps({"lineages": [{"name": "T", "zoom": True}]}))
        log += ["2026-09-06 12:31:00 round 2 start", "2026-09-06 12:31:05 round 2 input prepared from round 1 (800 cells)"]
    (unit / L.PROGRESS).write_text("\n".join(log) + "\n")
    return root, unit


def test_unit_page_glance_files_and_section_explanations(tmp_path):
    root, unit = make_run(tmp_path)
    html = render_unit(unit, "coll-Organ")
    assert '<main class="page">' in html and '<header class="hero">' in html
    assert 'class="pill released"' in html and "released after 1 round(s)" in html
    # at a glance: seven number cards
    glance = html[html.index('<div class="glance">'):html.index('<nav class="jump"')]
    for k in ("input cells", "final cells", "removed overall", "rounds", "coarse / fine labels", "samples done", "needs review"):
        assert f'<span class="k">{k}</span>' in glance, k
    assert "1,000" in glance and "800" in glance and "20.00%" in glance and "2 / 3" in glance
    # every section: heading + one plain-language sentence, and the sticky jump nav points at all of them
    for sid in ("files", "rounds", "samples", "sankey", "umap", "review"):
        assert f'<section class="block" id="{sid}">' in html and f'href="#{sid}"' in html, sid
    assert html.count('<p class="lede">') >= 6
    assert "The loop stops when a round removes less than 1" in html
    # files card: absolute h5ad path plus the release files as links
    files = html[html.index('id="files"'):html.index('id="rounds"')]
    assert f'<code class="path">{L.release_dir(unit) / "final.h5ad"}</code>' in files
    for f in ("summary.md", "needs_review.md", "cell_ledger.csv", "umap.json"):
        assert f'href="release/{f}"' in files, f
    # tables: numbers right-aligned, links relative to the unit
    assert '<th class="r">cells in</th>' in html and 'href="rounds/round01/crosssample/report.html"' in html
    assert 'href="persample/S1/report.html"' in html
    assert "run state updated" in html and "from the run directory" in html


def test_root_with_one_unit_is_shown_inline_with_rebased_links(tmp_path):
    root, unit = make_run(tmp_path)
    html = render_root(root, "coll-Organ")
    assert "<h1>coll-Organ</h1>" in html and "collection <b>coll</b>" in html
    assert "shown below in full" in html and 'href="units/organ/index.html"' in html
    assert '<div class="glance">' in html  # the unit's own content
    assert 'href="units/organ/release/summary.md"' in html
    assert 'href="units/organ/rounds/round01/crosssample/report.html"' in html
    assert 'href="units/organ/persample/S1/report.html"' in html
    assert "1,000 → 800" in html  # cells in → out in the header card
    # the static writer produces the same page, and the unit page next to it
    write_all(root)
    assert "shown below in full" in (root / L.INDEX).read_text()
    assert '<div class="glance">' in (unit / L.INDEX).read_text()


def test_running_unit_shows_cells_now_and_the_running_round(tmp_path):
    root, unit = make_run(tmp_path, released=False)
    html = render_unit(unit)
    assert 'class="pill running"' in html and "zoomin · lineages 0/1" in html
    assert '<span class="k">cells now</span>' in html and "+1 running" in html
    assert "not final, the loop is still running" not in html  # no h5ad on disk in this fixture
    assert "still growing" in html
    st = dataset_state(root)
    assert st["cls"] == "running" and st["final_cells"] == 800 and st["rounds"] == 2 and st["finished"] is None


def test_review_section_is_grouped_cards(tmp_path):
    root, unit = make_run(tmp_path)
    (L.persample_root(unit) / "needs_review.json").write_text(json.dumps(
        {"items": [{"step": "standardize", "source": "S1", "detail": "check input counts"}]}))
    html = render_unit(unit)
    assert '<div class="rv-group tone-info" id="review-upstream_review">' in html
    assert "check input counts" in html and "review-cards" not in html
    sec = html[html.index('id="review"'):]
    assert "<span class=\"count\">2 items — 1 input and per-sample review · 1 loop convergence</span>" in sec
    assert '<div class="rv-group tone-bad" id="review-convergence">' in sec


def test_home_overview_has_stats_table_and_filter(tmp_path):
    root, _ = make_run(tmp_path)
    html = _home_html({"coll-Organ": root, "Gone": tmp_path / "nowhere"})
    assert "<title>Periscope — overview</title>" in html and "Recursive self-improving annotation" in html
    strip = html[html.index('<div class="glance">'):html.index('<section class="block" id="datasets">')]
    for v, k in (("2", "datasets"), ("1", "released"), ("0", "running"), ("1", "failed"), ("1,000", "cells in"), ("800", "cells released")):
        assert f'>{v}</span><span class="k">{k}</span>' in strip, (v, k)
    assert 'id="ds-table"' in html and 'id="ds-q"' in html and 'type="search"' in html
    row = html[html.index('<tr data-text="coll-organ'):]
    row = row[:row.index("</tr>")]
    assert 'href="/coll-Organ/"' in row and "<td>coll</td><td>mouse</td>" in row
    assert 'data-v="1000">1,000</td>' in row and 'data-v="800">800</td>' in row and 'class="pill released"' in row
    assert 'aria-sort="none"><button type="button">cells in</button>' in html
    assert "ds-table" in HOME_JS and HOME_JS in html


def test_navigator_groups_by_collection_with_status_dots(tmp_path):
    root, _ = make_run(tmp_path)
    html = _navigator_html({"coll-Organ": root, "Loose": tmp_path / "nowhere"}, tmp_path / "registry.json")
    assert '<details class="group"><summary>coll<span class="gn">1 done</span></summary>' in html
    assert '<details class="group"><summary>other<span class="gn"><span class="st failed">1 failed</span></span></summary>' in html
    assert '<span class="dot released" title="released"></span><span class="nm">Organ</span><span class="cells">800</span>' in html
    assert '<span class="dot failed" title="missing on disk"></span><span class="nm">Loose</span>' in html
    assert 'id="nav-q"' in html and 'id="nav-sort"' in html and 'id="home-item"' in html and 'id="sb-resizer"' in html
    assert '<span id="nav-n">2</span>' in html


def test_collection_and_display_name(tmp_path):
    root, _ = make_run(tmp_path)
    assert collection_of(root) == "coll" and display_name(root) == "Organ"
    assert collection_of(tmp_path) == "" and display_name(tmp_path) == tmp_path.name


def test_status_colours_are_one_set_of_tokens():
    for cls in ("released", "running", "failed", "neutral"):
        assert f".{cls}," in CSS or f".{cls}{{" in CSS
    assert "--ok:" in CSS and "--run:" in CSS and "--bad:" in CSS and "--none:" in CSS
    assert ".pill{" in CSS and "var(--st" in CSS[CSS.index(".pill{"):CSS.index(".pill::before")]
    assert "font:var(--t4)/1.5" in CSS and "max-width:1200px" in CSS


@pytest.mark.skipif(not shutil.which("node"), reason="node not on PATH")
@pytest.mark.parametrize("name,js", [("nav", NAV_JS), ("home", HOME_JS), ("sankey", "const SANKEY_DATA = null;" + SANKEY_JS), ("umap", UMAP_JS)])
def test_every_inline_script_parses(tmp_path, name, js):
    f = tmp_path / f"{name}.js"
    f.write_text(js)
    subprocess.run(["node", "--check", str(f)], check=True)

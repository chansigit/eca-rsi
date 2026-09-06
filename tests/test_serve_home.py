from pathlib import Path

from ecarsi.serve import _home_html, _navigator_html


def test_home_html_renders_stats_and_no_dataset_frame():
    html = _home_html({})
    assert "<title>ecarsi serve</title>" in html
    assert "0" in html  # zero datasets
    assert "iframe" not in html


def test_navigator_includes_home_item_and_sort_control():
    html = _navigator_html({}, Path("/tmp/registry.json"))
    assert 'id="home-item"' in html
    assert 'id="nav-sort"' in html
    assert 'id="sb-resizer"' in html

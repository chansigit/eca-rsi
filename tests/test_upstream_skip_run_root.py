"""A mirrored ECA-RSI run root inside the ECA-PP source dir is not undeclared input."""
import json
from pathlib import Path

from ecarsi import organize


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "Intestine"
    (src / "standardize").mkdir(parents=True)
    (src / "standardize" / "standardized.h5ad").write_bytes(b"h5")
    (src / "standardize" / "result.json").write_text("{}")
    return src


def test_nested_run_root_is_pruned(tmp_path):
    src = _source(tmp_path)
    rsi = src / "rsi"
    (rsi / "organize").mkdir(parents=True)
    (rsi / "organize" / "manifest.json").write_text(json.dumps({"state": "complete"}))
    (rsi / "units" / "intestine" / "input").mkdir(parents=True)
    (rsi / "units" / "intestine" / "input" / "organized.h5ad").write_bytes(b"h5")
    (rsi / "units" / "intestine" / "release").mkdir()
    (rsi / "units" / "intestine" / "release" / "final.h5ad").write_bytes(b"h5")
    units, violations = organize.find_ecapp_units(tmp_path)
    assert [u["name"] for u in units] == ["Intestine"]
    assert violations == []


def test_plain_stray_h5ad_is_still_undeclared(tmp_path):
    src = _source(tmp_path)
    (src / "rsi").mkdir()
    (src / "rsi" / "final.h5ad").write_bytes(b"h5")  # no organize/manifest.json: not a run root
    _, violations = organize.find_ecapp_units(tmp_path)
    assert violations == [src / "rsi" / "final.h5ad"]

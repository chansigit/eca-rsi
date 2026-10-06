"""Public downstream APIs and numerical logging remain auditable."""

import warnings

import pytest

import msp.api as api
import msp.deg_logging as deg_logging


def test_deg_warning_summary_preserves_other_warnings_and_results(monkeypatch, caplog):
    result = object()

    def rank(*args, **kwargs):
        for _ in range(8):
            warnings.warn_explicit(
                "divide by zero encountered in log2",
                RuntimeWarning,
                "/env/scanpy/tools/_rank_genes_groups.py",
                400,
                module="scanpy.tools._rank_genes_groups",
            )
        warnings.warn("different numerical issue", RuntimeWarning, stacklevel=2)
        return result

    monkeypatch.setattr(deg_logging.sc.tl, "rank_genes_groups", rank)
    with pytest.warns(RuntimeWarning, match="different numerical issue") as recorded:
        assert deg_logging.rank_genes_groups(None) is result
    assert len(recorded) == 1
    assert "8 Scanpy log-fold-change warnings" in caplog.text
    assert "non-finite values remain" in caplog.text


def test_deg_errors_propagate(monkeypatch):
    def rank(*args, **kwargs):
        raise ValueError("invalid input")

    monkeypatch.setattr(deg_logging.sc.tl, "rank_genes_groups", rank)
    with pytest.raises(ValueError, match="invalid input"):
        deg_logging.rank_genes_groups(None)


def test_merge_components():
    entries = {"0": {"merge_target": "1"}, "1": {"merge_target": None}}
    assert api.components(entries) == {"0": ["0", "1"], "1": ["0", "1"]}



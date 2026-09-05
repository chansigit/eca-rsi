import os
import stat

import anndata as ad
import numpy as np
import pytest

from osp._io import _atomic_replace, atomic_write_h5ad, atomic_write_json


def test_atomic_replace_preserves_previous_file_on_failure(tmp_path):
    target = tmp_path / "result.txt"
    target.write_text("complete", encoding="utf-8")

    def fail(part):
        part.write_text("partial", encoding="utf-8")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        _atomic_replace(target, fail)

    assert target.read_text(encoding="utf-8") == "complete"
    assert not list(tmp_path.glob("*.part"))


def test_atomic_json_and_h5ad_round_trip(tmp_path):
    json_path = tmp_path / "result.json"
    atomic_write_json(json_path, {"label": "细胞", "count": 2})
    assert json_path.read_text(encoding="utf-8").endswith("\n")
    assert "细胞" in json_path.read_text(encoding="utf-8")

    source = ad.AnnData(np.arange(6).reshape(2, 3))
    h5ad_path = tmp_path / "result.h5ad"
    atomic_write_h5ad(source, h5ad_path)
    restored = ad.read_h5ad(h5ad_path)
    assert restored.shape == source.shape
    assert not list(tmp_path.glob("*.part"))


def test_atomic_json_rejects_non_standard_nan_without_replacing_file(tmp_path):
    path = tmp_path / "result.json"
    path.write_text('{"status": "old"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="JSON compliant"):
        atomic_write_json(path, {"value": float("nan")})
    assert path.read_text(encoding="utf-8") == '{"status": "old"}\n'


def test_atomic_writes_honor_the_umask_instead_of_mkstemp_private_mode(tmp_path):
    previous = os.umask(0o022)
    try:
        path = tmp_path / "shared.json"
        atomic_write_json(path, {"ok": True})
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644

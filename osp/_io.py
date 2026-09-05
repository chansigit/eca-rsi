"""Small atomic-write helpers for OSP's durable contract files.

Each writer creates a sibling temporary file and replaces the destination only
after the write succeeds. This keeps an interrupted run from leaving a
truncated file that an outer resume loop could mistake for a completed step.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path


def _default_file_mode() -> int:
    """The mode a plainly created file would get under the current umask."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _atomic_replace(target, writer: Callable[[Path], None]) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=target.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        writer(tmp)
        # mkstemp creates private (0600) files; the replaced destination
        # should look like any other file the user writes, so collaborators
        # with group access can still read the outputs.
        os.chmod(tmp, _default_file_mode())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path, text: str) -> None:
    """Write UTF-8 text without exposing a partial destination file."""

    def write(tmp: Path) -> None:
        tmp.write_text(text, encoding="utf-8")

    _atomic_replace(path, write)


def atomic_write_json(path, value) -> None:
    """Write readable UTF-8 JSON atomically."""
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def atomic_write_dataframe_csv(frame, path, **kwargs) -> None:
    """Write a pandas-like table as CSV without exposing a partial file."""

    def write(tmp: Path) -> None:
        frame.to_csv(tmp, **kwargs)

    _atomic_replace(path, write)


def atomic_write_h5ad(adata, path) -> None:
    """Write and reopen an H5AD before atomically replacing the destination."""

    def write(tmp: Path) -> None:
        import anndata as ad

        adata.write_h5ad(tmp)
        check = ad.read_h5ad(tmp, backed="r")
        try:
            if check.shape != adata.shape:
                raise OSError(f"H5AD validation shape mismatch: wrote {check.shape}, expected {adata.shape}")
        finally:
            check.file.close()

    _atomic_replace(path, write)

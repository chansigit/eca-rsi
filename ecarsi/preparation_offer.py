"""Cheap, read-only hints for continuing confirmed OSP work ahead of annotation.

The offer is advisory. ``prepare_osp`` rechecks mapping, identities and file
hashes under its writer lock before it computes or reuses a sample.
"""
from __future__ import annotations

from pathlib import Path

from .run_state import read_json


def _read(path: Path) -> dict:
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _compute_receipt(directory: Path, identity: str) -> bool:
    receipt = _read(directory / "compute_state.json")
    files = receipt.get("files")
    if receipt.get("identity") != identity or not isinstance(files, dict) or not files:
        return False
    for name, recorded in files.items():
        if not isinstance(name, str) or Path(name).name != name or not isinstance(recorded, dict):
            return False
        try:
            if (directory / name).stat().st_size != recorded["size"]:
                return False
        except (OSError, KeyError, TypeError):
            return False
    return True


def offer(output: str | Path) -> dict[str, int | None]:
    """Count computed/unannotated and missing samples from confirmed manifests.

    No expression matrix or output content is read. The minimum cell count
    helps a scheduler check whether one more OSP task fits a live worker.
    """
    root = Path(output).resolve()
    prepared = remaining = 0
    minimum = None
    for manifest_path in root.glob("units/*/persample/manifest.json"):
        manifest = _read(manifest_path)
        if manifest.get("schema_version") != 2 or not manifest.get("mapping_identity"):
            continue
        for sample in manifest.get("samples", []):
            if not isinstance(sample, dict):
                continue
            identity, cells = sample.get("identity"), sample.get("n_cells")
            if not isinstance(identity, str) or type(cells) is not int or cells < 1:
                continue
            directory = Path(sample.get("dir", "")).resolve()
            if not directory.is_relative_to(root):
                continue
            state = _read(directory / "run_state.json")
            if state.get("identity") == identity:
                if state.get("state") == "complete" and state.get("exit_code") == 0:
                    continue
                if state.get("state") == "failed" and state.get("failure_kind") in {
                    "qc_zero_survivors", "qc_too_few_survivors"
                }:
                    continue
            if _compute_receipt(directory, identity):
                prepared += 1
            else:
                remaining += 1
                minimum = cells if minimum is None else min(minimum, cells)
    return {"prepared_count": prepared, "remaining_count": remaining,
            "min_missing_cells": minimum}

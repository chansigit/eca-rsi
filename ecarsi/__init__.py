"""ecarsi: the control plane, stage programs and Periscope of ECA-RSI (docs/OVERVIEW.md)."""

from __future__ import annotations


def model() -> str:
    """Model for every agent call in this package: MODEL env, else the
    HARNESS-appropriate default — a bare model name is never portable
    across backends. Same rule as osp/msp/zmip's harness.default_model()."""
    from harness_bridge import default_model

    return default_model()


def version() -> dict | None:
    """The published version this code runs from (decision 0019): version.json beside the packages, written by
    ops/publish-version.sh, plus "root", the version's directory. None for a checkout or an image snapshot."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    path = root / "version.json"
    return dict(json.loads(path.read_text()), root=str(root)) if path.is_file() else None


def task_queue() -> str:
    """The Temporal task queue this code serves: one per published version, the old shared one otherwise."""
    v = version()
    return "ecarsi-" + v["name"] if v else "ecarsi-durable-v2"


def current_queue() -> str | None:
    """The queue of the version `versions/current` points at (ops/set-current.sh), beside this version's directory;
    None for a checkout or an image snapshot, which are no version. Every child workflow starts there (0022)."""
    import os
    from pathlib import Path

    v = version()
    if not v:
        return None
    try:
        return "ecarsi-" + os.path.basename(os.readlink(Path(v["root"]).parent / "current"))
    except OSError:
        return None

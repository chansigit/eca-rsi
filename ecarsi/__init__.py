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

    import json

    v = version()
    if not v:
        return None
    try:
        current = Path(v["root"]).parent / "current"
        name = os.path.basename(os.readlink(current))
        published = json.loads((current / "version.json").read_text()).get("published", "")
    except (OSError, ValueError):
        return None
    # Never backwards: a gate runs on a candidate newer than current, and must stay there (2026-10-08: a gate on
    # 3fb453d moved its unit to the older current version and tested nothing).
    if name == v["name"] or published <= v.get("published", ""):
        return None
    return "ecarsi-" + name

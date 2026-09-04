"""Cross-repository compatibility checks after harness extraction.

Harness implementation tests live in agent-harness-bridge. The legacy public
modules remain thin identity-preserving shims. The unrelated resources.py
copy is still checked here until resource scheduling is extracted separately.
"""

from __future__ import annotations

import os
from pathlib import Path

import harness_bridge
from harness_bridge import harness as bridge_harness

HERE = Path(__file__).resolve().parent.parent
LEGACY_SHIM_EXPORTS = {
    "DEFAULT_BACKEND",
    "DEFAULT_WALL_MINUTES",
    "LIMIT_PATTERN",
    "MAX_TIMEOUT_ATTEMPTS",
    "MAX_TRANSIENT_ATTEMPTS",
    "TRANSIENT_BACKOFF_SECONDS",
    "TRANSIENT_PATTERN",
    "ToolHandler",
}


def sibling(name: str) -> Path:
    env = os.environ.get("ECA_SIBLINGS")
    if env:
        for directory in env.split(":"):
            if Path(directory).name.startswith(name):
                return Path(directory)
    suffix = HERE.name[len("eca-rsi"):] if HERE.name.startswith("eca-rsi") else ""
    for candidate in (HERE.parent / f"{name}{suffix}", HERE.parent / name):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(f"no sibling checkout for {name} next to {HERE} (set ECA_SIBLINGS)")


def test_legacy_harness_modules_reexport_shared_objects():
    from ecarsi import harness as ecarsi_harness
    from msp import harness as msp_harness
    from osp import harness as osp_harness

    for shim in (ecarsi_harness, msp_harness, osp_harness):
        for name in harness_bridge.__all__:
            assert getattr(shim, name) is getattr(harness_bridge, name), name
        for name in LEGACY_SHIM_EXPORTS:
            assert getattr(shim, name) is getattr(bridge_harness, name), name
        assert set(shim.__all__) == set(harness_bridge.__all__) | LEGACY_SHIM_EXPORTS


def test_no_project_keeps_private_harness_implementations():
    for package_dir in (HERE / "ecarsi", sibling("msp") / "msp", sibling("osp") / "osp"):
        assert not list(package_dir.glob("_harness_*.py")), package_dir


def test_resource_copies_still_match():
    assert (HERE / "ecarsi" / "resources.py").read_bytes() == (
        sibling("msp") / "msp" / "resources.py"
    ).read_bytes()

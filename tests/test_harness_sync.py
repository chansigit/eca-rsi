"""Cross-repository compatibility checks after harness extraction.

Harness implementation tests live in agent-harness-bridge. The legacy public
modules remain thin identity-preserving shims. The unrelated resources.py
copy is still checked here until resource scheduling is extracted separately.
"""

from __future__ import annotations

from tests.bridge_contract import BRIDGE_LEGACY_API

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


def test_legacy_harness_modules_reexport_shared_objects():
    # msp.harness was removed in msp 0.4 and ecarsi.harness in ecarsi 0.4.3; osp still carries its shim.
    from osp import harness as osp_harness

    for shim in (osp_harness,):
        for name in BRIDGE_LEGACY_API:
            assert getattr(shim, name) is getattr(harness_bridge, name), name
        for name in LEGACY_SHIM_EXPORTS:
            assert getattr(shim, name) is getattr(bridge_harness, name), name
        assert BRIDGE_LEGACY_API | LEGACY_SHIM_EXPORTS <= set(shim.__all__)


def test_no_project_keeps_private_harness_implementations():
    for package_dir in (HERE / "ecarsi", HERE / "msp", HERE / "osp"):
        assert not list(package_dir.glob("_harness_*.py")), package_dir


def test_resource_copies_still_match():
    assert (HERE / "ecarsi" / "resources.py").read_bytes() == (HERE / "msp" / "resources.py").read_bytes()

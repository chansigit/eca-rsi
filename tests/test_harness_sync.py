"""The harness lives in harness_bridge only: no package keeps a private copy of its implementation."""

from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_no_project_keeps_private_harness_implementations():
    for package_dir in (HERE / "ecarsi", HERE / "osp", HERE / "msp", HERE / "zmip"):
        assert not list(package_dir.glob("_harness_*.py")), package_dir

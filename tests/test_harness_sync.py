"""The harness modules live as byte-identical copies in msp (source of
truth), osp and ecarsi — no shared package, per the independent-repos
convention — and resources.py in msp and ecarsi. This test is what keeps
them from drifting: edit msp's, cp to the others, run this.

    python -m tests.test_harness_sync            # from the eca-rsi repo root
    pytest tests/test_harness_sync.py

Sibling repos are found next to this one (../msp, ../osp — or the same
names with this checkout's suffix, e.g. eca-rsi-harness-deepseek →
msp-harness-deepseek); override with ECA_SIBLINGS=<msp dir>:<osp dir>.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
COPIES = {
    "harness.py": ("msp", "osp", "ecarsi"),
    "_harness_claude.py": ("msp", "osp", "ecarsi"),
    "_harness_deepseek.py": ("msp", "osp", "ecarsi"),
    "resources.py": ("msp", "ecarsi"),
}


def sibling(name: str) -> Path:
    env = os.environ.get("ECA_SIBLINGS")
    if env:
        for d in env.split(":"):
            if Path(d).name.startswith(name):
                return Path(d)
    suffix = HERE.name[len("eca-rsi"):] if HERE.name.startswith("eca-rsi") else ""
    for cand in (HERE.parent / f"{name}{suffix}", HERE.parent / name):
        if cand.is_dir():
            return cand
    raise FileNotFoundError(f"no sibling checkout for {name} next to {HERE} (set ECA_SIBLINGS)")


def pkg_dir(pkg: str) -> Path:
    return HERE / "ecarsi" if pkg == "ecarsi" else sibling(pkg) / pkg


def drift() -> list[str]:
    out = []
    for fname, pkgs in COPIES.items():
        src = (pkg_dir(pkgs[0]) / fname).read_bytes()
        for pkg in pkgs[1:]:
            path = pkg_dir(pkg) / fname
            if not path.is_file():
                out.append(f"{path} missing")
            elif path.read_bytes() != src:
                out.append(f"{path} differs from {pkg_dir(pkgs[0]) / fname}")
    return out


def test_harness_copies_identical():
    assert drift() == []


if __name__ == "__main__":
    problems = drift()
    for p in problems:
        print("DRIFT:", p)
    print("harness copies identical" if not problems else f"{len(problems)} drifted file(s)")
    sys.exit(1 if problems else 0)

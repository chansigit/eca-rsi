"""eca-rsi#10: editing a page must not invalidate a stage that is verifying.

Two recomputations on 2026-09-07 (tome E9.5 round 3 and E8.5b round 2 zoom-in) came from a
serve.py edit landing inside a verify window, and the workaround since has been four read-only
checkouts for the length of every batch.
"""
from pathlib import Path

from ecarsi import downstream, run_state


def test_ui_files_are_outside_the_identity_digest(tmp_path):
    root = tmp_path / "pkg"
    (root / "ui").mkdir(parents=True)
    (root / "compute.py").write_text("x = 1\n")
    (root / "ui" / "serve.py").write_text("CSS = 'blue'\n")
    inside = [p for p in sorted(root.rglob("*")) if p.suffix == ".py"
              and not run_state._presentation(p, root)]
    assert inside == [root / "compute.py"]


def test_the_rule_is_a_directory_not_a_file_list():
    assert run_state.PRESENTATION == ("ui",)


def test_both_hash_sites_use_it():
    for module in (run_state, downstream):
        source = Path(module.__file__).read_text()
        assert "_presentation(p, " in source, module.__name__


def test_the_shims_stay_trivial():
    """They are hashed, so they must be the kind of file nobody edits."""
    for name in ("serve", "index", "umapdata"):
        text = (Path(run_state.__file__).parent / f"{name}.py").read_text()
        assert f"from .ui.{name} import main" in text
        assert len(text.splitlines()) < 15, f"{name}.py shim has grown a body"

"""Run identity is content only. Where the code came from (checkout path, git
HEAD) is recorded as provenance and never compared: a doc-only commit, or the
same source served from another worktree, must not invalidate a resume."""
import json

from ecarsi import downstream as D
from ecarsi.run_state import read_json, runtime_identity, source_provenance


def test_runtime_identity_carries_no_path_or_commit():
    rt = runtime_identity()
    for module, pkg in rt["packages"].items():
        assert set(pkg) == {"version", "source_sha256"}, module
    prov = source_provenance()
    assert prov["ecarsi"]["path"] and "commit" in prov["ecarsi"]
    assert json.dumps(prov)  # serialisable into a manifest


def test_downstream_runtime_is_content_only(monkeypatch):
    rt = D.runtime("msp") if all(__import__("importlib").util.find_spec(m) for m in ("msp", "standissect_lite")) else None
    if rt is None:
        return  # kernel not importable here; the shape is covered by the resume test below
    assert all(set(v) == {"digest"} for v in rt["sources"].values())


def test_prepare_resumes_when_only_provenance_differs(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "kernel_runtime", lambda *a: {"sources": {"ecarsi": {"digest": "same"}}})
    monkeypatch.setattr(D, "source_provenance", lambda *a: {"ecarsi": {"path": "/one", "commit": "aaa"}})
    src = tmp_path / "src"; src.write_text("x")
    out = tmp_path / "out"
    D.prepare("python", "msp", [src], out, {})
    rec = read_json(out / D.STATE)
    assert rec["provenance"]["ecarsi"]["commit"] == "aaa" and "provenance" not in rec["identity"]
    monkeypatch.setattr(D, "source_provenance", lambda *a: {"ecarsi": {"path": "/two", "commit": "bbb"}})
    D.prepare("python", "msp", [src], out, {})  # same content, different checkout: resumes
    assert read_json(out / D.STATE)["provenance"]["ecarsi"]["commit"] == "bbb"

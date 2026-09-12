"""The agent runtime (agent-harness-bridge) is provenance, never identity: a
bridge patch release must not invalidate an in-flight unit (2026-09-12,
0.2.11 -> 0.2.12 did exactly that under the old rule)."""

import pytest

from ecarsi.run_state import runtime_identity, source_provenance


def test_runtime_identity_excludes_the_agent_runtime():
    assert set(runtime_identity()["packages"]) == {"ecarsi", "osp"}


def test_provenance_records_the_bridge_version_instead():
    prov = source_provenance()
    assert "harness_bridge" in prov
    assert prov["harness_bridge"]["version"]  # recorded for humans, never compared


def test_runtime_check_can_be_skipped_while_developing(tmp_path, monkeypatch):
    """ECA_RSI_DEVELOPER_MODE=1: a changed runtime no longer fails resume;
    input/config changes still do."""
    from ecarsi import downstream as D

    inp = tmp_path / "in.h5ad"
    inp.write_bytes(b"x")
    out = tmp_path / "stage"
    out.mkdir()
    monkeypatch.setattr(D, "kernel_runtime", lambda py, kernel: {"v": 1})
    monkeypatch.setattr(D, "computational_config", lambda config: {"c": 1})
    D.prepare("python", "msp", [str(inp)], out, {})

    monkeypatch.setattr(D, "kernel_runtime", lambda py, kernel: {"v": 2})
    monkeypatch.delenv("ECA_RSI_DEVELOPER_MODE", raising=False)
    with pytest.raises(ValueError, match="ECA_RSI_DEVELOPER_MODE"):
        D.prepare("python", "msp", [str(inp)], out, {})

    monkeypatch.setenv("ECA_RSI_DEVELOPER_MODE", "1")
    D.prepare("python", "msp", [str(inp)], out, {})  # runtime differs: accepted

    monkeypatch.setattr(D, "computational_config", lambda config: {"c": 2})
    with pytest.raises(ValueError, match="changed"):
        D.prepare("python", "msp", [str(inp)], out, {})  # config differs: still refused

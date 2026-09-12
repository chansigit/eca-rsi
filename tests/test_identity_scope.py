"""The agent runtime (agent-harness-bridge) is provenance, never identity: a
bridge patch release must not invalidate an in-flight unit (2026-09-12,
0.2.11 -> 0.2.12 did exactly that under the old rule)."""

from ecarsi.run_state import runtime_identity, source_provenance


def test_runtime_identity_excludes_the_agent_runtime():
    assert set(runtime_identity()["packages"]) == {"ecarsi", "osp"}


def test_provenance_records_the_bridge_version_instead():
    prov = source_provenance()
    assert "harness_bridge" in prov
    assert prov["harness_bridge"]["version"]  # recorded for humans, never compared

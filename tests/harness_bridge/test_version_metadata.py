"""Keep the public version consistent with the installed release metadata."""
from importlib.metadata import version

import harness_bridge


def test_public_version_matches_distribution_metadata():
    assert harness_bridge.__version__ == version("agent-harness-bridge")

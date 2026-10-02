import os

import pytest


@pytest.fixture(autouse=True)
def _restore_environment():
    """Model turns run in-process in some tests, and a model turn configures its process environment
    (agent.dispatch.configure: AGENT_MODEL_POOL, the provider URL). A runner or pool task owns its
    process, so that is right in production; here it would leak into every later test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)

import os

import pytest

# Strict mode (ecarsi.degraded): a step that would only degrade a real run fails its test. Set at import,
# so subprocesses and pool tasks the tests start inherit it; a test of the lenient path deletes it.
os.environ["ECARSI_STRICT"] = "1"


@pytest.fixture(autouse=True)
def _restore_environment():
    """Model turns run in-process in some tests, and a model turn configures its process environment
    (agent.dispatch.configure: AGENT_MODEL_POOL, the provider URL). A runner or pool task owns its
    process, so that is right in production; here it would leak into every later test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)

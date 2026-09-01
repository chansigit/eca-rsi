"""ecarsi — Agent-SDK-based tooling for the eca-rsi curation loop."""

import os

DEFAULT_MODEL = "claude-sonnet-5"


def model() -> str:
    """Model for every agent call in this package: MODEL env or sonnet-5."""
    return os.environ.get("MODEL", DEFAULT_MODEL)

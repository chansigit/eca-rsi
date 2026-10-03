"""ecarsi: the control plane, stage programs and Periscope of ECA-RSI (docs/OVERVIEW.md)."""

from __future__ import annotations


def model() -> str:
    """Model for every agent call in this package: MODEL env, else the
    HARNESS-appropriate default — a bare model name is never portable
    across backends. Same rule as osp/msp/zmip's harness.default_model()."""
    from harness_bridge import default_model

    return default_model()

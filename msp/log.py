"""Progress output for msp goes through ``logging`` (the ``msp`` logger family).

Library entry points call :func:`ensure`: it attaches a stdout handler, one wall-clock-stamped line per record,
only when no handler is reachable, so a caller that configured logging keeps its own.
"""

from __future__ import annotations

import logging
import sys


def ensure() -> None:
    """Attach the default stdout handler unless one is already reachable."""
    logger = logging.getLogger("msp")
    if not logger.hasHandlers():
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

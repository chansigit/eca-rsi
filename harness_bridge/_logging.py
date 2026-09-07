"""Progress output for the bridge and the pipelines built on it.

Every bridge module logs through ``logging.getLogger(__name__)``, so all of
its lines belong to the ``harness_bridge`` logger family. Applications call
:func:`configure_logging` once in their CLI entry point to route those lines,
plus their own logger families, to one stream with one format::

    from harness_bridge import configure_logging
    configure_logging("msp")            # harness_bridge + msp -> stdout

Each record is flushed as it is written, so Slurm and ``tee`` logs stay live
even when stdout is a pipe. Library entry points that want the old
print-like behaviour when nobody configured logging call
:func:`ensure_logging` instead; it attaches the default handler only when
no handler is reachable for the logger.
"""

from __future__ import annotations

import logging
import sys
from typing import IO

DEFAULT_FORMAT = "%(asctime)s %(message)s"
DEFAULT_DATEFMT = "%m-%d %H:%M:%S"  # every stage/tool line carries a wall-clock stamp, so durations fall out of any log
_MARK = "_harness_bridge_handler"


class _FlushingHandler(logging.StreamHandler):
    """``StreamHandler`` already flushes per record; the subclass exists so
    handlers installed by :func:`configure_logging` can be told apart from
    the application's own."""

    def __init__(self, stream: IO[str] | None = None):
        super().__init__(stream or sys.stdout)
        setattr(self, _MARK, True)


def _attach(names, level, stream, fmt) -> logging.Handler:
    handler = _FlushingHandler(stream)
    handler.setFormatter(logging.Formatter(fmt, DEFAULT_DATEFMT))
    for name in dict.fromkeys(names):
        logger = logging.getLogger(name)
        for old in list(logger.handlers):
            if getattr(old, _MARK, False):
                logger.removeHandler(old)
        logger.addHandler(handler)
        logger.setLevel(level)
    return handler


def configure_logging(
    *names: str,
    level: int | str = logging.INFO,
    stream: IO[str] | None = None,
    fmt: str = DEFAULT_FORMAT,
) -> logging.Handler:
    """Send the ``harness_bridge`` logger family and every family in
    ``names`` to ``stream`` (default: ``sys.stdout``) at ``level``.

    Idempotent: calling it again replaces the handler it installed earlier,
    so a CLI that configures logging and a library entry point that calls
    :func:`ensure_logging` never emit a line twice. Loggers keep propagating
    to the root logger, so ``pytest``'s ``caplog`` and application-level
    handlers still see the records."""
    return _attach(("harness_bridge", *names), level, stream, fmt)


def ensure_logging(*names: str) -> None:
    """Attach the default stdout handler to ``harness_bridge`` and ``names``
    unless a handler is already reachable for that logger (the application
    configured logging, or the root logger has a handler)."""
    missing = [name for name in dict.fromkeys(("harness_bridge", *names)) if not logging.getLogger(name).hasHandlers()]
    if missing:
        _attach(missing, logging.INFO, None, DEFAULT_FORMAT)

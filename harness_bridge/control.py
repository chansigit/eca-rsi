"""Cooperative pause at host-defined safe points; no agent interruption."""

import functools
import json
import logging
import os
import signal
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


class PauseRequested(SystemExit):
    def __init__(self):
        super().__init__(3)


def pause_requested(stage=None):
    marker = os.environ.get("ECA_RSI_PAUSE_FILE")
    if marker and Path(marker).exists():
        return True
    control = os.environ.get("ECA_RSI_CONTROL")
    if control:
        try:
            value = json.loads(Path(control).read_text())
            return isinstance(value, dict) and (
                value.get("pause") is True
                or stage in ("crosssample", "zoomin")
                and value.get("pause_after_stage") == stage
            )
        except (OSError, ValueError):
            pass  # An editor may be replacing the control file; check again at the next boundary.
    return False


def safe_point(stage=None):
    if pause_requested(stage):
        log.warning(
            "== safely paused%s; exit 3, rerun the same command to resume",
            f" after {stage}" if stage else "",
        )
        raise PauseRequested()


@contextmanager
def pause_signals():
    """Share one request file with descendants; a new invocation gets a fresh file."""
    previous_marker = os.environ.get("ECA_RSI_PAUSE_FILE")
    previous_control = os.environ.get("ECA_RSI_CONTROL")
    temporary = (
        tempfile.TemporaryDirectory(prefix="eca-pause-")
        if not previous_marker
        else None
    )
    marker = Path(previous_marker or str(Path(temporary.name) / "requested"))
    os.environ["ECA_RSI_PAUSE_FILE"] = str(marker)
    main_thread = threading.current_thread() is threading.main_thread()

    def request(signum, frame):
        marker.touch()

    previous_term = signal.signal(signal.SIGTERM, request) if main_thread else None
    try:
        yield
    finally:
        if main_thread:
            signal.signal(signal.SIGTERM, previous_term)
        for key, value in (
            ("ECA_RSI_PAUSE_FILE", previous_marker),
            ("ECA_RSI_CONTROL", previous_control),
        ):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if temporary:
            temporary.cleanup()


def pausable(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with pause_signals():
            try:
                return function(*args, **kwargs)
            except PauseRequested:
                return 3

    return wrapped

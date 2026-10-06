"""configure_logging / ensure_logging: one flushed line per record, idempotent."""

import io
import logging
import re

import pytest

from harness_bridge import configure_logging, ensure_logging
from harness_bridge._logging import _MARK


@pytest.fixture(autouse=True)
def _clean_loggers():
    names = ("harness_bridge", "harness_bridge.harness", "app_x")
    yield
    for name in names:
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            if getattr(h, _MARK, False):
                logger.removeHandler(h)
        logger.setLevel(logging.NOTSET)


def test_configure_routes_bridge_and_named_families_to_one_stream():
    buf = io.StringIO()
    configure_logging("app_x", stream=buf)
    logging.getLogger("harness_bridge.harness").info("== [t] hello")
    logging.getLogger("app_x.sub").info("== step")
    logging.getLogger("app_x.sub").debug("hidden")
    lines = buf.getvalue().splitlines()
    # every line carries a "MM-DD HH:MM:SS" stamp so stage durations can be read off any log
    assert [re.sub(r"^\d\d-\d\d \d\d:\d\d:\d\d ", "", l) for l in lines] == ["== [t] hello", "== step"]
    assert all(re.match(r"^\d\d-\d\d \d\d:\d\d:\d\d ", l) for l in lines)


def test_configure_is_idempotent_and_replaces_its_own_handler():
    first, second = io.StringIO(), io.StringIO()
    configure_logging("app_x", stream=first)
    configure_logging("app_x", stream=second)
    logging.getLogger("app_x").info("once")
    assert first.getvalue() == ""
    assert second.getvalue().endswith(" once\n")
    marked = [h for h in logging.getLogger("app_x").handlers if getattr(h, _MARK, False)]
    assert len(marked) == 1


def test_ensure_attaches_only_where_nothing_is_reachable(monkeypatch):
    # pytest keeps capture handlers on the root logger, which makes every
    # logger "reachable"; hide them so the test sees a bare interpreter.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    buf = io.StringIO()
    configure_logging(stream=buf)  # bridge configured by the application
    ensure_logging("app_x")  # app_x is not: gets the default handler, bridge untouched
    bridge = logging.getLogger("harness_bridge")
    assert [h for h in bridge.handlers if getattr(h, _MARK, False)][0].stream is buf
    assert any(getattr(h, _MARK, False) for h in logging.getLogger("app_x").handlers)
    ensure_logging("app_x")  # second call: still one handler each
    assert sum(getattr(h, _MARK, False) for h in logging.getLogger("app_x").handlers) == 1


def test_records_still_propagate_to_root(caplog):
    configure_logging("app_x", stream=io.StringIO())
    with caplog.at_level(logging.INFO, logger="app_x"):
        logging.getLogger("app_x").info("seen by caplog")
    assert "seen by caplog" in caplog.text

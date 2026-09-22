"""The monitor may only read what the owner of a fact published about itself.

This is the one rule the monitoring surface obeys, written down so that it is checked rather than
remembered. Periscope lagged behind reality for weeks because each fact it showed arrived by a
different mechanism, and two of those mechanisms were "look at the files someone else happened to
write and guess" -- which cannot distinguish a process working quietly from a process that died,
because neither writes anything. The rule that removes that whole class of bug:

  1. Ownership   - every fact is published by the node that owns it. The monitor derives nothing.
  2. Self-dating - every published record carries when it was observed. No record means "unknown",
                   never a guessed status.
  3. One-way     - owners write, the monitor reads. It opens no connection to a scheduler, so no
                   change to it can reach the computation. This is structural, not a convention.

`ecarsi/ui/` is the monitoring surface, and it is also the directory `run_state.PRESENTATION`
excludes from the computation's identity digest -- the same boundary serves both purposes, which is
why the boundary is a directory and these tests police it. Operator commands (`ecarsi.observatory`
status/releases/tokens) are on the other side of it and may talk to Temporal directly: a human
running a report is not a web page.
"""
import ast
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "ecarsi" / "ui"
SOURCES = sorted(p for p in UI.rglob("*.py") if "__pycache__" not in p.parts)

#: Clients that reach a scheduler or a database. Importing one is how the monitor would stop being
#: a reader; a page that can call the control plane can also hang it.
SCHEDULER_CLIENTS = {"temporalio", "hyperqueue", "psycopg", "psycopg2", "sqlalchemy", "asyncpg"}
#: Modules inside this repo that hold those clients. `ecarsi.control.temporal` resolves a service
#: endpoint; the monitor must be told the answer, not go and work it out.
CONTROL_MODULES = {"ecarsi.control", "ecarsi.control.temporal", "ecarsi.warm_pool.backend"}
#: Making an outbound connection, by any of the usual spellings.
CONNECT_CALLS = {"create_connection", "connect", "urlopen", "getaddrinfo"}


def imported(tree):
    """(module, alias) pairs, with relative imports resolved against ecarsi.ui."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                       # from . / .. -> ecarsi.ui / ecarsi
                prefix = "ecarsi.ui" if node.level == 1 else "ecarsi"
                base = f"{prefix}.{base}" if base else prefix
            yield base
            for alias in node.names:
                yield f"{base}.{alias.name}"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_the_monitor_holds_no_client_for_anything_it_watches(path):
    """Rule 3, statically: the surface cannot import a scheduler or database client at all."""
    for module in imported(ast.parse(path.read_text())):
        root = module.split(".")[0]
        assert root not in SCHEDULER_CLIENTS, f"{path.name} imports {module}"
        assert module not in CONTROL_MODULES, (
            f"{path.name} imports {module}: the control plane must publish this, not be asked for it")


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_the_monitor_opens_no_connection(path):
    """Rule 3, the other half: no outbound socket, to the control plane or anywhere else.

    A liveness probe is the tempting exception -- "is the Temporal UI up?" is one connect() away --
    and it is exactly the wrong move: the control plane owns that fact and can publish it, and a
    page that probes is a page that can be made to probe in a loop."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            assert name not in CONNECT_CALLS, f"{path.name} calls {name}(): the monitor only reads files"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_the_monitor_never_writes_into_the_run_it_watches(path):
    """Rule 3, third spelling. Reading is safe at any rate; writing is how a viewer corrupts a run.

    Its own caches and the static pages it renders are not the run's state, so `ecarsi.ui.index`
    writing index.html is fine -- what is forbidden is the durable state modules, which is how a
    page would come to hold a lock or overwrite a receipt."""
    forbidden = {"ecarsi.warm_pool.state.save", "ecarsi.warm_pool.state.lock",
                 "ecarsi.agent.save", "ecarsi.control.dataset.resume_dataset"}
    for module in imported(ast.parse(path.read_text())):
        assert module not in forbidden, f"{path.name} imports {module}: the monitor is read-only"


def test_a_record_without_a_time_is_unknown_not_a_status():
    """Rule 2. The failure that started all this: a dead run writes nothing, and a monitor that
    reads absence as "still going" reports a corpse as healthy. Absence must render as unknown."""
    from ecarsi.ui.serve import ControlVerdicts

    verdicts = ControlVerdicts(Path("/nonexistent/fleet-status.json"))
    assert verdicts.of("any-run") is None            # no publisher: no opinion at all
    assert verdicts.live() is False and verdicts.age() is None

    verdicts._data = {"generated_at": 0.0, "workflows": {"dataset/x": {"status": "RUNNING"}}}
    verdicts._path = None                            # keep the injected record; _load must not clear it
    assert verdicts.age() > ControlVerdicts.FRESH
    assert verdicts.of("x") is None, "a record older than FRESH must not be believed"

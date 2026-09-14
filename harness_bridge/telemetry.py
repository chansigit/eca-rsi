"""Opt-in, content-free agent activity journal shared across cluster processes."""
from __future__ import annotations

import contextvars
import fcntl
import json
import os
import socket
import tempfile
import time
from pathlib import Path

CURRENT_CALL = contextvars.ContextVar("agent_bridge_call", default=None)


def directory() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_TELEMETRY_DIR") or Path.home() / ".cache/ecarsi/agent-bridge")


def enabled() -> bool:
    return bool(os.environ.get("AGENT_BRIDGE_TELEMETRY_DIR") or os.environ.get("ECA_POOL_SCHEDULER"))


def record(kind: str, call_id: str | None = None, **fields) -> None:
    """Write one small event and its bounded UI projection; never accept prompt text."""
    if not enabled():
        return
    call_id = call_id or CURRENT_CALL.get()
    if not call_id:
        return
    root = directory()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    event = {"at": time.time(), "kind": kind, "id": call_id, "host": socket.gethostname().split(".")[0],
             "pid": os.getpid()}
    event.update({key: value for key, value in fields.items() if key in {
        "label", "cwd", "harness", "model", "tokens_in", "tokens_out", "cost_usd", "error_type"}})
    for key in ("label", "cwd", "harness", "model", "error_type"):
        if key in event:
            event[key] = str(event[key])[:256]
    with (root / "state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {
            "started": 0, "succeeded": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0,
            "usage_reported": 0, "observed_model_requests": 0, "cost_usd": 0.0, "cost_reported": 0,
            "active": {}, "by_model": {}, "recent": []}
        state.setdefault("cost_usd", 0.0)
        state.setdefault("cost_reported", 0)
        active = state["active"]
        if kind == "start":
            active[call_id] = {k: event[k] for k in ("id", "at", "host", "pid", "label", "cwd")}
            active[call_id]["phase"] = "active"
            state["started"] += 1
        elif call_id in active:
            row = active[call_id]
            row["updated_at"] = event["at"]
            for key in ("harness", "model"):
                if key in event:
                    row[key] = event[key]
            if kind == "attempt":
                row["phase"] = "active"
            elif kind == "attempt_error":
                row["phase"] = "retrying"
            elif kind == "model_start":
                row["phase"] = "waiting_for_model"
                state["observed_model_requests"] += 1
            elif kind == "model_end":
                row["phase"] = "active"
            elif kind in {"success", "failure"}:
                row = active.pop(call_id)
                row.update(status=kind, finished_at=event["at"],
                           duration_seconds=round(event["at"] - row["at"], 1))
                if kind == "failure":
                    row["error_type"] = event.get("error_type", "UnknownError")
                for key in ("tokens_in", "tokens_out", "cost_usd"):
                    if event.get(key) is not None:
                        row[key] = event[key]
                state["recent"] = [row, *state["recent"][:49]]
                state["succeeded" if kind == "success" else "failed"] += 1
                model = f"{row.get('harness', 'unknown')}:{row.get('model', 'unknown')}"
                tally = state["by_model"].setdefault(model, {"succeeded": 0, "failed": 0,
                                                               "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
                tally.setdefault("cost_usd", 0.0)
                tally["succeeded" if kind == "success" else "failed"] += 1
                if event.get("cost_usd") is not None:
                    state["cost_reported"] += 1
                    state["cost_usd"] += event["cost_usd"]
                    tally["cost_usd"] += event["cost_usd"]
                if event.get("tokens_in") is not None or event.get("tokens_out") is not None:
                    state["usage_reported"] += 1
                    for key in ("tokens_in", "tokens_out"):
                        value = event.get(key) or 0
                        state[key] += value
                        tally[key] += value
        state["updated_at"] = event["at"]
        log = root / f"events-{time.strftime('%Y-%m-%d', time.gmtime(event['at']))}.jsonl"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(event, separators=(",", ":")) + "\n").encode())
        finally:
            os.close(fd)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, separators=(",", ":"))
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)


def snapshot() -> dict:
    path = directory() / "state.json"
    if not path.exists():
        return {"started": 0, "succeeded": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0,
                "usage_reported": 0, "observed_model_requests": 0, "active": [], "recent": [],
                "cost_usd": 0.0, "cost_reported": 0, "by_model": {}, "updated_at": None,
                "journal_directory": str(directory())}
    state = json.loads(path.read_text())
    now = time.time()
    state["active"] = sorted((row for row in state["active"].values() if now - row["at"] < 14 * 3600),
                             key=lambda row: row["at"], reverse=True)
    state["journal_directory"] = str(directory())
    return state

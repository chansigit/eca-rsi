"""Regression test for the 2026-09-03 "second agent call in a process has no
tools" failure: sse-starlette keeps a process-global shutdown latch
(AppStatus.should_exit) that the graceful teardown of run_agent's first
uvicorn/FastMCP server flipped for good, so every later server's SSE
responses ended immediately and dsh's mcp-client could not attach.

No model, no dsh: the dsh run is replaced by a Python MCP client handshake
from a worker thread (an external client, like dsh), repeated for several
server generations in ONE process. Like dsh's mcp-client, the fake client
also opens the server-notification GET SSE stream and keeps it open for a
moment AFTER the run returns, so the server is torn down with a live SSE
stream — the situation in which sse-starlette's watcher flips the latch.
Asserted: the latch stays False after every teardown, and uvicorn never
logs "ASGI callable returned without completing response".

    python -m tests.test_mcp_server_lifecycle    # from the eca-rsi repo root
    pytest tests/test_mcp_server_lifecycle.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

GENERATIONS = 3


def _run(hold_seconds: float = 0.0) -> list[tuple[int, str, bool]]:
    import yaml
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from sse_starlette.sse import AppStatus

    import ecarsi._harness_deepseek as H
    from ecarsi.harness import AgentIncompleteError, ToolSpec

    async def add(args):
        return {"content": [{"type": "text", "text": str(int(args["a"]) + int(args["b"]))}]}

    async def submit(args):
        return {"content": [{"type": "text", "text": "ok"}], "_submitted": args}

    tools = [ToolSpec("add", "add", {"a": int, "b": int}, add),
             ToolSpec("submit_answer", "submit", {"answer": str}, submit)]
    outcomes: list[tuple[int, str, bool]] = []

    def fake_run_sync(**kw):
        url = yaml.safe_load(open(kw["patch_path"]))[0]["insert"][0]["config"]["url"]
        handshake_done = threading.Event()
        gen = len(outcomes)
        outcomes.append((gen, "did not finish", False))

        async def go():
            async with streamablehttp_client(url) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    names = [t.name for t in (await s.list_tools()).tools]
                    res = await s.call_tool("add", {"a": 1, "b": 2})
                    outcomes[gen] = (gen, f"{len(names)} tools, add -> {res.content[0].text}", True)
                    handshake_done.set()
                    await asyncio.sleep(hold_seconds)  # stay connected through the server's teardown

        def client_thread():
            try:
                asyncio.run(go())
            except Exception as e:  # noqa: BLE001
                if not outcomes[gen][2]:
                    outcomes[gen] = (gen, f"{type(e).__name__}: {str(e)[:120]}", False)
            finally:
                handshake_done.set()

        def get_stream_thread():
            # dsh's client opens GET /mcp (server → client notifications) and
            # keeps it open until it exits; hold it through the teardown
            import httpx
            try:
                with httpx.stream("GET", url, headers={"Accept": "text/event-stream"},
                                  timeout=httpx.Timeout(30, read=hold_seconds)) as r:
                    deadline = time.time() + hold_seconds
                    for _ in r.iter_lines():
                        if time.time() > deadline:
                            break
            except Exception:  # noqa: BLE001 - the server going away is expected
                pass

        threading.Thread(target=client_thread, daemon=True).start()
        threading.Thread(target=get_stream_thread, daemon=True).start()
        handshake_done.wait(timeout=30)
        time.sleep(0.3)  # let the GET stream be established before the run "ends"
        return SimpleNamespace(finish_reason="stop", final_response="", events=[])

    H._run_sync = fake_run_sync
    os.environ.setdefault("DSH_BIN", "/bin/true")

    class _Catch(logging.Handler):
        hits: list[str] = []

        def emit(self, record):
            if "without completing" in record.getMessage() or "without starting" in record.getMessage():
                self.hits.append(record.getMessage())
    logging.getLogger("uvicorn.error").addHandler(_Catch())
    for i in range(GENERATIONS):
        try:
            asyncio.run(H.run_agent(tools=tools, submit_tool="submit_answer", prompt="p", system_prompt=None,
                                    cwd="/tmp", model="x", effort=None, max_turns=5, allowed_builtin=("read",),
                                    label=f"gen{i}", max_buffer_size=None, wall_seconds=None))
        except AgentIncompleteError:
            pass
        assert AppStatus.should_exit is False, f"sse-starlette shutdown latch left set after generation {i}"
        assert not _Catch.hits, f"uvicorn reported truncated responses by generation {i}: {_Catch.hits[:3]}"
    return outcomes


def test_consecutive_mcp_servers_in_one_process():
    outcomes = _run(hold_seconds=3.0)
    assert all(ok for _, _, ok in outcomes), outcomes
    assert all("2 tools" in msg for _, msg, _ in outcomes), outcomes


if __name__ == "__main__":
    t0 = time.time()
    outcomes = _run(hold_seconds=3.0)
    for i, msg, ok in outcomes:
        print(f"generation {i}: {'OK ' if ok else 'FAILED'} {msg}")
    bad = [o for o in outcomes if not o[2]]
    print(f"{len(outcomes)} generations in {time.time() - t0:.1f}s, {len(bad)} failed")
    sys.exit(1 if bad else 0)

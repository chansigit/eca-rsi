"""A stage program that dies and restarts must not buy the same model turn twice.

This is the whole of eca-rsi#4 part A as generation 2 answers it. The issue proposed
journaling every accepted submission to disk, because generation 1's bridge kept them
in memory. Generation 2 does not need a journal: a model turn is a Bridge request and
a tool call is a Pool request, both already durable, and both named by an id that is a
pure function of the session and the call. A restarted program recomputes the same
ids, and the request stores answer from disk.

That property lives entirely in two expressions in `session.py`, and nothing else
guards them. Put a clock, a uuid, or an attempt counter into either one and every
restart silently re-buys the model call and recomputes the tool -- the expensive
failure the issue was filed about, arriving through the fix for it. Hence this test
asserts the *spelling* of both ids, not only that a replay happened to work.
"""
import json
from pathlib import Path
from unittest.mock import patch

from ecarsi.warm_pool.state import digest, read

import ecarsi.agent as bridge
import ecarsi.agent.session as session
from .test_agent_session import Client, ScriptedModel, completed_tool, execute_turn, setup


def test_a_restarted_session_replays_its_turn_and_tool_instead_of_repeating_them(tmp_path):
    from harness_bridge import _harness_openai as adapter

    spec, ref = setup(tmp_path)
    model = ScriptedModel()
    bridge_root, pool_requests = Path(spec["bridge_root"]), Path(spec["pool_root"]) / "requests"

    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=model):
        turn = session.submit_turn(ref, 0)
        reply = execute_turn(bridge_root, turn)
        item = session.tool_request(ref, reply, 0)
        completed_tool(spec, item)

        assert len(model.inputs) == 1
        assert [p.name for p in pool_requests.iterdir()] == [item["request_id"]]

        # The stage program dies here. Nothing of it survives but its output directory,
        # so the restart rebuilds the session from the spec alone -- no in-memory state.
        restarted = session.create_session(spec)
        assert restarted == ref

        assert session.submit_turn(restarted, 0) == turn
        assert bridge.status(bridge_root, turn)["state"] == "reply_saved"
        assert len(model.inputs) == 1, "the restart bought the model turn a second time"

        assert session.tool_request(restarted, reply, 0) == item
        assert [p.name for p in pool_requests.iterdir()] == [item["request_id"]]
        attempts = [p for p in (pool_requests / item["request_id"]).iterdir() if len(p.name) == 32]
        assert len(attempts) == 1, "the restart recomputed an accepted tool result"

        # Both ids derive from the session and the call, and from nothing else.
        assert turn == "test-session.turn-0"
        assert item["request_id"] == "test-session.tool-" + digest(["test-session.turn-0", "call-a"])[:16]

        # And the accepted submission itself is on disk, reachable from the reply the
        # restart re-read -- which is what the issue asked a journal to provide.
        arguments = read(reply)["response"]["calls"][0]["arguments"]
        assert (arguments if isinstance(arguments, dict) else json.loads(arguments))["value"] == 7

"""A session that resubmits the same rejected call three turns in a row is stopped, not left to burn its turns."""
import json
from unittest.mock import patch

import pytest
from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall

import ecarsi.agent.session as session
from tests.test_agent_session import Client, completed_tool, execute_turn, setup


class LoopingModel(Model):
    """Always the same call with the same arguments, whatever the tool said."""
    def __init__(self):
        self.turns = 0

    async def get_response(self, **kwargs):
        self.turns += 1
        call = ResponseFunctionToolCall(type="function_call", name="compute", call_id=f"call-{self.turns}",
                                        arguments='{"value":7}', id=f"fc-{self.turns}", status="completed")
        return ModelResponse(output=[call], usage=Usage(requests=1, input_tokens=10, output_tokens=4),
                             response_id=f"response-{self.turns}")

    async def stream_response(self, **kwargs):
        raise AssertionError("Streaming not used")
        yield


def test_three_identical_rejections_stop_the_session(tmp_path):
    from harness_bridge import _harness_openai as adapter
    from pathlib import Path
    spec, ref = setup(tmp_path)
    root = Path(spec["bridge_root"])
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=LoopingModel()):
        context, parents = None, []
        for turn in range(session.REPEAT_LIMIT):
            reply = execute_turn(root, session.submit_turn(ref, turn, context, parents))
            item = session.tool_request(ref, reply, 0)
            rejected = completed_tool(spec, item, {"is_error": True, "content": "Extra data: line 1 column 9"})
            if turn < session.REPEAT_LIMIT - 1:
                context = session.continuation(ref, reply, [rejected])
                parents = [item["request_id"]]
            else:
                with pytest.raises(ValueError, match="identical rejected compute call 3 times"):
                    session.continuation(ref, reply, [rejected])

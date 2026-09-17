"""A malformed model tool request must come back for correction, not kill a dataset.

On 2026-09-16 a single undeclared read-only tool ended 101 workflows and all 28
datasets in the scale batch: the host raised, the agent session died, and the
lineage, unit and dataset died with it.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall

import ecarsi.bridge as bridge
import ecarsi.bridge.session as session
import ecarsi.control as work_coordinator
from ecarsi.bridge.parallel import READS
from ecarsi.warm_pool.budget import from_artifact
from ecarsi.warm_pool.state import read, save


class TwoCalls(Model):
    """One batchable read, one tool that changes state: the batch must be refused."""

    async def get_response(self, **kwargs):
        return ModelResponse(response_id="response-1",
            usage=Usage(requests=1, input_tokens=5, output_tokens=2),
            output=[ResponseFunctionToolCall(type="function_call", name="read_evidence", call_id="call-a",
                        arguments='{"offset":0}', id="fc-a", status="completed"),
                    ResponseFunctionToolCall(type="function_call", name="subcluster", call_id="call-b",
                        arguments='{"cluster":"3"}', id="fc-b", status="completed")])

    async def stream_response(self, **kwargs):
        raise AssertionError("Streaming not used")
        yield


class UndeclaredRead(Model):
    """A batch of reads where the saved session never declared check_qc_scores read-only."""

    async def get_response(self, **kwargs):
        return ModelResponse(response_id="response-1",
            usage=Usage(requests=1, input_tokens=5, output_tokens=2),
            output=[ResponseFunctionToolCall(type="function_call", name="read_evidence", call_id="call-a",
                        arguments='{"offset":0}', id="fc-a", status="completed"),
                    ResponseFunctionToolCall(type="function_call", name="check_qc_scores", call_id="call-b",
                        arguments='{}', id="fc-b", status="completed")])

    async def stream_response(self, **kwargs):
        raise AssertionError("Streaming not used")
        yield


class Client:
    async def close(self):
        pass


def build(tmp):
    catalog = tmp / "models.json"
    save(catalog, {"models": [{"harness": "openai@vllm", "model": "test-model", "url": "http://localhost:1/v1"}]})
    pool = tmp / "pool"
    pool.mkdir(mode=0o700)
    (pool / "requests").mkdir()
    save(pool / "config.json", {"runtime": {"command": ["/usr/bin/python3"], "files": {}, "version": "test"}})
    root = bridge.init(tmp / "bridge", catalog, concurrency=1)

    def tool(name, properties, read_only):
        return dict(name=name, description=name, read_only=read_only, cpus=1, memory_mb=64,
            timeout_seconds=30, inputs=[], outputs=["result.json"], result_file="result.json",
            args=["-c", "raise AssertionError('must never run')", "{arguments}"],
            parameters={"type": "object", "properties": properties,
                        "required": list(properties), "additionalProperties": False})

    spec = dict(session_id="reject-session", dataset_id="test-data", prompt="Read the evidence.",
        max_turns=5, pool_root=str(pool), bridge_root=str(root), output_root=str(tmp / "session"),
        tools=[tool("read_evidence", {"offset": {"type": "integer"}}, True),
               tool("subcluster", {"cluster": {"type": "string"}}, False)])
    return spec, session.create_session(spec)


def test_batching_an_unbatchable_tool_returns_a_correction_not_a_failure(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, ref = build(tmp_path)
    root = Path(spec["bridge_root"])
    request = session.submit_turn(ref, 0)
    save(root / "requests" / request / "state.json", {"state": "running", "started_at": 1})
    with patch.object(adapter, "_client", return_value=Client()), \
         patch.object(adapter, "_model", return_value=TwoCalls()):
        bridge.execute(root, request)
    assert bridge.status(root, request)["state"] == "reply_saved"
    reply = root / "requests" / request / "result.json"

    # The policy still refuses the batch itself.
    with pytest.raises(session.ToolRejection, match="read-only"):
        session.tool_request(ref, reply, 0)

    # The Coordinator turns that refusal into a tool result the model can act on.
    for index in (0, 1):
        item = work_coordinator.agent_step("tool", [ref, str(reply), index, None])
        submitted = read(Path(spec["pool_root"]) / "requests" / item["request_id"] / "request.json")
        assert submitted["spec"]["args"][:2] == ["-m", "ecarsi.bridge.tool_errors"]
        packet = next(i["path"] for i in submitted["spec"]["inputs"]
                      if i["path"].endswith("argument-rejection.json"))
        response = read(packet)["response"]
        assert response["is_error"] and "read-only" in response["content"]

    # No registered scientific program was launched by the rejected batch.
    assert not any(read(p)["spec"]["args"][0] == "-c"
                   for p in (Path(spec["pool_root"]) / "requests").glob("*/request.json"))


def test_saved_session_batches_by_current_policy_not_its_stale_copy(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, ref = build(tmp_path)
    spec["tools"][1] = dict(spec["tools"][1], name="check_qc_scores", parameters={
        "type": "object", "properties": {}, "required": [], "additionalProperties": False})
    spec = dict(spec, session_id="stale-policy", output_root=str(tmp_path / "stale"))
    ref = session.create_session(spec)
    root = Path(spec["bridge_root"])
    request = session.submit_turn(ref, 0)
    save(root / "requests" / request / "state.json", {"state": "running", "started_at": 1})
    with patch.object(adapter, "_client", return_value=Client()), \
         patch.object(adapter, "_model", return_value=UndeclaredRead()):
        bridge.execute(root, request)
    reply = root / "requests" / request / "result.json"
    # check_qc_scores is read-only under current policy, so the batch executes sequentially.
    first = session.tool_request(ref, reply, 0)
    submitted = read(Path(spec["pool_root"]) / "requests" / first["request_id"] / "request.json")
    assert submitted["spec"]["operation_id"] == "read_evidence"
    assert submitted["spec"]["args"][:2] != ["-m", "ecarsi.bridge.tool_errors"]


def test_registered_reads_share_one_batching_policy():
    """Every stage's Coordinator applies READS; worker programs are hash-pinned and stay untouched."""
    import inspect
    import ecarsi.control.crosssample as crosssample_workflow
    import ecarsi.control.persample as persample_workflow
    import ecarsi.control.zoomin as zoomin_workflow
    assert "annotation_status" in READS
    for module in (crosssample_workflow, zoomin_workflow, persample_workflow):
        assert "in READS" in inspect.getsource(module), module.__name__


def test_artifact_budget_follows_its_input_size(tmp_path):
    pool = tmp_path / "pool"
    (pool / "requests").mkdir(parents=True)
    artifact = tmp_path / "annotated.h5ad"
    artifact.write_bytes(b"0" * (6 * 2**20))
    ref = dict(path=str(artifact), sha256="0" * 64)
    request = dict(request_id="zoom.prepare", operation_id="zoom-in.prepare", args=["-m", "x"],
                   cpus=1, memory_mb=4096, timeout_seconds=60, inputs=[], outputs=["prepared.json"])

    # A small input keeps the configured ceiling.
    assert from_artifact(request, ref, tmp_path / "small.json", str(pool))["memory_mb"] == 4096

    # A large input raises it instead of dying with MemoryError.
    sized = from_artifact(request, ref, tmp_path / "large.json", str(pool), copies=1000)
    assert sized["memory_mb"] == 7168
    assert from_artifact(request, ref, tmp_path / "large.json", str(pool), copies=1000) == sized
    with pytest.raises(ValueError, match="resource request changed"):
        from_artifact(dict(request, cpus=2), ref, tmp_path / "large.json", str(pool), copies=1000)


def test_retry_can_raise_a_memory_budget_without_losing_request_identity(tmp_path):
    """A prepare step that died on its RSS watchdog is retried larger; resubmission still matches."""
    import json as _json
    from ecarsi.warm_pool import state
    pool = tmp_path / "pool"
    pool.mkdir(mode=0o700)
    (pool / "requests").mkdir()
    state.save(pool / "config.json", {"runtime": {"command": ["/usr/bin/python3"], "files": {}, "version": "test"}})
    spec = dict(request_id="prepare-1", operation_id="zoom-in.prepare", args=["-c", "pass"], cpus=1,
                memory_mb=4096, timeout_seconds=30, inputs=[], outputs=["prepared.json"])
    first = state.submit(str(pool), spec)
    folder = pool / "requests" / "prepare-1"
    request = state.read(folder / "request.json")
    state.save(folder / request["attempt_id"] / "receipt.json", dict(state="failed", finished_at=1.0,
        attempt_id=request["attempt_id"], request_digest=request["digest"],
        runtime_digest=request["runtime_digest"], error="MemoryError"))
    with pytest.raises(ValueError, match="exceed"):
        state.retry(str(pool), "prepare-1", reason="too small", memory_mb=4096)
    retried = state.retry(str(pool), "prepare-1", reason="RSS budget was too small", memory_mb=9216)
    assert retried["state"] == "queued" and retried["attempt_id"] != first["attempt_id"]
    again = state.read(folder / "request.json")
    assert again["spec"]["memory_mb"] == 9216 and again["digest"] == state.digest(again["spec"])
    assert again["original_digest"] == request["digest"]
    # The Coordinator resubmits its unchanged 4096 MiB request on recovery; same work.
    assert state.submit(str(pool), spec)["attempt_id"] == retried["attempt_id"]
    with pytest.raises(ValueError, match="different content"):
        state.submit(str(pool), dict(spec, cpus=2))

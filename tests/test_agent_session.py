"""Exercise real SDK pause/serialization/resume with a synthetic model, never a provider."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from agents import Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

from ecarsi import agent_bridge as bridge, agent_session as session
from ecarsi.warm_pool.state import read, save, submit, file_digest


class ScriptedModel(Model):
    def __init__(self):
        self.inputs = []

    async def get_response(self, **kwargs):
        self.inputs.append(kwargs["input"])
        if len(self.inputs) == 1:
            # Chat adapters can omit this field even though SDK restore requires it.
            output = [ResponseOutputMessage(type="message", id="preface", role="assistant", status="completed",
                       content=[ResponseOutputText.model_construct(type="output_text", text="I will compute.")]),
                      ResponseFunctionToolCall(type="function_call", name="compute", call_id="call-a",
                       arguments='{"value":7}', id="fc-a", status="completed")]
        else:
            assert "worker-node" in json.dumps(kwargs["input"])
            output = [ResponseOutputMessage(type="message", id="message-2", role="assistant", status="completed",
                       content=[ResponseOutputText(type="output_text", text="Worker result accepted", annotations=[])])]
        return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=10, output_tokens=4),
                             response_id=f"response-{len(self.inputs)}")

    async def stream_response(self, **kwargs):
        raise AssertionError("Streaming not used")
        yield


class Client:
    async def close(self):
        pass


def test_provider_summary_retains_empty_reply_evidence_without_payloads():
    summary = session.provider_summary(200, dict(id='response-1', status='completed',
        usage=dict(output_tokens=13843), output=[dict(type='message',
            content=[dict(type='output_text', text='')])]))
    assert summary['usage']['output_tokens'] == 13843
    assert summary['output'][0]['text_chars'] == 0
    summary = session.provider_summary(200, dict(output=[dict(type='function_call', name='submit',
        arguments='private scientific content')], choices=[dict(finish_reason='length',
            message=dict(content='private text', tool_calls=[{}]))]))
    assert summary['choices'][0] == dict(finish_reason='length', text_chars=12, tool_calls=1)
    assert 'private' not in json.dumps(summary)


def setup(tmp):
    catalog = tmp / "models.json"
    save(catalog, {"models": [{"harness": "openai@vllm", "model": "test-model", "url": "http://localhost:1/v1"}]})
    pool = tmp / "pool"
    pool.mkdir(mode=0o700)
    (pool / "requests").mkdir()
    save(pool / "config.json", {"runtime": {"command": ["/usr/bin/python3"], "files": {}, "version": "test"}})
    root = bridge.init(tmp / "bridge", catalog, concurrency=1)
    spec = dict(session_id="test-session", dataset_id="test-data", prompt="Call compute then report its result.",
                max_turns=5, pool_root=str(pool), bridge_root=str(root), output_root=str(tmp / "session"),
                tools=[dict(name="compute", description="Compute on a worker",
                    parameters={"type": "object", "properties": {"value": {"type": "integer"}},
                                "required": ["value"], "additionalProperties": False},
                    args=["-c", "raise AssertionError('must never run on Bridge')", "{arguments}"],
                    cpus=1, memory_mb=64, timeout_seconds=30, inputs=[], outputs=["result.json"], result_file="result.json")])
    return spec, session.create_session(spec)


def execute_turn(root, request_id):
    save(root / "requests" / request_id / "state.json", {"state": "running", "started_at": 1})
    bridge.execute(root, request_id)
    result = bridge.status(root, request_id)
    assert result["state"] == "reply_saved", result
    return root / "requests" / request_id / "result.json"


def test_pinned_adapter_survives_upgrade_and_rejects_tampering(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, ref = setup(tmp_path)
    # A distinct, complete adapter revision is retained, not a patched live module.
    previous = tmp_path / "previous.py"
    previous.write_bytes(Path(session.__file__).read_bytes() + b"\n# previous release\n")
    archived = session.archive_adapter(spec["bridge_root"], previous)
    saved = read(ref["path"])
    saved["adapter_sha256"] = archived["sha256"]
    save(ref["path"], saved)
    ref = session.reference(ref["path"])
    before = Path(ref["path"]).read_bytes()
    request_id = session.submit_turn(ref, 0)
    request = read(Path(spec["bridge_root"]) / "requests" / request_id / "request.json")
    loaded = session.pinned_adapter(spec["bridge_root"], archived["sha256"])
    assert loaded.__file__ == archived["path"]
    model = ScriptedModel()
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=model):
        reply = execute_turn(Path(spec["bridge_root"]), request_id)
    assert read(reply)["response"]["kind"] == "tools" and len(model.inputs) == 1
    assert Path(ref["path"]).read_bytes() == before
    snapshot = Path(archived["path"])
    snapshot.chmod(0o600)
    snapshot.write_text("raise AssertionError('must not execute unverified source')")
    with pytest.raises(ValueError, match="Archived agent adapter changed"):
        session.validate_turn(request["spec"])
    snapshot.unlink()
    with pytest.raises(FileNotFoundError):
        session.validate_turn(request["spec"])


def test_recovery_keeps_original_tool_policy_when_batching_is_introduced(tmp_path):
    spec, ref = setup(tmp_path)
    upgraded = {**spec, "tools": [dict(t, read_only=True) for t in spec["tools"]]}
    assert session.create_session(upgraded) == ref
    assert session.verified(ref)["spec"] == spec
    changed = {**upgraded, "tools": [dict(t, memory_mb=t["memory_mb"]+1) for t in upgraded["tools"]]}
    with pytest.raises(ValueError, match="another specification"):
        session.create_session(changed)


def test_missing_submission_repair_is_bounded_and_preserves_original_evidence(tmp_path):
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    state = session.immutable(tmp_path/'original-state.json', {'read': []})
    spec = dict(spec, session_id='missing-submit', output_root=str(tmp_path/'missing-submit'),
                completion_tool='compute', tool_state=state)
    original = session.create_session(spec)
    reply = session.immutable(tmp_path/'final-reply.json', {'response': {'kind': 'final'}})
    result = session.immutable(Path(spec['output_root'])/'result.json', {'session': original, 'reply': reply})
    repaired = session.create_session(spec)
    assert repaired != original and session.verified(result)['session'] == original
    replacement = session.verified(repaired)['spec']
    assert replacement['tool_state'] == state and replacement['completion_tool'] == 'compute'
    assert session.create_session(spec) == repaired
    # A second failure stays visible for review; no recursive retry chain.
    session.immutable(Path(replacement['output_root'])/'result.json', {'session': repaired, 'reply': reply})
    assert session.create_session(spec) == repaired
    assert not (Path(replacement['output_root'])/'submission-recovery').exists()


def completed_tool(spec, item, value=None):
    folder = Path(spec["pool_root"]) / "requests" / item["request_id"]
    request = read(folder / "request.json")
    attempt = folder / request["attempt_id"]
    output = attempt / "outputs/result.json"
    save(output, value if value is not None else {"worker": "worker-node", "value": 49})
    save(attempt / "receipt.json", {"state": "succeeded", "started_at": 1, "finished_at": 2,
         "outputs": [{**session.reference(output), "size": output.stat().st_size}]})
    return {**item, "path": str(output)}


def test_local_restore_failure_releases_bridge_capacity(tmp_path):
    import harness_bridge._harness_openai as adapter
    from agents import RunState
    model = ScriptedModel()
    spec, ref = setup(tmp_path)
    root = Path(spec["bridge_root"])
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=model):
        reply = execute_turn(root, session.submit_turn(ref, 0))
        item = session.tool_request(ref, reply, 0)
        context = session.continuation(ref, reply, [completed_tool(spec, item)])
        request = session.submit_turn(ref, 1, context, [item["request_id"]])
        save(root / "requests" / request / "state.json", {"state": "running"})
        with patch.object(RunState, "from_json", side_effect=RuntimeError("invalid local state")):
            bridge.execute(root, request)
        result = bridge.status(root, request)
        assert result["state"] == "failed" and result["provider_called"] is False
        assert len(model.inputs) == 1


def test_sdk_checkpoint_worker_handoff_and_resume(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, ref = setup(tmp_path)
    model = ScriptedModel()
    root = Path(spec["bridge_root"])
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=model):
        first = session.submit_turn(ref, 0)
        reply = execute_turn(root, first)
        response = read(reply)["response"]
        assert response["kind"] == "tools" and len(model.inputs) == 1
        assert not list((Path(spec["pool_root"]) / "requests").iterdir())
        with pytest.raises(ValueError, match="Only the first"):
            session.submit_turn(ref, 1)
        # A paused session's model request is terminal, so another session can
        # acquire the sole Bridge slot while its worker tool is still pending.
        other_spec = {**spec, "session_id": "another", "output_root": str(tmp_path / "another")}
        other = session.create_session(other_spec)
        other_id = session.submit_turn(other, 0)
        with patch.object(bridge, "launch", return_value=None) as launch:
            bridge.serve(root, once=True)
            assert launch.call_args.args[1].name == other_id
        item = session.tool_request(ref, reply, 0)
        assert session.tool_request(ref, reply, 0) == item  # activity replay is idempotent
        accepted = completed_tool(spec, item)
        context = session.continuation(ref, reply, [accepted])
        assert session.continuation(ref, reply, [accepted]) == context
        second = session.submit_turn(ref, 1, context, [item["request_id"]])
        final = execute_turn(root, second)
        assert read(final.parent / "sdk-restore.json")["empty_annotations_restored"] > 0
        assert read(final)["response"]["kind"] == "final"
        assert read(final)["response"]["usage"] == {"tokens_in": 10, "tokens_out": 4, "cost_usd": None}
        bridge.execute(root, second)
        assert len(model.inputs) == 2  # receipt replay never calls the model again
        assert len(list((Path(spec["pool_root"]) / "requests").iterdir())) == 1
        # Serialized SDK context, not a live Python process, carried the original call ID/result.
        assert read(context["path"])["results"][0]["call_id"] == "call-a"
        # An interruption result survives loss of its Bridge executor before acknowledgement.
        reply.unlink()
        bridge.recover_result(reply.parent, "test-crash")
        assert read(reply)["response"]["sdk_state"] == response["sdk_state"]


def test_invalid_tool_and_modified_or_cancelled_results_cannot_resume(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, ref = setup(tmp_path)
    root = Path(spec["bridge_root"])
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=ScriptedModel()):
        reply = execute_turn(root, session.submit_turn(ref, 0))
    original = read(reply)
    bad = json.loads(json.dumps(original))
    bad["response"]["calls"][0]["arguments"]["command"] = "forbidden"
    save(reply, bad)
    with pytest.raises(Exception, match="Additional properties"):
        session.tool_request(ref, reply, 0)
    assert not list((Path(spec["pool_root"]) / "requests").iterdir())
    save(reply, original)
    item = session.tool_request(ref, reply, 0)
    accepted = completed_tool(spec, item)
    output = Path(accepted["path"])
    output.write_text('{"modified":true}')
    with pytest.raises(ValueError, match="Artifact changed"):
        session.continuation(ref, reply, [accepted])
    accepted = completed_tool(spec, item)
    save(Path(spec["pool_root"]) / "requests" / item["request_id"] / "cancel.json", {"requested_at": 1})
    with pytest.raises(ValueError, match="uncancelled"):
        session.continuation(ref, reply, [accepted])


def test_decoded_json_field_is_losslessly_encoded_before_validation(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, _ = setup(tmp_path)
    spec = {**spec, 'session_id':'json-test', 'output_root':str(tmp_path/'json-session')}
    spec['tools'] = [{**spec['tools'][0], 'parameters':{'type':'object', 'properties':{'value_json':{'type':'string'}},
                    'required':['value_json'], 'additionalProperties':False}}]
    ref=session.create_session(spec);root=Path(spec['bridge_root'])
    with patch.object(adapter,'_client',return_value=Client()),patch.object(adapter,'_model',return_value=ScriptedModel()):
        reply=execute_turn(root,session.submit_turn(ref,0))
    value={'samples':[{'include':True,'label':'a'}]};payload=read(reply)
    payload['response']['calls'][0]['arguments']={'value_json':value};save(reply,payload)
    task=session.tool_request(ref,reply,0)
    request=read(Path(spec['pool_root'])/'requests'/task['request_id']/'request.json')
    arguments=read(request['spec']['args'][-1])
    assert json.loads(arguments['value_json'])==value


def test_completion_tool_and_business_trace(tmp_path):
    from harness_bridge import _harness_openai as adapter
    spec, _ = setup(tmp_path)
    spec = {**spec, "session_id": "business", "output_root": str(tmp_path / "business"),
            "completion_tool": "compute", "trace": {"workflow_id": "organize/dataset",
                "dataset_id": spec["dataset_id"], "unit_id": "organize.plan", "depends_on": ["dataset.prepare"]}}
    ref = session.create_session(spec)
    root = Path(spec["bridge_root"])
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=ScriptedModel()):
        first = session.submit_turn(ref, 0)
        reply = execute_turn(root, first)
    assert read(reply.parent / "request.json")["spec"]["trace"] == spec["trace"]
    item = session.tool_request(ref, reply, 0)
    request = read(Path(spec["pool_root"]) / "requests" / item["request_id"] / "request.json")
    assert request["spec"]["trace"] == {**spec["trace"], "depends_on": [first]}
    accepted = completed_tool(spec, item, {"accepted": True, "response": {"plan": "validated"}})
    context = session.continuation(ref, reply, [accepted])
    final = session.complete_tool(ref, context)
    assert session.verified(read(final)["output"])["accepted"] is True
    assert session.complete_tool(ref, context) == final
    from ecarsi.work_coordinator import agent_step
    assert agent_step("cached_completion", [ref]) == final
    save(Path(spec["pool_root"]) / "requests" / item["request_id"] / "cancel.json", {"requested_at": 1})
    with pytest.raises(ValueError, match="uncancelled"):
        session.complete_tool(ref, context)
    with pytest.raises(ValueError, match="no longer accepted"):
        agent_step("cached_completion", [ref])


@pytest.mark.parametrize("portable", [False, True])
def test_worker_images_and_state_survive_sdk_continuation(tmp_path, portable):
    from harness_bridge import _harness_openai as adapter
    class VisionModel(ScriptedModel):
        async def get_response(self, **kwargs):
            if len(self.inputs) == 1:
                assert "input_image" in json.dumps(kwargs["input"])
                self.inputs.append(kwargs["input"])
                return ModelResponse(output=[ResponseFunctionToolCall(type="function_call", name="compute",
                    call_id="call-b", arguments='{"value":8}', id="fc-b", status="completed")],
                    usage=Usage(requests=1, input_tokens=10, output_tokens=4), response_id="response-2")
            return await super().get_response(**kwargs)
    spec, _ = setup(tmp_path)
    if portable:
        config_path = Path(spec["bridge_root"]) / "config.json"
        save(config_path, {**read(config_path), "pool_root": spec["pool_root"]})
    initial = session.immutable(tmp_path / "initial.json", {"version": 0})
    later = session.immutable(tmp_path / "later.json", {"version": 1})
    spec = {**spec, "session_id": "vision", "output_root": str(tmp_path / "vision"), "tool_state": initial,
            "tools": [{**spec["tools"][0], "multimodal": True, "args": spec["tools"][0]["args"] + ["{state}"]}]}
    ref = session.create_session(spec)
    root = Path(spec["bridge_root"])
    with patch.object(adapter, "_client", return_value=Client()), patch.object(adapter, "_model", return_value=VisionModel()):
        reply = execute_turn(root, session.submit_turn(ref, 0))
        item = session.tool_request(ref, reply, 0)
        image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aMZkAAAAASUVORK5CYII="
        accepted = completed_tool(spec, item, {"worker": "worker-node", "images": [image], "state": later})
        context = session.continuation(ref, reply, [accepted])
        reply = execute_turn(root, session.submit_turn(ref, 1, context, [item["request_id"]]))
        second = session.tool_request(ref, reply, 0)
        request = read(Path(spec["pool_root"]) / "requests" / second["request_id"] / "request.json")
        assert request["spec"]["args"][-1] == later["path"]
        assert later in request["spec"]["inputs"]


def test_invalid_arguments_return_to_model_without_executing_tool(tmp_path,monkeypatch):
    from harness_bridge import _harness_openai as adapter
    from ecarsi.work_coordinator import agent_step
    from ecarsi.agent_tool_errors import write_rejection
    class CorrectingModel(ScriptedModel):
        async def get_response(self,**kwargs):
            self.inputs.append(kwargs['input'])
            turn=len(self.inputs)
            if turn==1:
                output=[ResponseFunctionToolCall(type='function_call',name='compute',call_id='bad',arguments='{}',id='bad',status='completed')]
            elif turn==2:
                assert 'Rejected call to compute' in json.dumps(kwargs['input'])
                output=[ResponseFunctionToolCall(type='function_call',name='compute',call_id='good',arguments='{"value":7}',id='good',status='completed')]
            else:
                assert 'worker-node' in json.dumps(kwargs['input'])
                output=[ResponseOutputMessage(type='message',id='done',role='assistant',status='completed',
                    content=[ResponseOutputText(type='output_text',text='done',annotations=[])])]
            return ModelResponse(output=output,usage=Usage(requests=1,input_tokens=10,output_tokens=4),response_id='r'+str(turn))
    spec,_=setup(tmp_path)
    state=session.immutable(tmp_path/'state.json',{'preserved':17})
    spec={**spec,'session_id':'correcting','output_root':str(tmp_path/'correcting'),'tool_state':state,
          'tools':[{**spec['tools'][0],'args':spec['tools'][0]['args']+['{state}']}]}
    ref=session.create_session(spec);root=Path(spec['bridge_root']);model=CorrectingModel()
    with patch.object(adapter,'_client',return_value=Client()),patch.object(adapter,'_model',return_value=model):
        reply=execute_turn(root,session.submit_turn(ref,0))
        item=agent_step('tool',[ref,str(reply),0,None])
        request=read(Path(spec['pool_root'])/'requests'/item['request_id']/'request.json')
        assert request['spec']['args'][:2]==['-m','ecarsi.agent_tool_errors']
        assert 'must never run' not in str(request['spec']['args'])
        output=tmp_path/'error-output';output.mkdir();monkeypatch.chdir(output)
        write_rejection(request['spec']['args'][2])
        response=read(output/'result.json');assert response['is_error'] and response['state']==state
        context=session.continuation(ref,reply,[completed_tool(spec,item,response)])
        reply=execute_turn(root,session.submit_turn(ref,1,context,[item['request_id']]))
        corrected=agent_step('tool',[ref,str(reply),0,None])
        actual=read(Path(spec['pool_root'])/'requests'/corrected['request_id']/'request.json')['spec']
        assert state in actual['inputs']  # rejection preserves the last accepted state
        context=session.continuation(ref,reply,[completed_tool(spec,corrected,{'state':state,'worker':'worker-node','value':49})])
        final=execute_turn(root,session.submit_turn(ref,2,context,[corrected['request_id']]))
        assert read(final)['response']['kind']=='final' and len(model.inputs)==3


def test_batched_calls_keep_ordered_worker_state_and_require_every_result(tmp_path):
    from harness_bridge import _harness_openai as adapter
    class BatchModel(ScriptedModel):
        async def get_response(self, **kwargs):
            assert kwargs['model_settings'].parallel_tool_calls is True
            self.inputs.append(kwargs['input'])
            if len(self.inputs) == 1:
                output = [ResponseFunctionToolCall(type='function_call', name='compute', call_id='call-' + str(i),
                          arguments=json.dumps({'value': i}), id='fc-' + str(i), status='completed') for i in (7, 8)]
            else:
                assert all(label in json.dumps(kwargs['input']) for label in ('first-output', 'second-output'))
                output = [ResponseOutputMessage(type='message', id='done', role='assistant', status='completed',
                          content=[ResponseOutputText(type='output_text', text='Both worker results accepted', annotations=[])])]
            return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=10, output_tokens=4),
                                 response_id='r' + str(len(self.inputs)))
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    initial = session.immutable(tmp_path/'initial.json', {'version': 0})
    later = session.immutable(tmp_path/'later.json', {'version': 1})
    spec = dict(spec, session_id='batched', output_root=str(tmp_path/'batched'), tool_state=initial,
                tools=[dict(spec['tools'][0], read_only=True, args=spec['tools'][0]['args'] + ['{state}'])])
    ref = session.create_session(spec)
    with patch.object(adapter, '_client', return_value=Client()), patch.object(adapter, '_model', return_value=BatchModel()):
        reply = execute_turn(root, session.submit_turn(ref, 0))
        with patch('ecarsi.agent_evidence.plan', side_effect=AssertionError('Do not prefetch a native batch')):
            first = session.tool_request(ref, reply, 0)
        accepted_first = completed_tool(spec, first, {'text': 'first-output', 'state': later})
        second = session.tool_request(ref, reply, 1, first['request_id'])
        request = read(Path(spec['pool_root'])/'requests'/second['request_id']/'request.json')['spec']
        assert later in request['inputs'] and request['args'][-1] == later['path']
        accepted_second = completed_tool(spec, second, {'text': 'second-output', 'state': later})
        with pytest.raises(ValueError, match='Missing or reordered'):
            session.continuation(ref, reply, [accepted_second])
        with pytest.raises(ValueError, match='Missing or reordered'):
            session.continuation(ref, reply, [accepted_second, accepted_first])
        context = session.continuation(ref, reply, [accepted_first, accepted_second])
        final = execute_turn(root, session.submit_turn(ref, 1, context, [second['request_id']]))
        assert read(final)['response']['kind'] == 'final'
        assert len(list((Path(spec['pool_root'])/'requests').iterdir())) == 2


def test_mixed_read_and_decision_batch_cannot_dispatch_any_tool(tmp_path):
    from harness_bridge import _harness_openai as adapter
    class MixedModel(ScriptedModel):
        async def get_response(self, **kwargs):
            assert kwargs['model_settings'].tool_choice is None
            output = [ResponseFunctionToolCall(type='function_call', name=name, call_id=name,
                      arguments='{"value":7}', id=name, status='completed') for name in ('compute', 'submit')]
            return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=10, output_tokens=4), response_id='mixed')
    spec, _ = setup(tmp_path)
    root = Path(spec['bridge_root'])
    save(root/'config.json', dict(read(root/'config.json'), pool_root=spec['pool_root']))
    spec = dict(spec, session_id='mixed', output_root=str(tmp_path/'mixed'), completion_tool='submit',
                tools=[dict(spec['tools'][0], read_only=True), dict(spec['tools'][0], name='submit')])
    ref = session.create_session(spec)
    with patch.object(adapter, '_client', return_value=Client()), patch.object(adapter, '_model', return_value=MixedModel()):
        reply = execute_turn(root, session.submit_turn(ref, 0))
        for index in (0, 1):
            with pytest.raises(ValueError, match='Batch only declared read-only'):
                session.tool_request(ref, reply, index)
        assert not list((Path(spec['pool_root'])/'requests').iterdir())


def test_adapter_archive_rejects_invalid_source_before_publication(tmp_path):
    import hashlib
    spec, _ = setup(tmp_path)
    source = tmp_path/'incomplete.py';source.write_text('if True:\nnot indented\n')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(SyntaxError):
        session.archive_adapter(spec['bridge_root'], source)
    assert not (Path(spec['bridge_root'])/'adapters'/(sha+'.py')).exists()

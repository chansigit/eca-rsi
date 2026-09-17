"""Durable agent tool boundaries: Pool executes model turns and registered programs.

Tools are trusted application registrations, never model-selected commands or budgets.
The Agents SDK interruption is a machine handoff, not a human approval request.
"""
import json
import hashlib
import os
from pathlib import Path
import re
import tempfile
from types import ModuleType, SimpleNamespace

from ..warm_pool.state import digest, file_digest, identifier, immutable, lock, read, reference, save, validate, verified


class ToolRejection(ValueError):
    """A model-attributable tool request. Returned to the model for correction.

    Host-integrity failures stay plain ValueError and still fail the workflow.
    """


def archive_adapter(bridge_root, source=None):
    """Keep the exact model-call adapter available for the lifetime of its sessions."""
    from . import root_path
    from ..warm_pool.state import sync_directory
    directory = root_path(bridge_root) / "adapters"
    directory.mkdir(mode=0o700, exist_ok=True)
    content = Path(source or __file__).read_bytes()
    compile(content, str(source or __file__), "exec")
    sha = hashlib.sha256(content).hexdigest()
    path = directory / (sha + ".py")
    with lock(directory / "archive.lock"):
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=directory, prefix=".adapter-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o400)
                os.replace(temporary, path)
                sync_directory(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if file_digest(path) != sha:
            raise ValueError("Archived agent adapter changed")
    return reference(path)


def pinned_adapter(bridge_root, sha):
    """Load verified source, without changing the currently imported application module."""
    from . import root_path
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ValueError("Invalid agent adapter digest")
    if file_digest(Path(__file__)) == sha:
        return None
    path = root_path(bridge_root) / "adapters" / (sha + ".py")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != sha:
        raise ValueError("Archived agent adapter changed")
    module = ModuleType("ecarsi._agent_adapter_" + sha)
    module.__file__, module.__package__ = str(path), "ecarsi.bridge"
    exec(compile(content, str(path), "exec"), module.__dict__)
    return module


def validate_spec(spec):
    from jsonschema import Draft202012Validator
    from ..warm_pool.state import pool_root
    from . import root_path
    required = {"session_id", "dataset_id", "prompt", "tools", "pool_root", "bridge_root",
                "output_root", "max_turns"}
    if not isinstance(spec, dict) or not required <= spec.keys() or spec.keys() - required - {"trace", "completion_tool", "tool_state"}:
        raise ValueError("Agent session requires explicit identity, prompt, tools, services and max_turns")
    identifier(spec["session_id"])
    if len(spec["session_id"]) > 60:
        raise ValueError("session_id must leave room for turn and tool IDs")
    for key in ("prompt", "dataset_id"):
        if not isinstance(spec[key], str) or not spec[key].strip() or len(spec[key]) > 65536:
            raise ValueError(key + " must be nonempty")
    if type(spec["max_turns"]) is not int or not 1 <= spec["max_turns"] <= 100:
        raise ValueError("max_turns must be between 1 and 100")
    pool_root(spec["pool_root"])
    root_path(spec["bridge_root"])
    if not Path(spec["output_root"]).is_absolute():
        raise ValueError("output_root must be absolute")
    tools = spec["tools"]
    if not isinstance(tools, list) or not 1 <= len(tools) <= 16:
        raise ValueError("Register between 1 and 16 worker tools")
    names = set()
    for tool in tools:
        if set(tool) - {"multimodal", "read_only"} != {"name", "description", "parameters", "args", "cpus", "memory_mb",
                         "timeout_seconds", "inputs", "outputs", "result_file"}:
            raise ValueError("Each tool needs a fixed program, resource budget and output contract")
        if type(tool.get("read_only", False)) is not bool:
            raise ValueError("read_only must be boolean")
        if tool.get("read_only") and tool["name"] == spec.get("completion_tool"):
            raise ValueError("Decision submission cannot be a read-only tool")
        identifier(tool["name"])
        if tool["name"] in names:
            raise ValueError("Duplicate tool name")
        names.add(tool["name"])
        if not isinstance(tool["description"], str) or not tool["description"]:
            raise ValueError("Tool description is required")
        schema = tool["parameters"]
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("Tool arguments must be a closed JSON object")
        # Only a whole argv token is substituted; no shell/string interpolation.
        if not isinstance(tool["args"], list) or tool["args"].count("{arguments}") != 1:
            raise ValueError("Tool args must contain one {arguments} file token")
        if tool.get("multimodal", False) not in (True, False):
            raise ValueError("multimodal must be boolean")
        if "{state}" in tool["args"] and (tool["args"].count("{state}") != 1 or "tool_state" not in spec):
            raise ValueError("Stateful tools require one state token and an initial tool_state")
        validate({"request_id": "validation", "operation_id": tool["name"],
                  **{k: tool[k] for k in ("args", "cpus", "memory_mb", "timeout_seconds", "inputs", "outputs")}})
        if tool["result_file"] not in tool["outputs"]:
            raise ValueError("result_file must be a declared UTF-8 JSON output")
    if "trace" in spec:
        from ..warm_pool.state import validate_trace
        validate_trace(spec["trace"])
        if spec["trace"]["dataset_id"] != spec["dataset_id"]:
            raise ValueError("Session trace must belong to its dataset")
    if "completion_tool" in spec and spec["completion_tool"] not in names:
        raise ValueError("Completion tool must be registered")
    if "tool_state" in spec:
        verified(spec["tool_state"])
    return spec


def create_session(spec, *, recover_missing_submission=True):
    from ..model_web import normalized_models
    import agents
    spec = validate_spec(spec)
    root = Path(spec["output_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError("Session directory must be private and user-owned")
    path = root / "session.json"
    with lock(root / "session.lock"):
        old = read(path)
        if old is not None:
            # read_only is host batching policy, not a scientific contract, and the
            # saved session keeps its own. Everything else must still match exactly.
            def contract(value):
                return {**value, "tools": [{k: v for k, v in tool.items() if k != "read_only"}
                                           for tool in value["tools"]]}
            if contract(old["spec"]) != contract(spec):
                raise ValueError("Session directory already belongs to another specification")
            result = read(root / 'result.json')
            if (recover_missing_submission and old['spec'].get('completion_tool')
                    and old.get('protocol', 1) >= 2 and result and 'output' not in result
                    and result.get('session') == reference(path) and result.get('reply')
                    and verified(result['reply'])['response']['kind'] == 'final'):
                # One bounded repair with a fresh required-tool adapter. Keep
                # the old reply/session intact; re-read evidence from the
                # original state instead of crediting unseen observations.
                repair = dict(old['spec'], session_id='repair-' + digest([reference(path), result])[:32],
                              output_root=str(root / 'submission-recovery'))
                intent = immutable(root / 'submission-recovery.json', dict(
                    reason='Agent ended without its required validated submission',
                    session=reference(path), result=reference(root / 'result.json'), spec=repair))
                return create_session(verified(intent)['spec'], recover_missing_submission=False)
            return reference(path)
        config = read(Path(spec["bridge_root"]) / "config.json")
        models = normalized_models(read(config["catalog"]))
        if not models or models[0]["harness"] not in {"openai", "openai@vllm", "openai@openrouter"}:
            raise ValueError("Durable tool handoff currently requires the OpenAI Agents SDK harness")
        api = os.environ.get("OPENAI_AGENTS_API", "responses")
        if api not in {"responses", "chat_completions"}:
            raise ValueError("Unsupported Agents API mode")
        adapter = archive_adapter(spec["bridge_root"])
        save(path, {"spec": spec, "model": models[0], "api_mode": api,
                    "protocol": 2 if config.get("pool_root") else 1,
                    "sdk_version": agents.__version__, "adapter_sha256": adapter["sha256"]})
    return reference(path)


RESET_NOTE = ('\n\nContext reset: this is a fresh conversation because the previous transcript grew past what '
              'the model provider accepts. The host kept every tool state: accepted submissions, read markers '
              'and the pending scope all still stand. Use list_evidence and the status tools to see where you '
              'are, then continue from the next required step; do not repeat submissions the host already accepted.')


def reset_spec(spec, context, generation):
    """The same judgement in a fresh conversation: a new session id and directory, the prompt told why,
    and the host state the old transcript had reached as the initial tool_state."""
    base = re.sub(r'-g\d+$', '', spec['session_id'])
    root = Path(spec['output_root'])
    while root.name.startswith('generation-'):
        root = root.parent
    fresh = dict(spec, session_id=f'{base}-g{generation}', output_root=str(root / f'generation-{generation}'),
                 prompt=spec['prompt'] + RESET_NOTE)
    if 'tool_state' in spec:
        fresh['tool_state'] = context.get('tool_state') or verified(context['results'][-1]['output'])['state']
    return fresh


def reset_session(session_ref, context_ref, generation, reason):
    """Eye round 5, 2026-09-16: 37 turns, 4.5 M input tokens, then HTTP 400 on every attempt; the
    dataset died. Now the session restarts here, bounded by the workflow's reset count."""
    session = verified(session_ref)
    spec = reset_spec(session['spec'], verified(context_ref), generation)
    intent = immutable(Path(session['spec']['output_root']) / f'context-reset-{generation}.json',
                       dict(reason=reason, session=session_ref, context=context_ref, spec=spec))
    return create_session(verified(intent)['spec'], recover_missing_submission=False)


def validate_turn(spec):
    if set(spec) != {"request_id", "operation_id", "session", "context", "trace"}:
        raise ValueError("Agent turn requires session/context references and trace")
    session = verified(spec["session"])
    adapter = pinned_adapter(session["spec"]["bridge_root"], session["adapter_sha256"])
    if adapter is not None:
        return adapter.validate_turn(spec)
    if not spec["request_id"].startswith(session["spec"]["session_id"] + ".turn-"):
        raise ValueError("Turn request does not belong to its session")
    number = int(spec["request_id"].rsplit(".turn-", 1)[1])
    if not 0 <= number < session["spec"]["max_turns"] or (number == 0) != (spec["context"] is None):
        raise ValueError("Only the first model turn may start without saved context")
    if spec["context"] is not None:
        context = verified(spec["context"])
        verified(context["reply"])
        reply_path = Path(context["reply"]["path"])
        if reply_path.parent.name != f'{session["spec"]["session_id"]}.turn-{number-1}':
            raise ValueError("Continuation must follow the preceding model turn")
        reply = turn_reply(spec["session"], reply_path)
        if reply["kind"] != "tools" or context["sdk_state"] != reply["sdk_state"]:
            raise ValueError("Saved continuation does not match the model reply")
        from ..warm_pool.state import status
        for result in context["results"]:
            if status(session["spec"]["pool_root"], result["pool_request_id"])["state"] != "succeeded":
                raise ValueError("Tool result is no longer eligible to resume")
    if file_digest(Path(__file__)) != session["adapter_sha256"]:
        raise ValueError("Agent session adapter changed")


def portable_history(items):
    """Public messages and tool receipts, with no provider-owned response IDs."""
    result = []
    for item in items:
        kind = item.get("type", "message")
        if kind == "reasoning":
            continue  # Private provider reasoning is not a portable conversation artifact.
        if kind == "message":
            content = item["content"]
            if isinstance(content, list):
                content = [{"type": "input_text", "text": part["text"]}
                           if part["type"] == "output_text" else
                           {"type": "input_text", "text": part["refusal"]}
                           if part["type"] == "refusal" else part for part in content]
            result.append(dict(role=item["role"], content=content))
        elif kind in {"function_call", "function_call_output"}:
            keys = ("call_id", "name", "arguments") if kind == "function_call" else ("call_id", "output")
            result.append(dict(type=kind, **{key: item[key] for key in keys}))
        else:
            raise ValueError("Nonportable agent history item: " + kind)
    return result


async def run_turn(request, folder, *, model=None, portable_upgrade=False):
    """One model/tool boundary; no registered program can execute in this process."""
    session = verified(request["spec"]["session"])
    if portable_upgrade and session.get('protocol', 1) != 2:
        raise ValueError('Only portable sessions support an audited adapter upgrade')
    adapter = None if portable_upgrade else pinned_adapter(session["spec"]["bridge_root"], session["adapter_sha256"])
    if adapter is not None:
        return await adapter.run_turn(request, folder, model=model)
    from agents import Agent, FunctionTool, ModelSettings, RunConfig, Runner, RunState
    import agents
    from harness_bridge._harness_openai import _client, _model, PROVIDERS

    spec = request["spec"]
    validate_turn(spec)
    session = verified(spec["session"])
    if session["sdk_version"] != agents.__version__:
        raise ValueError("SDK version differs from saved session")
    context = verified(spec["context"]) if spec["context"] else None
    results = {r["call_id"]: r for r in context["results"]} if context else {}
    policy = {t["name"]: t for t in session["spec"]["tools"]}

    async def consume_result(ctx, arguments_json):
        result = results.get(ctx.tool_call_id)
        if (result is None or result["name"] != ctx.tool_name or
                result["arguments"] != json.loads(arguments_json)):
            raise ValueError("Tool has no matching accepted Pool result")
        output = verified(result["output"])
        if policy[ctx.tool_name].get("multimodal"):
            from agents import ToolOutputImage, ToolOutputText
            images = output.get("images", [])
            if not isinstance(images, list) or len(images) > 16 or any(
                    not isinstance(url, str) or not url.startswith("data:image/png;base64,") for url in images):
                raise ValueError("Worker image output must contain bounded PNG data URLs")
            return [ToolOutputText(text=json.dumps({k: v for k, v in output.items() if k not in {"images", "state"}})),
                    *[ToolOutputImage(image_url=url, detail="high") for url in images]]
        return json.dumps({k: v for k, v in output.items() if k != "state"}, ensure_ascii=True)

    tools = [FunctionTool(name=t["name"], description=t["description"],
              params_json_schema=t["parameters"], strict_json_schema=False,
              on_invoke_tool=consume_result, needs_approval=True) for t in policy.values()]
    chosen = model or session["model"]
    portable = session.get("protocol", 1) >= 2
    if chosen != session["model"] and not portable:
        raise ValueError("Legacy SDK sessions cannot switch model identity")
    provider = next(k for k, v in PROVIDERS.items() if v["harness"] == chosen["harness"])
    if chosen["url"]:
        os.environ[PROVIDERS[provider]["base_env"]] = chosen["url"]
    client = _client(provider)
    # Persist response shape, never prompts, arguments, credentials or clinical data.
    if hasattr(client, '_client'):
        async def observe(response):
            await response.aread()
            try:
                body = response.json()
            except ValueError:
                body = {}
            save(folder / 'provider-response.json', provider_summary(response.status_code, body))
        client._client.event_hooks['response'].append(observe)
    try:
        batchable = [t["name"] for t in policy.values() if t.get("read_only")] if portable else []
        instructions = session["spec"]["prompt"]
        if batchable:
            instructions += ("\n\nBatch independent evidence requests in the same model turn when their "
                "arguments are already known. Eligible read-only tools: " + ", ".join(batchable) +
                ". Registered independent reads may execute concurrently; changes remain ordered. Request all other tools individually. "
                "Never submit a decision until its required evidence has been returned and reviewed.")
        agent = Agent(name="RSI durable agent", instructions=instructions,
                      model=_model(chosen["model"], session["api_mode"], client), tools=tools,
                      # Completion is enforced by the host, not provider-specific
                      # forced-call decoding (which can return an empty response).
                      model_settings=ModelSettings(parallel_tool_calls=bool(batchable), store=False))
        run_input = "Carry out the task using the registered tools, then report the result."
        before_in = before_out = 0
        if context and portable:
            from agents import ItemHelpers
            from openai.types.responses import ResponseFunctionToolCall
            reply = verified(context["reply"])["response"]
            run_input = portable_history(reply["input_history"])
            for call in reply["calls"]:
                output = await consume_result(SimpleNamespace(tool_call_id=call["call_id"], tool_name=call["name"]),
                                              json.dumps(call["arguments"]))
                tool_call = ResponseFunctionToolCall(type="function_call", **{
                    **call, "arguments": json.dumps(call["arguments"])})
                run_input.append(ItemHelpers.tool_call_output_item(tool_call, output))
            before_in, before_out = context["usage_total"]["tokens_in"], context["usage_total"]["tokens_out"]
        elif context:
            run_input = await RunState.from_json(agent, context["sdk_state"])
            pending = run_input.get_interruptions()
            if {i.raw_item.call_id for i in pending} != set(results):
                raise ValueError("Continuation must cover every pending tool exactly once")
            for item in pending:
                result = results[item.raw_item.call_id]
                if result["name"] != item.raw_item.name or result["arguments"] != json.loads(item.raw_item.arguments):
                    raise ValueError("Continuation does not match original tool call")
                verified(result["output"])
                run_input.approve(item)
            before_in, before_out = context["usage_total"]["tokens_in"], context["usage_total"]["tokens_out"]
        result = await Runner.run(agent, run_input, max_turns=session["spec"]["max_turns"],
                                  run_config=RunConfig(tracing_disabled=True))
        usage = result.context_wrapper.usage
        total = {"tokens_in": usage.input_tokens + (before_in if portable else 0),
                 "tokens_out": usage.output_tokens + (before_out if portable else 0)}
        calls = [{"call_id": i.raw_item.call_id, "name": i.raw_item.name,
                  "arguments": json.loads(i.raw_item.arguments)} for i in result.interruptions]
        response = {"kind": "tools" if calls else "final", "calls": calls,
                    "sdk_state": result.to_state().to_json() if calls else None,
                    "final_output": result.final_output if not calls else None,
                    "model": chosen, "usage_total": total,
                    "usage": {"tokens_in": total["tokens_in"] - before_in,
                              "tokens_out": total["tokens_out"] - before_out, "cost_usd": None}}
        if portable:
            response["input_history"] = portable_history(result.to_input_list())
        # Save the resumable result before executor teardown and the Bridge acknowledgement.
        save(folder / "turn-response.json", response)
        return response
    finally:
        await client.close()


def provider_summary(http_status, body):
    """Bounded diagnostic metadata for empty, truncated and rejected responses."""
    return dict(http_status=http_status, response_id=body.get('id'), status=body.get('status'),
        usage=body.get('usage'), incomplete_details=body.get('incomplete_details'),
        error_code=(body.get('error') or {}).get('code'),
        output=[dict(type=item.get('type'), name=item.get('name'),
                     argument_chars=len(item.get('arguments') or ''),
                     text_chars=sum(len(part.get('text') or '') for part in item.get('content', [])))
                for item in body.get('output', [])],
        choices=[dict(finish_reason=item.get('finish_reason'),
                      text_chars=len(item.get('message', {}).get('content') or ''),
                      tool_calls=len(item.get('message', {}).get('tool_calls') or []))
                 for item in body.get('choices', [])])


def submit_turn(session_ref, number, context_ref=None, parents=()):
    from . import submit
    session = verified(session_ref)
    s = session["spec"]
    if not 0 <= number < s["max_turns"]:
        raise ValueError("Agent turn budget exhausted")
    request_id = f'{s["session_id"]}.turn-{number}'
    submit(s["bridge_root"], {"request_id": request_id, "operation_id": "agent.turn",
           "session": session_ref, "context": context_ref,
           "trace": {"workflow_id": "agent/" + s["session_id"], "dataset_id": s["dataset_id"],
                     "unit_id": "agent.model", **s.get("trace", {}),
                     "depends_on": list(parents) if number else s.get("trace", {}).get("depends_on", list(parents))}})
    return request_id


def turn_reply(session_ref, path):
    from . import status
    s = verified(session_ref)["spec"]
    path = Path(path).resolve(strict=True)
    if path.name != "result.json" or path.parent.parent != Path(s["bridge_root"]).resolve() / "requests":
        raise ValueError("Reply is outside this session's Bridge")
    request = read(path.parent / "request.json")
    if request["spec"].get("session") != session_ref or status(s["bridge_root"], path.parent.name)["state"] != "reply_saved":
        raise ValueError("Reply does not belong to this session or is not accepted")
    return read(path)["response"]


def tool_request(session_ref, reply_path, index, previous=None):
    """Coordinator validates model arguments against the immutable registered tool policy."""
    from jsonschema import Draft202012Validator
    from ..warm_pool.state import submit
    session = verified(session_ref)
    s = session["spec"]
    reply_path = Path(reply_path)
    reply = turn_reply(session_ref, reply_path)
    calls = reply["calls"]
    if reply["kind"] != "tools" or not 1 <= len(calls) <= 64 or len({c["call_id"] for c in calls}) != len(calls):
        raise ValueError("Expected distinct pending tool calls")
    if len(calls) > 1:
        # Current host policy, not only the session's saved copy of it: sessions
        # saved before a tool was declared read-only otherwise reject the same
        # batch turn after turn. Parallel execution still needs the saved flag.
        from .parallel import READS
        readonly = {t["name"] for t in s["tools"] if t.get("read_only") or t["name"] in READS}
        if any(call["name"] not in readonly or call["name"] == s.get("completion_tool") for call in calls):
            raise ToolRejection("Batch only declared read-only tools; request decisions and changes individually")
    call = calls[index]
    if len(json.dumps(call)) > 262144:
        raise ToolRejection("Tool arguments exceed the 256 KiB handoff limit")
    tool = next((t for t in s["tools"] if t["name"] == call["name"]), None)
    if tool is None:
        raise ToolRejection("Unregistered agent tool")
    values = call["arguments"]
    if isinstance(values, dict):
        # Some model adapters decode an explicitly JSON-valued string field.
        # Canonicalize only that lossless representation; all policy validation remains.
        fields = tool["parameters"].get("properties", {})
        values = {name: json.dumps(value, allow_nan=False)
                  if name.endswith("_json") and fields.get(name, {}).get("type") == "string"
                  and isinstance(value, (dict, list)) else value for name, value in values.items()}
    Draft202012Validator(tool["parameters"]).validate(values)
    if len(json.dumps(values)) > 262144:
        raise ToolRejection("Tool arguments exceed the 256 KiB handoff limit")
    turn_id = reply_path.parent.name
    request_id = s["session_id"] + ".tool-" + digest([turn_id, call["call_id"]])[:16]
    directory = Path(s["output_root"]) / request_id
    directory.mkdir(mode=0o700, exist_ok=True)
    arguments = immutable(directory / "arguments.json", values)
    args = [arguments["path"] if a == "{arguments}" else a for a in tool["args"]]
    state_inputs = []
    if "{state}" in args:
        state_ref = s["tool_state"]
        prior_result = None
        if previous:
            from ..warm_pool.state import status
            prior = status(s["pool_root"], previous)
            if prior["state"] != "succeeded":
                raise ValueError("Previous tool has no accepted state")
            prior_request = read(Path(s["pool_root"]) / "requests" / previous / "request.json")
            prior_tool = next(t for t in s["tools"] if t["name"] == prior_request["spec"]["operation_id"])
            expected = Path(s["pool_root"]) / "requests" / previous / prior["attempt_id"] / "outputs" / prior_tool["result_file"]
            source = next(o for o in prior["receipt"]["outputs"] if Path(o["path"]) == expected)
            prior_result = verified({k: source[k] for k in ("path", "sha256")})
        else:
            saved = read(reply_path.parent / "request.json")["spec"].get("context")
            if saved:
                context = verified(saved)
                prior_result = ({'state': context['tool_state']} if 'tool_state' in context else
                                verified(context["results"][-1]["output"]))
        if prior_result is not None:
            state_ref = prior_result["state"]
        verified(state_ref)
        state_inputs = [state_ref]
        args = [state_ref["path"] if a == "{state}" else a for a in args]
    request = {"request_id": request_id, "operation_id": tool["name"], "args": args,
           **{k: tool[k] for k in ("cpus", "memory_mb", "timeout_seconds", "outputs")},
           "inputs": [arguments, reference(reply_path), *state_inputs, *tool["inputs"]],
           "trace": {"workflow_id": "agent/" + s["session_id"], "dataset_id": s["dataset_id"],
                     "unit_id": tool["name"], **s.get("trace", {}), "depends_on": [previous or turn_id]}}
    if len(calls) > 1:
        # The model already requested this batch. Prefetching its other calls
        # would return the same evidence twice in the continuation.
        from .tool_execution import plan
        request = plan(request, directory, s['pool_root'])
    else:
        from .evidence import plan
        request = plan(request, directory, s)
    if (state_inputs and len(args) == 6 and args[:3] == ['-m', 'ecarsi.stages.persample', 'tool']
            and tool['name'] in {'check_genes', 'check_qc_scores', 'submit_annotation'}):
        state = verified(state_ref)
        if state['version'] == 0:
            from ..warm_pool.budget import from_compute
            request = from_compute(request, state['bundle'], directory / 'resources.json', s['pool_root'])
    from .parallel import budget
    request = budget(request, directory, s, tool, state_ref if state_inputs else None)
    submit(s["pool_root"], request)
    return {"request_id": request_id, "result_file": tool["result_file"], "index": index}


REPEAT_LIMIT = 3


def repeated_rejections(reply_path, results):
    """How many consecutive turns, ending with this one, made the same single call and had it rejected.
    Eye 2026-09-17: 25 identical rejected proposals in two minutes, 167k input tokens each, for one
    stray quote; the session would have burnt its 80 turns without a chance of converging."""
    from ..warm_pool.state import digest
    if len(results) != 1 or not verified(results[0]["output"]).get("is_error"):
        return 0
    key = digest([results[0]["name"], results[0]["arguments"]])
    count = 1
    earlier = read(Path(reply_path).parent / "request.json")["spec"].get("context")
    while earlier and count < REPEAT_LIMIT:
        previous = verified(earlier)
        last = (previous.get("results") or [None])[-1]
        if (not last or len(previous["results"]) != 1 or digest([last["name"], last["arguments"]]) != key
                or not verified(last["output"]).get("is_error")):
            break
        count += 1
        earlier = read(Path(previous["reply"]["path"]).parent / "request.json")["spec"].get("context")
    return count


def continuation(session_ref, reply_path, accepted, parallel=False):
    s = verified(session_ref)["spec"]
    reply = turn_reply(session_ref, reply_path)
    if [a["index"] for a in accepted] != list(range(len(reply["calls"]))):
        raise ValueError("Missing or reordered tool results")
    results = []
    from ..warm_pool.state import status
    for item in accepted:
        current = status(s["pool_root"], item["request_id"])
        if current["state"] != "succeeded":
            raise ValueError("Only successful, uncancelled Pool results may resume an agent")
        outputs = current["receipt"]["outputs"]
        output = next((o for o in outputs if o["path"] == item["path"]), None)
        tool = next(t for t in s["tools"] if t["name"] == reply["calls"][item["index"]]["name"])
        limit = 16 * 2**20 if tool.get("multimodal") else 262144
        if output is None or Path(output["path"]).stat().st_size > limit:
            raise ValueError("Tool result exceeds its declared JSON handoff limit")
        ref = {k: output[k] for k in ("path", "sha256")}
        verified(ref)
        results.append({**reply["calls"][item["index"]], "output": ref, "pool_request_id": item["request_id"]})
    context = {"reply": reference(reply_path), "sdk_state": reply["sdk_state"],
               "usage_total": reply["usage_total"], "results": results}
    repeats = repeated_rejections(reply_path, results)
    if repeats >= REPEAT_LIMIT:
        raise ValueError(f"Model repeated an identical rejected {results[0]['name']} call {repeats} times; "
                         "the session cannot converge and is stopped")
    if parallel:
        from .parallel import eligible, merge_states
        if not eligible(s, reply['calls']):
            raise ValueError('Unregistered parallel evidence batch')
        prior = read(Path(reply_path).parent / 'request.json')['spec'].get('context')
        base = s['tool_state']
        if prior:
            previous = verified(prior)
            base = previous.get('tool_state') or verified(previous['results'][-1]['output'])['state']
        merged = merge_states(verified(base), [verified(verified(r['output'])['state']) for r in results])
        context['tool_state'] = immutable(Path(s['output_root']) / (Path(reply_path).parent.name + '.state.json'), merged)
    return immutable(Path(s["output_root"]) / (Path(reply_path).parent.name + ".continuation.json"), context)


def complete_tool(session_ref, context_ref):
    """A registered submit tool can finish with an accepted, verified worker result."""
    s = verified(session_ref)["spec"]
    context = verified(context_ref)
    reply = turn_reply(session_ref, context["reply"]["path"])
    if context["sdk_state"] != reply["sdk_state"]:
        raise ValueError("Completion context does not match the model reply")
    result = context["results"][-1]
    if result["name"] != s.get("completion_tool"):
        return None
    from ..warm_pool.state import status
    current = status(s["pool_root"], result["pool_request_id"])
    if current["state"] != "succeeded" or result["output"] not in [
            {k: o[k] for k in ("path", "sha256")} for o in current["receipt"]["outputs"]]:
        raise ValueError("Completion requires an accepted, uncancelled tool result")
    output = verified(result["output"])
    if output.get("accepted") is not True:
        return None
    return immutable(Path(s["output_root"]) / "result.json", {
        "session": session_ref, "reply": context["reply"], "output": result["output"],
        "pool_request_id": result["pool_request_id"]})["path"]

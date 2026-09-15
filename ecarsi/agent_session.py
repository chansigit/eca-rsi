"""Durable agent tool boundaries: Bridge calls models; Pool executes registered programs.

Tools are trusted application registrations, never model-selected commands or budgets.
The Agents SDK interruption is a machine handoff, not a human approval request.
"""
import json
import os
from pathlib import Path

from .warm_pool.state import digest, file_digest, identifier, lock, read, save, validate


def reference(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": file_digest(path)}


def verified(ref):
    if set(ref) != {"path", "sha256"} or not Path(ref["path"]).is_absolute():
        raise ValueError("Expected absolute artifact reference")
    if file_digest(ref["path"]) != ref["sha256"]:
        raise ValueError("Artifact changed: " + ref["path"])
    return read(ref["path"])


def immutable(path, value):
    path = Path(path)
    with lock(path.with_suffix(path.suffix + ".lock")):
        old = read(path)
        if old is not None and digest(old) != digest(value):
            raise ValueError("Conflicting durable content: " + str(path))
        if old is None:
            save(path, value)
    return reference(path)


def validate_spec(spec):
    from jsonschema import Draft202012Validator
    from .warm_pool.state import pool_root
    from .agent_bridge import root_path
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
        if set(tool) - {"multimodal"} != {"name", "description", "parameters", "args", "cpus", "memory_mb",
                         "timeout_seconds", "inputs", "outputs", "result_file"}:
            raise ValueError("Each tool needs a fixed program, resource budget and output contract")
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
        from .warm_pool.state import validate_trace
        validate_trace(spec["trace"])
        if spec["trace"]["dataset_id"] != spec["dataset_id"]:
            raise ValueError("Session trace must belong to its dataset")
    if "completion_tool" in spec and spec["completion_tool"] not in names:
        raise ValueError("Completion tool must be registered")
    if "tool_state" in spec:
        verified(spec["tool_state"])
    return spec


def create_session(spec):
    from .model_web import normalized_models
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
            if old["spec"] != spec:
                raise ValueError("Session directory already belongs to another specification")
            return reference(path)
        config = read(Path(spec["bridge_root"]) / "config.json")
        models = normalized_models(read(config["catalog"]))
        if not models or models[0]["harness"] not in {"openai", "openai@vllm", "openai@openrouter"}:
            raise ValueError("Durable tool handoff currently requires the OpenAI Agents SDK harness")
        api = os.environ.get("OPENAI_AGENTS_API", "responses")
        if api not in {"responses", "chat_completions"}:
            raise ValueError("Unsupported Agents API mode")
        save(path, {"spec": spec, "model": models[0], "api_mode": api,
                    "sdk_version": agents.__version__, "adapter_sha256": file_digest(Path(__file__))})
    return reference(path)


def validate_turn(spec):
    if set(spec) != {"request_id", "operation_id", "session", "context", "trace"}:
        raise ValueError("Agent turn requires session/context references and trace")
    session = verified(spec["session"])
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
        from .warm_pool.state import status
        for result in context["results"]:
            if status(session["spec"]["pool_root"], result["pool_request_id"])["state"] != "succeeded":
                raise ValueError("Tool result is no longer eligible to resume")
    if file_digest(Path(__file__)) != session["adapter_sha256"]:
        raise ValueError("Agent session adapter changed")


async def run_turn(request, folder):
    """One model/tool boundary; no registered program can execute in this process."""
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
    chosen = session["model"]
    provider = next(k for k, v in PROVIDERS.items() if v["harness"] == chosen["harness"])
    if chosen["url"]:
        os.environ[PROVIDERS[provider]["base_env"]] = chosen["url"]
    client = _client(provider)
    try:
        agent = Agent(name="RSI durable agent", instructions=session["spec"]["prompt"],
                      model=_model(chosen["model"], session["api_mode"], client), tools=tools,
                      model_settings=ModelSettings(parallel_tool_calls=False, store=False))
        run_input = "Carry out the task using the registered tools, then report the result."
        before_in = before_out = 0
        if context:
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
        total = {"tokens_in": usage.input_tokens, "tokens_out": usage.output_tokens}
        calls = [{"call_id": i.raw_item.call_id, "name": i.raw_item.name,
                  "arguments": json.loads(i.raw_item.arguments)} for i in result.interruptions]
        response = {"kind": "tools" if calls else "final", "calls": calls,
                    "sdk_state": result.to_state().to_json() if calls else None,
                    "final_output": result.final_output if not calls else None,
                    "model": chosen, "usage_total": total,
                    "usage": {"tokens_in": total["tokens_in"] - before_in,
                              "tokens_out": total["tokens_out"] - before_out, "cost_usd": None}}
        # Save the resumable result before executor teardown and the Bridge acknowledgement.
        save(folder / "turn-response.json", response)
        return response
    finally:
        await client.close()


def submit_turn(session_ref, number, context_ref=None, parents=()):
    from .agent_bridge import submit
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
    from .agent_bridge import status
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
    from .warm_pool.state import submit
    session = verified(session_ref)
    s = session["spec"]
    reply_path = Path(reply_path)
    reply = turn_reply(session_ref, reply_path)
    calls = reply["calls"]
    if reply["kind"] != "tools" or not 1 <= len(calls) <= 16 or len({c["call_id"] for c in calls}) != len(calls):
        raise ValueError("Expected distinct pending tool calls")
    call = calls[index]
    if len(json.dumps(call)) > 262144:
        raise ValueError("Tool arguments exceed the 256 KiB handoff limit")
    tool = next((t for t in s["tools"] if t["name"] == call["name"]), None)
    if tool is None:
        raise ValueError("Unregistered agent tool")
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
        raise ValueError("Tool arguments exceed the 256 KiB handoff limit")
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
            from .warm_pool.state import status
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
                prior_result = verified(verified(saved)["results"][-1]["output"])
        if prior_result is not None:
            state_ref = prior_result["state"]
        verified(state_ref)
        state_inputs = [state_ref]
        args = [state_ref["path"] if a == "{state}" else a for a in args]
    submit(s["pool_root"], {"request_id": request_id, "operation_id": tool["name"], "args": args,
           **{k: tool[k] for k in ("cpus", "memory_mb", "timeout_seconds", "outputs")},
           "inputs": [arguments, reference(reply_path), *state_inputs, *tool["inputs"]],
           "trace": {"workflow_id": "agent/" + s["session_id"], "dataset_id": s["dataset_id"],
                     "unit_id": tool["name"], **s.get("trace", {}), "depends_on": [previous or turn_id]}})
    return {"request_id": request_id, "result_file": tool["result_file"], "index": index}


def continuation(session_ref, reply_path, accepted):
    s = verified(session_ref)["spec"]
    reply = turn_reply(session_ref, reply_path)
    if [a["index"] for a in accepted] != list(range(len(reply["calls"]))):
        raise ValueError("Missing or reordered tool results")
    results = []
    from .warm_pool.state import status
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
    return immutable(Path(s["output_root"]) / (Path(reply_path).parent.name + ".continuation.json"),
                     {"reply": reference(reply_path), "sdk_state": reply["sdk_state"],
                      "usage_total": reply["usage_total"], "results": results})


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
    from .warm_pool.state import status
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

"""Synthetic chat-normalized coding profiles; never execute returned tools."""
import copy
import hashlib
import json
import random
from pathlib import Path

PROFILES = {
    "claude-code": ("Read", "Bash", "Edit", "Grep"),
    "codex": ("exec_command", "apply_patch", "read_file", "update_plan"),
    "opencode": ("read", "bash", "edit", "grep"),
    "deepseek": ("read_file", "run_terminal", "replace_in_file", "search_code"),
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def source_text(key, chars):
    rng = random.Random(key)
    blocks = []
    size = 0
    while size < chars:
        tag = f"{rng.getrandbits(64):016x}"
        block = (f"# file: src/services/worker_{tag}.py\n"
                 f"async def resolve_{tag}(store, tenant_id, request):\n"
                 "    key = (tenant_id, request.session_id)\n"
                 "    cached = await store.get(key)\n"
                 "    if cached is not None:\n        return cached\n"
                 "    result = await store.compute(request.payload)\n"
                 "    await store.put(key, result, ttl=300)\n    return result\n\n")
        blocks.append(block)
        size += len(block)
    return "".join(blocks)[:chars]


def tools_for(profile):
    names = PROFILES[profile]
    schemas = [
        {"file_path": {"type": "string"}},
        {"command": {"type": "string"}},
        {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}},
        {"pattern": {"type": "string"}},
    ]
    if profile == "codex":
        schemas = [{"cmd": {"type": "string"}}, {"patch": {"type": "string"}},
                   {"path": {"type": "string"}}, {"plan": {"type": "array", "items": {"type": "string"}}}]
    return [{"type": "function", "function": {
        "name": name, "description": f"Coding workspace operation: {name}. Return structured output.",
        "parameters": {"type": "object", "properties": schema, "required": list(schema)},
    }} for name, schema in zip(names, schemas)]


def initial_messages(profile, index, args):
    project = index % args.projects
    # Put diversity BEFORE the long shared context, not in an ineffective suffix.
    key = f"{args.seed}:{profile}:project-{project}"
    if args.prefix_mode == "unique":
        key += f":session-{index}"
    elif args.prefix_mode == "shared":
        key = f"{args.seed}:{profile}:shared"
    system = (f"Workspace fingerprint: {digest(key)}\n"
              f"You are a {profile}-style coding assistant. Inspect files, explain changes, "
              "use tools to implement and test a fix. Preserve existing APIs.\n"
              "<project_instructions>Use async I/O; isolate tenants; write regression tests; "
              "never log credentials. Follow the repository conventions.</project_instructions>\n"
              "<repository_context>\n" + source_text(key, args.context_chars) + "\n</repository_context>")
    messages = [{"role": "system", "content": system}, {"role": "user", "content":
        f"Task {index}: fix stale cache reads under concurrent coding sessions. Inspect source and tests."}]
    for turn in range(args.history_turns):
        tool = tools_for(profile)[0]["function"]
        call_id = f"history_{index}_{turn}"
        properties = tool["parameters"]["properties"]
        arguments = {k: ("src/services/cache.py" if "path" in k else "rg cache src") for k in properties}
        messages += [{"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function", "function": {
                "name": tool["name"], "arguments": json.dumps(arguments)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": source_text(
                f"{key}:history:{index}:{turn}", args.tool_chars)}]
    return messages


def advance(messages, assistant, index, turn, args):
    messages.append(assistant)
    for call in assistant.get("tool_calls", []):
        messages.append({"role": "tool", "tool_call_id": call["id"], "content":
            "Simulated tool output (no command executed):\n" + source_text(
                f"{args.seed}:{index}:{turn}:{call['id']}", args.tool_chars)})
    if not assistant.get("tool_calls"):
        messages.append({"role": "user", "content":
            f"Iteration {turn + 1}: inspect this additional file and extend the regression tests.\n" +
            source_text(f"{args.seed}:{index}:followup:{turn}", args.tool_chars)})


def load_replay(path):
    """Each JSONL row is one session with ordered, full chat request bodies."""
    if not path:
        return None
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("Replay file is empty")
    for row in rows:
        if not isinstance(row.get("requests"), list) or not row["requests"]:
            raise ValueError("Each replay row needs a nonempty requests array")
        for body in row["requests"]:
            if not isinstance(body.get("messages"), list) or not body["messages"]:
                raise ValueError("Replay accepts full Chat Completions bodies with messages")
            if body.get("n", 1) != 1:
                raise ValueError("Only n=1 is supported")
    return rows


def body_for(args, messages, profile, replay_body=None):
    body = copy.deepcopy(replay_body) if replay_body is not None else {
        "messages": messages, "tools": tools_for(profile), "tool_choice": "auto",
        "max_tokens": args.max_tokens, "temperature": 0.2,
    }
    body.update(model=args.model, stream=True)
    if not args.no_stream_usage:
        body["stream_options"] = {**body.get("stream_options", {}), "include_usage": True}
    else:
        body.pop("stream_options", None)
    return body

"""Talking to a model about your kitchen.

This is the one part of HomeStock that makes outbound network calls, and it
only does so when the user has configured a provider and a key. With nothing
configured, nothing leaves the machine — the rest of the app works exactly as
it did before.

Four providers, two wire formats. OpenRouter, OpenAI and Ollama all speak the
OpenAI chat-completions shape; Anthropic has its own. Everything is stdlib
urllib, because a household inventory app should not grow a dependency tree to
send a POST.

The tool surface handed to the model is the MCP tool surface, read straight off
the FastMCP tool manager — same names, same JSON Schema, same docstrings the
server already publishes. There is deliberately no second list to maintain: a
tool added to server.py is available in chat the moment it exists.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from homestock import server

# How many times the model may call tools before we stop and answer with what
# we have. A kitchen question needs two or three rounds; anything past this is
# a loop, and an unbounded loop against a metered API is the user's money.
MAX_TOOL_ROUNDS = 6
HTTP_TIMEOUT = 120

# Tools that change the event log. The model may call them, but a UI can show
# the user which turns wrote something rather than leaving it to be discovered.
WRITING_TOOLS = frozenset({
    "add_items", "correct_stock", "discard_item", "consume_items", "void_event",
    "set_shelf_life", "merge_items", "add_alias", "add_capture",
    "resolve_capture", "record_ingest_run",
})

PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "label": "Ollama (on this machine)",
        "wire": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "needs_key": False,
        "default_model": "llama3.1",
        "leaves_machine": False,
        "note": "Runs locally. Nothing leaves this computer.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "wire": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "needs_key": True,
        "default_model": "anthropic/claude-sonnet-5",
        "leaves_machine": True,
        "note": "One key, most models. Your messages and the kitchen data the "
                "model asks for are sent to OpenRouter and on to the model you pick.",
    },
    "openai": {
        "label": "OpenAI",
        "wire": "openai",
        "base_url": "https://api.openai.com/v1",
        "needs_key": True,
        "default_model": "gpt-5",
        "leaves_machine": True,
        "note": "Your messages and the kitchen data the model asks for are sent to OpenAI.",
    },
    "anthropic": {
        "label": "Anthropic",
        "wire": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "needs_key": True,
        "default_model": "claude-sonnet-5",
        "leaves_machine": True,
        "note": "Your messages and the kitchen data the model asks for are sent to Anthropic.",
    },
}

SYSTEM_PROMPT = """You are the assistant inside HomeStock, a household inventory app.

You can see what this household has bought, what is probably in the house now,
what is running low and what is about to go off, by calling the tools provided.

How to be useful here:
- Call tools rather than guessing. `get_stock()` with no argument lists every
  known item; `what_should_i_order()` is the shopping list; `get_expiring_soon()`
  is what needs eating.
- Stock is an *estimate* derived from purchase history, not a fact. Say so when
  it matters, and prefer "you probably have" over "you have". Every row carries
  a confidence and the reasoning behind it — use them.
- When the user tells you something about reality ("we're out of milk", "I used
  the butter"), record it with `correct_stock` or `consume_items`. A correction
  outranks the model's guess and resets its clock, which is the whole point.
- Item names must match existing ones exactly. Check `get_stock()` first; use
  `add_alias` or `merge_items` when the same thing has arrived under two names.
- Be brief. This is a kitchen, not a report."""


def available_providers() -> list[dict]:
    """What the settings UI offers, and what each one costs in privacy."""
    return [{"id": pid, **{k: v for k, v in p.items() if k != "wire"}}
            for pid, p in PROVIDERS.items()]


def tool_schemas(wire: str) -> list[dict]:
    """The MCP tool surface, in the shape this provider expects.

    Read from the FastMCP tool manager rather than hand-listed, so a tool added
    to server.py needs no change here and the two can never disagree."""
    out = []
    for t in server.mcp._tool_manager.list_tools():
        schema = t.parameters or {"type": "object", "properties": {}}
        desc = (t.description or "").strip()
        if wire == "anthropic":
            out.append({"name": t.name, "description": desc, "input_schema": schema})
        else:
            out.append({"type": "function", "function": {
                "name": t.name, "description": desc, "parameters": schema}})
    return out


def run_tool(name: str, args: dict) -> Any:
    """Execute one tool call. Never raises: a model that sent bad arguments
    should be told so and allowed to try again, not crash the conversation."""
    tool = server.mcp._tool_manager.get_tool(name)
    if tool is None:
        return {"error": f"no such tool {name!r}"}
    try:
        return tool.fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}


def _post(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise ChatError(f"{e.code} from the provider: {detail}") from e
    except urllib.error.URLError as e:
        raise ChatError(f"could not reach the provider: {e.reason}") from e
    except TimeoutError as e:
        raise ChatError("the provider timed out") from e


class ChatError(Exception):
    """Something went wrong talking to a provider. Shown to the user as-is, so
    the message has to be intelligible to someone who is not a programmer."""


def _openai_round(cfg, model, key, messages, tools, base_url):
    payload = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    headers = {"authorization": f"Bearer {key}"} if key else {}
    data = _post(f"{base_url}/chat/completions", payload, headers)
    if "choices" not in data:
        raise ChatError(f"unexpected reply from the provider: {json.dumps(data)[:300]}")
    msg = data["choices"][0]["message"]
    calls = [{"id": c["id"], "name": c["function"]["name"],
              "args": json.loads(c["function"]["arguments"] or "{}")}
             for c in (msg.get("tool_calls") or [])]
    return msg.get("content") or "", calls, msg


def _anthropic_round(cfg, model, key, messages, tools, base_url):
    system = [m["content"] for m in messages if m["role"] == "system"]
    convo = [m for m in messages if m["role"] != "system"]
    payload = {"model": model, "max_tokens": 2048, "messages": convo}
    if system:
        payload["system"] = system[0]
    if tools:
        payload["tools"] = tools
    data = _post(f"{base_url}/messages", payload,
                 {"x-api-key": key or "", "anthropic-version": "2023-06-01"})
    if "content" not in data:
        raise ChatError(f"unexpected reply from the provider: {json.dumps(data)[:300]}")
    text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
             for b in data["content"] if b.get("type") == "tool_use"]
    return text, calls, {"role": "assistant", "content": data["content"]}


def chat(messages: list[dict], provider: str, model: str | None = None,
         api_key: str | None = None, base_url: str | None = None,
         use_tools: bool = True) -> dict:
    """One turn of conversation, tools and all.

    `messages` is the history in OpenAI shape ({role, content}); the system
    prompt is prepended here so a client cannot forget it. Returns
    {reply, tool_calls: [{name, args, wrote}], rounds} — the tool calls are
    returned so the UI can show its working, which is the same promise the
    pantry window makes about every number it prints.
    """
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise ChatError(f"unknown provider {provider!r}")
    if cfg["needs_key"] and not api_key:
        raise ChatError(f"{cfg['label']} needs an API key. Add one in Settings.")

    model = model or cfg["default_model"]
    base_url = (base_url or cfg["base_url"]).rstrip("/")
    tools = tool_schemas(cfg["wire"]) if use_tools else []
    run_round = _anthropic_round if cfg["wire"] == "anthropic" else _openai_round

    convo = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
    performed: list[dict] = []

    for round_no in range(MAX_TOOL_ROUNDS + 1):
        # The final round is asked without tools, so the model has to answer.
        # If a provider hands back tool calls regardless, they are ignored
        # rather than looped on: the user gets the text and the turn ends.
        last = round_no == MAX_TOOL_ROUNDS
        text, calls, raw = run_round(cfg, model, api_key, convo, [] if last else tools, base_url)
        if last or not calls:
            return {"reply": text, "tool_calls": performed, "rounds": round_no + 1,
                    "hit_tool_limit": bool(last and calls)}

        convo.append(raw)
        results = []
        for c in calls:
            result = run_tool(c["name"], c["args"])
            performed.append({"name": c["name"], "args": c["args"],
                              "wrote": c["name"] in WRITING_TOOLS})
            results.append((c, result))

        if cfg["wire"] == "anthropic":
            convo.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": c["id"],
                 "content": json.dumps(r, default=str)} for c, r in results]})
        else:
            convo.extend({"role": "tool", "tool_call_id": c["id"],
                          "content": json.dumps(r, default=str)} for c, r in results)

    raise ChatError("the model kept calling tools without answering")  # unreachable

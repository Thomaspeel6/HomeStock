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

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from homestock import server

# How many times the model may call tools before we stop and answer with what
# we have. A kitchen question needs two or three rounds; anything past this is
# a loop, and an unbounded loop against a metered API is the user's money.
MAX_TOOL_ROUNDS = 6
# A cloud provider that has not answered in two minutes is not going to. A
# local model reading two receipt photographs on someone's laptop genuinely
# takes longer than that, and timing it out looks identical to a broken app.
HTTP_TIMEOUT = 120
LOCAL_HTTP_TIMEOUT = 900
# A receipt photo bigger than this is not a receipt photo.
MAX_IMAGE_BYTES = 12 * 1024 * 1024

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
        "default_model": "",
        "leaves_machine": False,
        "note": "Runs locally. Nothing leaves this computer.",
        "hint": "Needs Ollama running. Pick one of the models you have pulled.",
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


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ChatError(f"{e.code} from the provider: "
                        f"{e.read().decode('utf-8', 'replace')[:200]}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ChatError(f"could not reach the provider: {e}") from e


def list_models(provider: str, api_key: str | None = None,
                base_url: str | None = None) -> list[str]:
    """Which models this provider can actually serve right now.

    Settings used to be a free-text box, which meant typing a model name blind
    and finding out it was wrong only when a message failed. For Ollama it is
    worse than a typo: the answer depends on what the user has pulled, so no
    hardcoded default can be right.
    """
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise ChatError(f"unknown provider {provider!r}")
    if cfg["needs_key"] and not api_key:
        return []
    base = (base_url or cfg["base_url"]).rstrip("/")
    headers = ({"x-api-key": api_key or "", "anthropic-version": "2023-06-01"}
               if cfg["wire"] == "anthropic"
               else ({"authorization": f"Bearer {api_key}"} if api_key else {}))
    data = _get(f"{base}/models", headers)
    rows = data.get("data") or data.get("models") or []
    names = [r.get("id") or r.get("name") for r in rows if isinstance(r, dict)]
    return sorted(n for n in names if n)


def tool_schemas(wire: str, writes: bool = True) -> list[dict]:
    """The MCP tool surface, in the shape this provider expects.

    Read from the FastMCP tool manager rather than hand-listed, so a tool added
    to server.py needs no change here and the two can never disagree."""
    out = []
    for t in server.mcp._tool_manager.list_tools():
        if not writes and t.name in WRITING_TOOLS:
            continue
        schema = t.parameters or {"type": "object", "properties": {}}
        desc = (t.description or "").strip()
        if wire == "anthropic":
            out.append({"name": t.name, "description": desc, "input_schema": schema})
        else:
            out.append({"type": "function", "function": {
                "name": t.name, "description": desc, "parameters": schema}})
    return out


def run_tool(name: str, args: dict) -> tuple[Any, list[int]]:
    """Execute one tool call, and report which events it created.

    Never raises: a model that sent bad arguments should be told so and allowed
    to try again, not crash the conversation. Returns (result, event_ids) —
    the ids are what makes a turn undoable, because a small model asked a
    read-only question will still sometimes reach for a destructive tool.
    """
    tool = server.mcp._tool_manager.get_tool(name)
    if tool is None:
        return {"error": f"no such tool {name!r}"}, []
    before = server.max_event_id() if name in WRITING_TOOLS else 0
    try:
        result = tool.fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}, []
    except Exception as e:
        return {"error": f"{name} failed: {type(e).__name__}: {e}"}, []
    if name not in WRITING_TOOLS:
        return result, []
    after = server.max_event_id()
    return result, list(range(before + 1, after + 1))


def _post(url: str, payload: dict, headers: dict, timeout: int = HTTP_TIMEOUT) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"content-type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise ChatError(f"{e.code} from the provider: {detail}") from e
    except urllib.error.URLError as e:
        raise ChatError(f"could not reach the provider: {e.reason}") from e
    except TimeoutError as e:
        raise ChatError(f"the provider did not answer within {timeout}s") from e


def _timeout(cfg: dict) -> int:
    return HTTP_TIMEOUT if cfg["leaves_machine"] else LOCAL_HTTP_TIMEOUT


class ChatError(Exception):
    """Something went wrong talking to a provider. Shown to the user as-is, so
    the message has to be intelligible to someone who is not a programmer."""


def _openai_round(cfg, model, key, messages, tools, base_url):
    payload = {"model": model, "messages": messages}
    if tools:
        payload["tools"] = tools
    headers = {"authorization": f"Bearer {key}"} if key else {}
    data = _post(f"{base_url}/chat/completions", payload, headers, _timeout(cfg))
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
                 {"x-api-key": key or "", "anthropic-version": "2023-06-01"}, _timeout(cfg))
    if "content" not in data:
        raise ChatError(f"unexpected reply from the provider: {json.dumps(data)[:300]}")
    text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    calls = [{"id": b["id"], "name": b["name"], "args": b.get("input") or {}}
             for b in data["content"] if b.get("type") == "tool_use"]
    return text, calls, {"role": "assistant", "content": data["content"]}


def chat(messages: list[dict], provider: str, model: str | None = None,
         api_key: str | None = None, base_url: str | None = None,
         use_tools: bool = True, allow_writes: bool = True) -> dict:
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
    if not model:
        raise ChatError(f"Choose a model for {cfg['label']} in Settings.")
    base_url = (base_url or cfg["base_url"]).rstrip("/")
    # With writes off, the writing tools are not offered at all rather than
    # offered and refused: a model cannot misuse a tool it was never given,
    # and it stops narrating attempts it is not allowed to make.
    tools = tool_schemas(cfg["wire"], writes=allow_writes) if use_tools else []
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
                    "hit_tool_limit": bool(last and calls),
                    # Everything this turn wrote, so the UI can offer one Undo.
                    "undo": [i for c in performed for i in c["event_ids"]]}

        convo.append(raw)
        results = []
        for c in calls:
            result, event_ids = run_tool(c["name"], c["args"])
            performed.append({"name": c["name"], "args": c["args"],
                              "wrote": bool(event_ids), "event_ids": event_ids})
            results.append((c, result))

        if cfg["wire"] == "anthropic":
            convo.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": c["id"],
                 "content": json.dumps(r, default=str)} for c, r in results]})
        else:
            convo.extend({"role": "tool", "tool_call_id": c["id"],
                          "content": json.dumps(r, default=str)} for c, r in results)

    raise ChatError("the model kept calling tools without answering")  # unreachable


# --- Emptying the inbox -----------------------------------------------------

READ_CAPTURES_PROMPT = """Work the capture inbox.

Each capture below is something bought that is not yet in the kitchen. For each:

1. Work out the items. A note like "2 milk" is two litres of milk. A barcode is
   a product number — if you cannot identify it confidently, skip it and say so.
   A photographed receipt: read the line items, ignoring totals, discounts,
   loyalty points and the shop's address.
2. Call add_items once per capture with source matching its kind ('photo' for a
   receipt photo, 'barcode' for a barcode, 'manual' for a typed note) and
   source_ref set to "capture:<id>". A photographed receipt carries its own
   date — use it, in YYYY-MM-DD. Notes and barcodes may default to today.
3. Call resolve_capture(capture_id, status='done'). If you could not read it,
   use status='skipped' with a note saying why. Never guess at a blurry photo:
   an honest skip is worth more than an invented row.

Call get_stock() first and reuse existing item names exactly, so the same thing
does not arrive under two names.

Finish with one short sentence per capture saying what you recorded or skipped."""

# What the providers actually accept. HEIC and AVIF are stored faithfully when
# that is what arrived, but no model reads them, so they are reported rather
# than sent and silently failing.
READABLE_IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png", "webp": "image/webp"}


def _image_block(path: str, wire: str) -> dict | None:
    """A photographed receipt is only readable by a model that can see it."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size > MAX_IMAGE_BYTES:
        return None
    mime = READABLE_IMAGE_TYPES.get(p.suffix.lower().lstrip("."))
    if not mime:
        return None
    data = base64.b64encode(p.read_bytes()).decode()
    if wire == "anthropic":
        return {"type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data}}
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


def read_captures(provider: str, model: str | None = None, api_key: str | None = None,
                  base_url: str | None = None) -> dict:
    """Turn everything in the capture inbox into stock.

    The four doors were built and then nothing emptied what they filled: the
    design said "an agent works the queue" and no agent existed. The app has a
    model configured, so it does the job itself.
    """
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise ChatError(f"unknown provider {provider!r}")

    pending = server.mcp._tool_manager.get_tool("list_captures").fn("pending")
    if not pending:
        return {"read": 0, "reply": "Nothing waiting.", "tool_calls": [], "undo": []}

    parts: list[dict] = []
    lines: list[str] = []
    unreadable: list[str] = []
    for c in pending:
        if c["kind"] == "receipt_photo":
            block = _image_block(c.get("path") or "", cfg["wire"])
            if block is None:
                unreadable.append(
                    f"capture {c['id']}: the image is missing, too large, or in a format "
                    "models cannot read (HEIC and AVIF are not accepted — "
                    "re-take it as JPEG)")
                continue
            lines.append(f"- capture {c['id']}: a photographed receipt (image below)")
            parts.append(block)
        else:
            lines.append(f"- capture {c['id']}: {c['kind']} — {c.get('text')!r}")

    if not lines:
        return {"read": 0, "reply": "; ".join(unreadable) or "Nothing readable.",
                "tool_calls": [], "undo": []}

    content = [{"type": "text", "text": READ_CAPTURES_PROMPT + "\n\n" + "\n".join(lines)}, *parts]
    out = chat([{"role": "user", "content": content}], provider=provider, model=model,
               api_key=api_key, base_url=base_url)

    # Verify rather than believe. Observed with a local vision model: it
    # narrated a full receipt for two photographs, invented the same fruit for
    # both, had every add_items rejected, and then marked all four captures
    # done anyway. A capture marked read that produced nothing is worse than
    # one left waiting — the photo is gone from the inbox and never became
    # stock. So anything that wrote nothing goes back in the queue.
    ids = [c["id"] for c in pending]
    recorded, reopened = _verify_captures(ids)
    out["read"] = len(recorded)
    out["reopened"] = reopened
    notes = []
    if recorded:
        notes.append(f"Recorded {len(recorded)} of {len(ids)} captures.")
    if reopened:
        notes.append(f"{len(reopened)} produced nothing and are back in the inbox "
                     "— the model described items it did not manage to record.")
    notes.extend(unreadable)
    if notes:
        out["reply"] = (out["reply"].strip() + "\n\n" + "\n".join(notes)).strip()
    return out


def _verify_captures(ids: list[int]) -> tuple[list[int], list[int]]:
    """Which captures actually produced events, and which were falsely closed."""
    resolve = server.mcp._tool_manager.get_tool("resolve_capture").fn
    recorded, reopened = [], []
    with server.get_db() as conn:
        for cid in ids:
            live = conn.execute(
                "SELECT COUNT(*) FROM events WHERE source_ref = ? AND voided = 0",
                (f"capture:{cid}",)).fetchone()[0]
            if live:
                recorded.append(cid)
                continue
            row = conn.execute("SELECT status FROM captures WHERE id = ?", (cid,)).fetchone()
            if row and row["status"] == "done":
                reopened.append(cid)
    for cid in reopened:
        resolve(cid, status="pending",
                note="Reopened: marked read but nothing was recorded.")
    return recorded, reopened

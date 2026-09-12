"""Chat layer tests. No network: every provider is exercised against a fake
transport, because the thing worth testing is the tool loop, not urllib."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from homestock import chat, server

_fn = lambda t: t.fn if hasattr(t, "fn") else t
add_items = _fn(server.add_items)

today = datetime.now(UTC).date()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "chat.db"
    monkeypatch.setattr(server, "DB_PATH", db)
    server.init_db(db)
    return db


def buy(name, days_ago, ref, unit="unit"):
    add_items(items=[{"name": name, "quantity": 1, "unit": unit, "line_no": 0}],
              source="email", source_ref=ref,
              purchased_at=(today - timedelta(days=days_ago)).isoformat())


def test_the_tool_surface_is_the_mcp_surface_not_a_second_list():
    """A tool added to server.py must reach chat without being registered twice."""
    mcp_names = {t.name for t in server.mcp._tool_manager.list_tools()}
    for wire, key in [("openai", lambda s: s["function"]["name"]), ("anthropic", lambda s: s["name"])]:
        assert {key(s) for s in chat.tool_schemas(wire)} == mcp_names, wire
    assert len(mcp_names) == 19


def test_every_tool_carries_a_description_and_a_schema():
    """The docstring IS the API documentation the model reads."""
    for s in chat.tool_schemas("anthropic"):
        assert s["description"].strip(), s["name"]
        assert s["input_schema"].get("type") == "object", s["name"]


def test_run_tool_reports_bad_arguments_rather_than_raising():
    assert "error" in chat.run_tool("get_stock", {"nonsense": 1})
    assert "no such tool" in chat.run_tool("not_a_tool", {})["error"]


def test_run_tool_actually_reaches_the_event_log():
    buy("milk", 5, "m1", unit="l")
    assert chat.run_tool("get_stock", {"item": "milk"})["name"] == "milk"


class FakeProvider:
    """Replays a scripted sequence of provider replies and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def __call__(self, url, payload, headers):
        self.sent.append({"url": url, "payload": payload, "headers": headers})
        return self.script.pop(0)


def openai_reply(text=None, calls=()):
    return {"choices": [{"message": {
        "content": text,
        "tool_calls": [{"id": f"c{i}", "function": {"name": n, "arguments": json.dumps(a)}}
                       for i, (n, a) in enumerate(calls)] or None}}]}


def anthropic_reply(text=None, calls=()):
    blocks = ([{"type": "text", "text": text}] if text else [])
    blocks += [{"type": "tool_use", "id": f"c{i}", "name": n, "input": a}
               for i, (n, a) in enumerate(calls)]
    return {"content": blocks}


def test_openai_shaped_providers_run_the_tool_loop(monkeypatch):
    buy("milk", 5, "m1", unit="l")
    fake = FakeProvider([openai_reply(calls=[("get_stock", {"item": "milk"})]),
                         openai_reply(text="You probably still have milk.")])
    monkeypatch.setattr(chat, "_post", fake)

    out = chat.chat([{"role": "user", "content": "do I need milk?"}],
                    provider="openrouter", api_key="k")
    assert out["reply"] == "You probably still have milk."
    assert [c["name"] for c in out["tool_calls"]] == ["get_stock"]
    assert out["rounds"] == 2
    # the tool result was actually fed back
    assert any(m.get("role") == "tool" for m in fake.sent[1]["payload"]["messages"])


def test_anthropic_shaped_provider_runs_the_same_loop(monkeypatch):
    buy("milk", 5, "m1", unit="l")
    fake = FakeProvider([anthropic_reply(calls=[("get_stock", {"item": "milk"})]),
                         anthropic_reply(text="Probably fine.")])
    monkeypatch.setattr(chat, "_post", fake)

    out = chat.chat([{"role": "user", "content": "milk?"}], provider="anthropic", api_key="k")
    assert out["reply"] == "Probably fine."
    # system goes in its own field, not the message list
    assert "system" in fake.sent[0]["payload"]
    assert all(m["role"] != "system" for m in fake.sent[0]["payload"]["messages"])


def test_a_writing_tool_is_reported_as_having_written(monkeypatch):
    buy("milk", 5, "m1", unit="l")
    fake = FakeProvider([openai_reply(calls=[("correct_stock", {"item": "milk", "quantity": 0})]),
                         openai_reply(text="Noted, milk is on the list.")])
    monkeypatch.setattr(chat, "_post", fake)

    out = chat.chat([{"role": "user", "content": "we're out of milk"}],
                    provider="openai", api_key="k")
    assert out["tool_calls"][0] == {"name": "correct_stock",
                                    "args": {"item": "milk", "quantity": 0}, "wrote": True}
    # and it really happened, rather than merely being claimed
    assert chat.run_tool("get_stock", {"item": "milk"})["estimated_state"] == "likely_out"


def test_the_tool_loop_is_bounded(monkeypatch):
    """An unbounded loop against a metered API spends the user's money."""
    buy("milk", 5, "m1", unit="l")
    fake = FakeProvider([openai_reply(calls=[("get_stock", {})])] * 20)
    monkeypatch.setattr(chat, "_post", fake)

    out = chat.chat([{"role": "user", "content": "loop"}], provider="openai", api_key="k")
    assert out["rounds"] == chat.MAX_TOOL_ROUNDS + 1
    assert out["hit_tool_limit"] is True
    # the final round is asked without tools, so the model has to answer
    assert "tools" not in fake.sent[-1]["payload"]
    assert len(fake.sent) == chat.MAX_TOOL_ROUNDS + 1


def test_a_cloud_provider_without_a_key_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(chat, "_post", FakeProvider([]))
    with pytest.raises(chat.ChatError, match="needs an API key"):
        chat.chat([{"role": "user", "content": "hi"}], provider="openrouter")


def test_ollama_needs_no_key_and_is_declared_local():
    local = [p for p in chat.available_providers() if not p["leaves_machine"]]
    assert [p["id"] for p in local] == ["ollama"]
    assert all(p["needs_key"] for p in chat.available_providers() if p["leaves_machine"])


def test_every_provider_declares_whether_data_leaves_the_machine():
    """The settings UI promises the user this, so it cannot be optional."""
    for p in chat.available_providers():
        assert isinstance(p["leaves_machine"], bool)
        assert p["note"].strip() and p["label"].strip()


# --- The contract between the Python engine and the Mac app -----------------

def test_the_handshake_tells_the_app_where_the_backend_landed(tmp_path):
    """The Mac app spawns this process on port 0 and has no other way to learn
    the port or the write token."""
    import json as _json
    import os
    import signal
    import subprocess
    import sys as _sys
    env = {**os.environ, "HOMESTOCK_DB": str(tmp_path / "hs.db")}
    proc = subprocess.Popen(
        [_sys.executable, "-m", "homestock.ui_main", "--port", "0", "--handshake"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
    try:
        line = proc.stdout.readline()
        hs = _json.loads(line)
        assert hs["homestock"] == "ready"
        assert isinstance(hs["port"], int) and hs["port"] > 0
        assert len(hs["token"]) > 20
        import urllib.request
        body = urllib.request.urlopen(f"http://127.0.0.1:{hs['port']}/api/state", timeout=5).read()
        assert "health" in _json.loads(body)
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


# --- Model discovery: Settings offers a list rather than a blind text box ----

def test_a_blank_model_is_refused_with_advice_not_a_provider_error():
    """Ollama has no sane default model — it depends what the user pulled — so
    an unset model must say what to do rather than 400 from the provider."""
    with pytest.raises(chat.ChatError, match="Choose a model"):
        chat.chat([{"role": "user", "content": "hi"}], provider="ollama")


def test_list_models_unwraps_both_shapes_providers_use(monkeypatch):
    monkeypatch.setattr(chat, "_get", lambda url, headers: {
        "data": [{"id": "gpt-5"}, {"id": "gpt-4.1"}]})
    assert chat.list_models("openai", api_key="k") == ["gpt-4.1", "gpt-5"]

    monkeypatch.setattr(chat, "_get", lambda url, headers: {
        "models": [{"name": "llama3.2:3b"}]})
    assert chat.list_models("ollama") == ["llama3.2:3b"]


def test_listing_models_for_a_keyless_cloud_provider_asks_for_nothing(monkeypatch):
    """No key means no request at all — not a 401 the user has to interpret."""
    def explode(*a, **k):
        raise AssertionError("should not have called the provider")
    monkeypatch.setattr(chat, "_get", explode)
    assert chat.list_models("openrouter") == []
    assert chat.list_models("anthropic") == []


def test_local_provider_needs_no_key_to_list_models(monkeypatch):
    called = []
    monkeypatch.setattr(chat, "_get", lambda url, headers: called.append(headers) or {"models": []})
    chat.list_models("ollama")
    assert called and "authorization" not in called[0]

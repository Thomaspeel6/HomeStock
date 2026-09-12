"""Pantry UI tests — the window a person looks at, over a real HTTP server."""

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from homestock import server, ui

_fn = lambda t: t.fn if hasattr(t, "fn") else t
add_items = _fn(server.add_items)
set_shelf_life = _fn(server.set_shelf_life)
correct_stock = _fn(server.correct_stock)
get_events_page = _fn(server.get_events)


def get_events(*args, **kwargs):
    """The rows only. get_events() now paginates and returns an envelope;
    every assertion here is about the rows, so unwrap once rather than at
    forty call sites. get_events_page exercises the envelope itself."""
    page = get_events_page(*args, **kwargs)
    assert "error" not in page, page
    return page["events"]

# The server dates events in UTC (server._today()). Deriving the expected
# dates from the local clock made the suite fail for any contributor behind
# UTC, and for the author between midnight and 01:00 BST.
today = datetime.now(UTC).date()
# The proxy in some environments would swallow loopback requests.
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def d(days_ago: int) -> str:
    return (today - timedelta(days=days_ago)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "ui.db"
    monkeypatch.setattr(server, "DB_PATH", db)
    server.init_db(db)
    return db


@pytest.fixture
def http():
    """A live UI server on an ephemeral port."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def post(base, payload, token=ui.TOKEN):
    req = urllib.request.Request(
        f"{base}/api/action", data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-homestock-token": token},
    )
    return _open(req)


def buy(name, days_ago, ref, unit="unit", qty=1):
    add_items(items=[{"name": name, "quantity": qty, "unit": unit, "line_no": 0}],
              source="email", source_ref=ref, purchased_at=d(days_ago))


def test_empty_kitchen_renders_without_history(http):
    state = json.loads(_open(f"{http}/api/state").read())
    assert state["order"] == [] and state["expiring"] == [] and state["shelf"] == []
    assert state["health"]["stale"] is True
    assert b"Your kitchen" in _open(f"{http}/").read()


def test_state_sorts_items_into_exactly_one_list():
    for i, days in enumerate([31, 26, 21, 16, 11, 6]):
        buy("semi-skimmed milk", days, f"m{i}", unit="l", qty=2)  # due to rebuy
    for i, days in enumerate([30, 20, 10, 1]):
        buy("bananas", days, f"b{i}", unit="kg")  # fresh
    buy("whole chicken", 1, "c1", unit="kg", qty=1.4)
    set_shelf_life("whole chicken", 3, storage="fridge")
    set_shelf_life("semi-skimmed milk", 7)

    s = ui.build_state()
    names = lambda k: [x["name"] for x in s[k]]
    assert names("order") == ["semi-skimmed milk"]
    assert names("expiring") == ["whole chicken"]
    assert names("shelf") == ["bananas"]
    # milk is due to rebuy AND nominally expiring; it must not appear twice
    everywhere = names("order") + names("expiring") + names("shelf")
    assert len(everywhere) == len(set(everywhere))


def test_binning_removes_it_from_use_soon(http):
    buy("lettuce", 1, "l1")
    set_shelf_life("lettuce", 3)
    assert [x["name"] for x in ui.build_state()["expiring"]] == ["lettuce"]
    post(http, {"item": "lettuce", "action": "binned"})
    assert ui.build_state()["expiring"] == []


def test_long_expired_items_stop_nagging():
    buy("whole chicken", 13, "c1", unit="kg", qty=1.4)
    set_shelf_life("whole chicken", 3)
    # 10 days past a 3-day use-by: eaten or binned long ago, not "use soon"
    assert ui.build_state()["expiring"] == []
    assert "whole chicken" in [x["name"] for x in ui.build_state()["shelf"]]


def test_write_requires_the_launch_token(http):
    buy("milk", 5, "m1")
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http, {"item": "milk", "action": "out"}, token="guessed")
    assert e.value.code == 403
    assert get_events("milk")[-1]["type"] == "bought"  # nothing was written


def test_actions_write_the_right_event(http):
    buy("milk", 5, "m1")
    buy("lettuce", 1, "l1")

    post(http, {"item": "milk", "action": "out"})
    assert ui.get_stock("milk")["corrected_quantity"] == 0

    post(http, {"item": "milk", "action": "have"})
    assert ui.get_stock("milk")["corrected_quantity"] == 1

    post(http, {"item": "lettuce", "action": "binned"})
    # binning records the waste AND that there is none left, so the row leaves
    # "Use soon" instead of nagging about food already in the bin
    assert [e["type"] for e in get_events("lettuce")] == ["bought", "discarded", "corrected"]
    assert ui.get_stock("lettuce")["corrected_quantity"] == 0


def test_bad_requests_are_rejected_cleanly(http):
    for payload, code in [({"item": "ghost", "action": "out"}, 400),
                          ({"item": "x", "action": "detonate"}, 400),
                          ({"nope": 1}, 400)]:
        with pytest.raises(urllib.error.HTTPError) as e:
            post(http, payload)
        assert e.value.code == code
    with pytest.raises(urllib.error.HTTPError) as e:
        _open(f"{http}/etc/passwd")
    assert e.value.code == 404


def test_item_names_are_escaped_into_the_page(http):
    add_items(items=[{"name": "<script>alert(1)</script> tea", "quantity": 1,
                      "unit": "pack", "line_no": 0}],
              source="manual", source_ref="x1")
    body = json.loads(_open(f"{http}/api/state").read())
    assert body["shelf"][0]["name"] == "<script>alert(1)</script> tea"
    assert "<script>alert(1)</script>" not in _open(f"{http}/").read().decode()


# --- capture: phone and laptop ----------------------------------------------

# A real 1x1 PNG, so the decoder is exercised rather than mocked.
PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
       "DUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def loopback(monkeypatch):
    """Every test starts on the safe default; LAN tests opt in explicitly."""
    monkeypatch.setattr(ui, "LAN_MODE", False)
    monkeypatch.setattr(ui, "_pair", {"code": None, "expires": 0.0, "attempts": 0})


def get(base, path, cookie=None, host=None):
    req = urllib.request.Request(f"{base}{path}")
    if cookie:
        req.add_header("Cookie", cookie)
    if host:
        req.add_header("Host", host)  # what a rebound DNS name looks like on the wire
    return _open(req)


def capture(base, payload, token=ui.TOKEN, cookie=None):
    req = urllib.request.Request(
        f"{base}/api/capture", data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "x-homestock-token": token},
    )
    if cookie:
        req.add_header("Cookie", cookie)
    return _open(req)


def pair(base, code):
    req = urllib.request.Request(
        f"{base}/api/pair", data=json.dumps({"code": code}).encode(),
        headers={"content-type": "application/json"})
    return _open(req)


def test_photo_capture_writes_a_file_and_queues_it(http, fresh_db):
    r = json.loads(capture(http, {"kind": "receipt_photo", "data_url": PNG,
                                  "device": "phone"}).read())
    assert r["kind"] == "receipt_photo" and r["status"] == "pending"

    [row] = _fn(server.list_captures)("pending")
    assert row["device"] == "phone" and row["mime"] == "image/png"
    stored = Path(row["path"])
    assert stored.exists() and stored.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    # photos live beside the database, not inside it
    assert stored.parent == fresh_db.parent / "captures"


def test_barcode_and_note_capture_from_either_device(http):
    capture(http, {"kind": "barcode", "text": "5000119003478", "device": "phone"})
    capture(http, {"kind": "note", "text": "2 milk, bread, 6 eggs", "device": "laptop"})
    rows = _fn(server.list_captures)("pending")
    assert [(r["kind"], r["device"]) for r in rows] == [
        ("barcode", "phone"), ("note", "laptop")]
    assert json.loads(get(http, "/api/captures").read())[0]["text"] == "5000119003478"


def test_junk_uploads_are_refused(http):
    for payload in [{"kind": "receipt_photo", "data_url": "data:image/png;base64,!!!"},
                    {"kind": "receipt_photo", "data_url": "data:text/html;base64,PHNjcmlwdD4="},
                    {"kind": "receipt_photo", "data_url": "not-a-data-url"},
                    {"kind": "note", "text": "   "},
                    {"kind": "telepathy", "text": "milk"}]:
        with pytest.raises(urllib.error.HTTPError) as e:
            capture(http, payload)
        assert e.value.code == 400
    assert _fn(server.list_captures)("pending") == []


def test_capture_still_needs_the_token_on_loopback(http):
    with pytest.raises(urllib.error.HTTPError) as e:
        capture(http, {"kind": "note", "text": "milk"}, token="guessed")
    assert e.value.code == 403
    assert _fn(server.list_captures)("pending") == []


# --- LAN mode: nothing is readable until a device pairs ----------------------

@pytest.fixture
def from_the_network(monkeypatch):
    """LAN mode, as seen by a phone rather than by this laptop.

    Every test here dials 127.0.0.1, but loopback is deliberately exempt from
    pairing — the laptop has to be able to read its own pairing code. Pretending
    the peer is elsewhere is what actually exercises the gate."""
    monkeypatch.setattr(ui, "LAN_MODE", True)
    monkeypatch.setattr(ui.Handler, "_loopback", lambda self: False)


def test_lan_mode_hides_everything_until_paired(http, from_the_network):
    buy("milk", 5, "m1")

    # the pantry itself must not leak to an unpaired device on the network
    for path in ("/api/state", "/api/captures"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(http, path)
        assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        capture(http, {"kind": "note", "text": "milk"}, token="guessed")
    assert e.value.code == 403

    # and the pages offer pairing rather than content — including the write
    # token, which an unpaired device must not be handed
    for path in ("/", "/capture"):
        body = get(http, path).read()
        assert b"Pairing code" in body
        assert ui.TOKEN.encode() not in body


def test_the_laptop_is_never_locked_out_of_its_own_pairing_code(http, monkeypatch):
    """LAN mode gates the network, not this machine. If loopback needed the
    code too, the page that displays the code would be behind the code."""
    monkeypatch.setattr(ui, "LAN_MODE", True)
    ui.new_pair_code()
    assert b"Your kitchen" in get(http, "/").read()
    assert json.loads(get(http, "/api/pairing").read())["code"] == ui._pair["code"]


def test_pairing_grants_access_and_a_wrong_code_does_not(http, from_the_network):
    buy("milk", 5, "m1")
    code = ui.new_pair_code()

    with pytest.raises(urllib.error.HTTPError) as e:
        pair(http, "000000" if code != "000000" else "111111")
    assert e.value.code == 403

    resp = pair(http, code)
    cookie = resp.headers["Set-Cookie"]
    assert "SameSite=Strict" in cookie and "HttpOnly" in cookie
    # The cookie says which device, never which page. If it carried the write
    # token, anything that leaked a "read-only" cookie would leak write access.
    assert ui.DEVICE_SECRET in cookie and ui.TOKEN not in cookie

    jar = cookie.split(";")[0]
    assert json.loads(get(http, "/api/state", cookie=jar).read())["health"]["items"] == 1
    r = json.loads(capture(http, {"kind": "note", "text": "eggs", "device": "phone"},
                           cookie=jar).read())
    assert r["status"] == "pending"


def test_the_cookie_value_is_not_accepted_as_the_write_header(http, from_the_network):
    """The two secrets have to actually differ, not merely be named differently."""
    buy("milk", 5, "m1")
    jar = pair(http, ui.new_pair_code()).headers["Set-Cookie"].split(";")[0]
    with pytest.raises(urllib.error.HTTPError) as e:
        post(http, {"item": "milk", "action": "out"}, token=jar.split("=", 1)[1])
    assert e.value.code == 403
    assert [ev["type"] for ev in get_events("milk")] == ["bought"]


@pytest.mark.parametrize("host", ["evil.test", "attacker.example.com"])
def test_a_request_under_a_foreign_host_name_is_refused(http, host):
    """DNS rebinding: a public page whose name resolves to 127.0.0.1 is
    same-origin with us, so the custom-header rule alone does not stop it. It
    cannot forge the Host header, so that is what we check."""
    buy("milk", 5, "m1")
    for path in ("/api/state", "/"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(http, path, host=host)
        assert e.value.code == 403


def test_non_ascii_credentials_are_refused_rather_than_crashing(http, from_the_network):
    """secrets.compare_digest raises TypeError on non-ASCII str, and these
    values arrive straight off the wire."""
    ui.new_pair_code()
    with pytest.raises(urllib.error.HTTPError) as e:
        pair(http, "café12")
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        get(http, "/api/state", cookie="hs=café")
    assert e.value.code == 403


def test_a_guessed_code_burns_after_five_attempts(http, from_the_network):
    code = ui.new_pair_code()
    wrong = "999999" if code != "999999" else "888888"
    for _ in range(ui.PAIR_MAX_ATTEMPTS - 1):
        with pytest.raises(urllib.error.HTTPError):
            pair(http, wrong)
    assert ui._pair["code"] == code  # not burned yet — the boundary is exact
    with pytest.raises(urllib.error.HTTPError):
        pair(http, wrong)
    # the real code is now dead too — the laptop must issue a fresh one
    with pytest.raises(urllib.error.HTTPError) as e:
        pair(http, code)
    assert e.value.code == 403
    assert ui.check_pair_code(ui.new_pair_code()) is True


def test_expired_codes_are_refused(http, from_the_network):
    code = ui.new_pair_code()
    ui._pair["expires"] = 0.0
    assert ui.check_pair_code(code) is False


def test_a_paired_cookie_alone_cannot_write(http, monkeypatch):
    """A cookie rides along with a cross-site request; a custom header does
    not. So the cookie may unlock reading, and must never unlock writing."""
    monkeypatch.setattr(ui, "LAN_MODE", True)
    jar = pair(http, ui.new_pair_code()).headers["Set-Cookie"].split(";")[0]

    # reading is fine with the cookie alone
    assert json.loads(get(http, "/api/state", cookie=jar).read())["order"] == []

    # writing is not
    req = urllib.request.Request(
        f"{http}/api/capture", data=json.dumps({"kind": "note", "text": "x"}).encode(),
        headers={"content-type": "application/json", "Cookie": jar})
    with pytest.raises(urllib.error.HTTPError) as e:
        _open(req)
    assert e.value.code == 403
    assert _fn(server.list_captures)("pending") == []

    # the page's own script, which has the header, still works
    assert json.loads(capture(http, {"kind": "note", "text": "x"}, cookie=jar).read())[
        "status"] == "pending"


def test_pairing_endpoint_is_absent_on_loopback(http):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(http, "/api/pairing")
    assert e.value.code == 404

"""HomeStock server tests. Run: uv run pytest"""

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homestock import server


def _fn(tool):
    return tool.fn if hasattr(tool, "fn") else tool


add_items = _fn(server.add_items)
get_stock = _fn(server.get_stock)
what_should_i_order = _fn(server.what_should_i_order)
void_event = _fn(server.void_event)
record_ingest_run = _fn(server.record_ingest_run)
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


def d(days_ago: int) -> str:
    return (today - timedelta(days=days_ago)).isoformat()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr(server, "DB_PATH", db)
    server.init_db(db)
    return db


def seed_receipt(name, days_ago, ref, unit="unit", qty=1):
    return add_items(
        items=[{"name": name, "quantity": qty, "unit": unit, "line_no": 0}],
        source="email", source_ref=ref, purchased_at=d(days_ago),
    )


def test_insert_validation_and_rejects():
    r = add_items(
        items=[
            {"name": "  Semi-Skimmed   MILK ", "quantity": 2, "unit": "l", "price": 2.5, "line_no": 0},
            {"name": "whole chicken", "quantity": 1.2, "unit": "kg", "line_no": 1},
            {"name": "bad line", "quantity": -1, "unit": "kg", "line_no": 2},
            {"name": "bad unit", "quantity": 1, "unit": "litres", "line_no": 3},
        ],
        source="email", source_ref="order-1001", purchased_at=d(30),
    )
    assert r["inserted"] == 2
    assert [x["line_no"] for x in r["rejected"]] == [2, 3]
    # canonicalisation
    assert [i["name"] for i in get_stock()] == ["semi-skimmed milk", "whole chicken"]


def test_idempotency():
    seed_receipt("milk", 5, "order-1")
    r = seed_receipt("milk", 5, "order-1")
    assert r == {"inserted": 0, "ignored": 1, "rejected": []}


def test_email_requires_purchased_at():
    r = add_items(items=[{"name": "eggs", "quantity": 1, "unit": "pack", "line_no": 0}],
                  source="email", source_ref="order-2")
    assert "purchased_at is required" in r["rejected"][0]["reason"]


def test_future_date_rejected():
    r = add_items(items=[{"name": "eggs", "quantity": 1, "unit": "pack", "line_no": 0}],
                  source="email", source_ref="order-3",
                  purchased_at=(today + timedelta(days=3)).isoformat())
    assert "future" in r["rejected"][0]["reason"]


def test_stock_states_and_order_list():
    for i, days_ago in enumerate([31, 26, 21, 16, 11, 6]):  # milk every 5 days, last 6 days ago
        seed_receipt("semi-skimmed milk", days_ago, f"order-m{i}", unit="l", qty=2)
    s = get_stock("Semi-Skimmed Milk")
    assert s["purchases_observed"] == 6
    assert s["median_interval_days"] == 5
    assert s["estimated_state"] == "likely_out"

    seed_receipt("whole chicken", 10, "order-c1", unit="kg", qty=1.2)  # 1 purchase -> unknown
    assert get_stock("whole chicken")["estimated_state"] == "unknown"

    for i, days_ago in enumerate([120, 115, 110, 105]):  # discontinued: past 3x median
        seed_receipt("oat bars", days_ago, f"order-o{i}", unit="pack")

    order = what_should_i_order()
    assert [o["name"] for o in order] == ["semi-skimmed milk"]
    assert order[0]["overshoot"] == 1.2


def test_void_then_reinsert():
    seed_receipt("whole chicken", 30, "order-1001", unit="kg", qty=1.2)
    assert void_event("order-1001", line_no=0) == {"voided": 1}
    r = add_items(items=[{"name": "whole chicken", "quantity": 1.5, "unit": "kg", "line_no": 0}],
                  source="email", source_ref="order-1001", purchased_at=d(30))
    assert r["inserted"] == 1
    ev = get_events("whole chicken")
    assert len(ev) == 2 and ev[0]["voided"] == 1 and ev[1]["quantity"] == 1.5


def test_void_no_match_is_noop():
    assert void_event("no-such-ref") == {"voided": 0}


def test_manual_defaults_to_today():
    r = add_items(items=[{"name": "dish soap", "quantity": 1, "unit": "unit", "line_no": 0}],
                  source="manual", source_ref="manual:lidl:2026-08-10:4.50")
    assert r["inserted"] == 1
    assert get_events("dish soap")[0]["occurred_at"] == today.isoformat()


def test_ingest_runs_cursor():
    r = record_ingest_run(window_start=d(30), window_end=d(0), emails_seen=12, events_written=9)
    assert r["earliest_window_start"] == d(30)
    r = record_ingest_run(window_start=d(60), window_end=d(30), emails_seen=8, events_written=7,
                          skipped=[{"source_ref": "order-x", "reason": "no line items"}])
    assert r["earliest_window_start"] == d(60)


# --- Migrations (CRITICAL, regression rule) --------------------------------

def test_migration_fresh_db_reaches_latest(fresh_db):
    conn = server._connect(fresh_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(server.MIGRATIONS)
    conn.close()


def test_migration_is_noop_when_current(fresh_db):
    seed_receipt("milk", 5, "order-1")
    server.init_db(fresh_db)  # second run must not raise or duplicate
    assert len(get_events("milk")) == 1


def test_migration_upgrades_old_db_preserving_data(fresh_db, monkeypatch):
    seed_receipt("milk", 5, "order-1")
    # simulate a future release adding a migration
    monkeypatch.setattr(server, "MIGRATIONS",
                        [*server.MIGRATIONS, "ALTER TABLE items ADD COLUMN emoji TEXT;"])
    server.init_db(fresh_db)
    conn = server._connect(fresh_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(server.MIGRATIONS)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
    assert "emoji" in cols
    conn.close()
    assert len(get_events("milk")) == 1  # data intact


# --- Concurrency (two processes, one WAL DB) --------------------------------

def test_two_process_concurrent_writes(fresh_db):
    """A second process hammers writes while this process writes too.
    busy_timeout must make writers queue, not raise SQLITE_BUSY."""
    script = f"""
import sqlite3
conn = sqlite3.connect({str(fresh_db)!r}, timeout=5)
conn.execute("PRAGMA busy_timeout = 5000")
for i in range(50):
    conn.execute("INSERT INTO items (name) VALUES (?)", (f"proc2-item-{{i}}",))
    conn.commit()
conn.close()
"""
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        for i in range(50):
            r = add_items(items=[{"name": f"proc1 item {i}", "quantity": 1, "unit": "unit", "line_no": 0}],
                          source="manual", source_ref=f"manual:test:{i}")
            assert r["inserted"] == 1
    finally:
        assert proc.wait(timeout=30) == 0
    conn = server._connect(fresh_db)
    n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    assert n == 100


# --- v2: ground truth, shelf life, merge, health ----------------------------

correct_stock = _fn(server.correct_stock)
discard_item = _fn(server.discard_item)
set_shelf_life = _fn(server.set_shelf_life)
get_expiring_soon = _fn(server.get_expiring_soon)
merge_items = _fn(server.merge_items)
get_health = _fn(server.get_health)


def test_v1_database_upgrades_to_latest_preserving_data(tmp_path, monkeypatch):
    """The critical path: a homestock.db written by the shipped v0.1.0 release
    must survive every later rebuild of the events table with all rows intact.

    Rows are written with raw SQL against the v1 schema on purpose — that is
    what an old release actually left on disk, and today's code cannot be used
    to produce it (it expects tables v1 never had)."""
    db = tmp_path / "v1.db"
    monkeypatch.setattr(server, "DB_PATH", db)
    conn = server._connect(db)
    conn.executescript(server.MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO items (name) VALUES ('milk')")
    conn.executemany(
        "INSERT INTO events (item_id, type, quantity, unit, price, occurred_at, "
        "source, source_ref, line_no, voided) VALUES (1,'bought',?,?,?,?,?,?,?,?)",
        [(2, "l", 1.55, d(12), "email", "order-v1", 0, 1),   # voided
         (2, "l", 1.55, d(12), "email", "order-v1", 0, 0),   # the correction
         (1, "l", None, d(5), "manual", "manual:corner:x", 0, 0)],
    )
    conn.commit()
    conn.close()

    server.init_db(db)  # upgrade v1 -> latest

    conn = server._connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(server.MIGRATIONS)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(items)")]
    assert "shelf_life_days" in cols and "storage" in cols
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aliases", "captures"} <= tables
    conn.close()

    ev = get_events("milk")
    assert len(ev) == 3 and [e["voided"] for e in ev] == [1, 0, 0]
    assert [e["price"] for e in ev][:2] == [1.55, 1.55]  # columns did not shift
    # and the rebuilt table accepts what v1 forbade
    assert correct_stock("milk", 0)["recorded"] == "corrected"
    assert get_stock("milk")["purchases_observed"] == 2


def test_correction_overrides_estimate_and_resets_clock():
    for i, days_ago in enumerate([31, 26, 21, 16, 11, 6]):  # milk every 5d, last 6d ago
        seed_receipt("milk", days_ago, f"order-m{i}", unit="l", qty=2)
    assert get_stock("milk")["estimated_state"] == "likely_out"

    correct_stock("milk", 2, unit="l")  # "we still have two litres"
    s = get_stock("milk")
    assert s["confirmed"] is True
    assert s["days_since_observation"] == 0
    assert s["estimated_state"] == "likely_in_stock"
    assert s["days_since_last_purchase"] == 6  # the receipt fact is unchanged


def test_confirmed_out_reaches_the_order_list_on_one_purchase():
    seed_receipt("paprika", 40, "order-p1")
    assert get_stock("paprika")["estimated_state"] == "unknown"  # too thin to infer
    correct_stock("paprika", 0)
    s = get_stock("paprika")
    assert s["estimated_state"] == "likely_out" and s["confidence"] == "high"
    order = what_should_i_order()
    assert [o["name"] for o in order] == ["paprika"]
    assert order[0]["reason"] == "you said you were out"


def test_correction_requires_known_item_and_valid_quantity():
    assert "unknown item" in correct_stock("unicorn steak", 1)["error"]
    seed_receipt("rice", 10, "order-r1", unit="kg")
    assert "quantity must be" in correct_stock("rice", -2)["error"]


def test_confidence_tracks_regularity():
    for i, d_ago in enumerate([35, 28, 21, 14, 7]):  # metronomic
        seed_receipt("bread", d_ago, f"order-b{i}")
    for i, d_ago in enumerate([60, 59, 30, 3]):  # erratic
        seed_receipt("wine", d_ago, f"order-w{i}")
    assert get_stock("bread")["confidence"] == "high"
    assert get_stock("wine")["confidence"] == "low"
    assert get_stock("bread")["interval_variation"] == 0.0


def test_shelf_life_and_expiring_soon():
    seed_receipt("whole chicken", 2, "order-c1", unit="kg", qty=1.4)
    seed_receipt("tinned beans", 2, "order-t1", unit="pack")
    assert get_expiring_soon() == []  # nothing has a shelf life yet

    assert set_shelf_life("whole chicken", 3, storage="fridge")["shelf_life_days"] == 3
    assert set_shelf_life("tinned beans", 900)["shelf_life_days"] == 900

    soon = get_expiring_soon(within_days=3)
    assert [x["name"] for x in soon] == ["whole chicken"]
    assert soon[0]["days_left"] == 1 and soon[0]["storage"] == "fridge"

    correct_stock("whole chicken", 0)  # eaten — it can no longer spoil
    assert get_expiring_soon(within_days=3) == []


def test_set_shelf_life_validation():
    seed_receipt("milk", 1, "order-1")
    assert "positive integer" in set_shelf_life("milk", 0)["error"]
    assert "storage must be" in set_shelf_life("milk", 7, storage="cupboard")["error"]
    assert "unknown item" in set_shelf_life("ghost", 7)["error"]


def test_merge_items_folds_history_and_inherits_metadata():
    seed_receipt("milk", 20, "order-1", unit="l", qty=2)
    seed_receipt("semi-skimmed milk", 15, "order-2", unit="l", qty=2)
    seed_receipt("semi-skimmed milk", 10, "order-3", unit="l", qty=2)
    set_shelf_life("milk", 7, storage="fridge")

    r = merge_items("Milk", "semi-skimmed milk")
    assert r == {"merged": "milk", "into": "semi-skimmed milk",
                 "events_moved": 1, "alias_learned": "milk"}
    assert [i["name"] for i in get_stock()] == ["semi-skimmed milk"]

    s = get_stock("semi-skimmed milk")
    assert s["purchases_observed"] == 3          # history folded in, none lost
    assert s["median_interval_days"] == 5
    assert s["shelf_life_days"] == 7             # survivor inherited what it lacked
    assert s["storage"] == "fridge"


def test_merge_items_rejects_bad_targets():
    seed_receipt("milk", 5, "order-1")
    assert "same name" in merge_items("milk", "MILK")["error"]
    assert "unknown item" in merge_items("nope", "milk")["error"]
    assert "merge into a name that exists" in merge_items("milk", "nope")["error"]


def test_health_flags_stale_ingestion():
    h = get_health()
    assert h["stale"] is True and h["days_since_ingest"] is None  # never ingested
    assert h["schema_version"] == len(server.MIGRATIONS)

    seed_receipt("milk", 5, "order-1")
    record_ingest_run(window_start=d(7), window_end=d(0), emails_seen=3, events_written=1)
    h = get_health()
    assert h["stale"] is False
    assert (h["items"], h["events"], h["ingest_runs"]) == (1, 1, 1)
    assert h["latest_event"] == d(5)
    # diagnostics must be safe to paste into a bug report
    assert "milk" not in json.dumps(h)


def test_discard_is_recorded_separately_from_correction():
    seed_receipt("lettuce", 4, "order-1")
    discard_item("lettuce")
    types = [e["type"] for e in get_events("lettuce")]
    assert types == ["bought", "discarded"]


# --- v3: aliases, the capture inbox, cooking ---------------------------------

add_alias = _fn(server.add_alias)
list_aliases = _fn(server.list_aliases)
add_capture = _fn(server.add_capture)
list_captures = _fn(server.list_captures)
resolve_capture = _fn(server.resolve_capture)
check_recipe = _fn(server.check_recipe)
consume_items = _fn(server.consume_items)


def test_four_input_paths_converge_on_one_item():
    """The point of aliases: a barcode, a receipt line and a person all name
    the same milk, and it stays one repurchase cycle rather than three."""
    seed_receipt("semi-skimmed milk", 20, "email-1", unit="l", qty=2)
    add_alias("TESCO SEMI SKMD MILK", "semi-skimmed milk")
    add_alias("Tesco British Semi Skimmed Milk 2.27L", "semi-skimmed milk")
    add_alias("milk", "semi-skimmed milk")

    add_items(items=[{"name": "TESCO SEMI SKMD MILK", "quantity": 2, "unit": "l", "line_no": 0}],
              source="photo", source_ref="photo-1", purchased_at=d(15))
    add_items(items=[{"name": "Tesco British Semi Skimmed Milk 2.27L", "quantity": 2,
                      "unit": "l", "line_no": 0}],
              source="barcode", source_ref="bc-1", purchased_at=d(10))
    add_items(items=[{"name": "milk", "quantity": 2, "unit": "l", "line_no": 0}],
              source="loyalty", source_ref="clubcard-1", purchased_at=d(5))

    assert [i["name"] for i in get_stock()] == ["semi-skimmed milk"]
    s = get_stock("MILK")  # and a lookup by any of those names finds it
    assert s["purchases_observed"] == 4 and s["median_interval_days"] == 5
    assert {e["source"] for e in get_events("milk")} == {"email", "photo", "barcode", "loyalty"}


def test_merge_learns_an_alias_so_drift_cannot_recur():
    seed_receipt("milk", 20, "o1", unit="l")
    seed_receipt("semi-skimmed milk", 10, "o2", unit="l")
    merge_items("milk", "semi-skimmed milk")
    assert list_aliases("semi-skimmed milk") == [
        {"alias": "milk", "item": "semi-skimmed milk", "source": "merge",
         "created_at": list_aliases()[0]["created_at"]}]
    # the agent slipping back to the old name no longer creates a duplicate
    add_items(items=[{"name": "Milk", "quantity": 1, "unit": "l", "line_no": 0}],
              source="email", source_ref="o3", purchased_at=d(2))
    assert [i["name"] for i in get_stock()] == ["semi-skimmed milk"]


def test_alias_rejects_shadowing_a_real_item():
    seed_receipt("milk", 5, "o1")
    seed_receipt("oat milk", 5, "o2")
    assert "use merge_items" in add_alias("oat milk", "milk")["error"]
    assert "not an alias of itself" in add_alias("milk", "milk")["error"]
    assert "unknown item" in add_alias("skimmed", "ghost")["error"]


def test_new_sources_are_accepted_and_bad_ones_are_not():
    for src in ("photo", "barcode", "loyalty", "manual", "email"):
        r = add_items(items=[{"name": f"thing {src}", "quantity": 1, "unit": "unit", "line_no": 0}],
                      source=src, source_ref=f"ref-{src}", purchased_at=d(1))
        assert r["inserted"] == 1, (src, r)
    r = add_items(items=[{"name": "x", "quantity": 1, "unit": "unit", "line_no": 0}],
                  source="telepathy", source_ref="t1", purchased_at=d(1))
    assert "source must be one of" in r["rejected"][0]["reason"]


def test_capture_inbox_round_trip():
    c = add_capture("receipt_photo", path="/tmp/receipt-1.jpg", device="phone", mime="image/jpeg")
    assert c["status"] == "pending"
    add_capture("barcode", text="5000119003478", device="phone")
    add_capture("note", text="2 milk, bread, 6 eggs", device="laptop")

    pending = list_captures()
    assert [x["kind"] for x in pending] == ["receipt_photo", "barcode", "note"]
    assert [x["device"] for x in pending] == ["phone", "phone", "laptop"]

    # the agent reads it, records the items, then closes it
    add_items(items=[{"name": "milk", "quantity": 2, "unit": "l", "line_no": 0}],
              source="photo", source_ref="capture-1", purchased_at=d(0))
    assert resolve_capture(c["capture_id"])["status"] == "done"
    assert [x["id"] for x in list_captures()] != [c["capture_id"]]
    assert len(list_captures("done")) == 1
    assert len(list_captures("all")) == 3


def test_unreadable_capture_is_skipped_with_a_reason_not_guessed():
    c = add_capture("receipt_photo", path="/tmp/blurry.jpg", device="phone")
    r = resolve_capture(c["capture_id"], status="skipped", note="too blurry to read")
    assert r["status"] == "skipped"
    assert list_captures("skipped")[0]["note"] == "too blurry to read"
    assert list_captures() == []  # and it stops cluttering the inbox


def test_capture_validation():
    assert "kind must be one of" in add_capture("telepathy", text="x")["error"]
    assert "either text or a path" in add_capture("note")["error"]
    assert "no capture 999" in resolve_capture(999)["error"]


def test_check_recipe_sorts_ingredients_by_what_you_have():
    for i, days in enumerate([31, 26, 21, 16, 11, 6]):
        seed_receipt("semi-skimmed milk", days, f"m{i}", unit="l", qty=2)  # due to rebuy
    for i, days in enumerate([30, 20, 10, 1]):
        seed_receipt("eggs", days, f"e{i}", unit="pack")                   # fresh
    for i, days in enumerate([40, 30, 20, 8]):
        seed_receipt("plain flour", days, f"f{i}", unit="kg")              # ~10d cycle, 8d ago
    seed_receipt("nutmeg", 200, "n1")                                      # one purchase

    r = check_recipe(["eggs", "semi-skimmed milk", "plain flour", "nutmeg", "saffron"])
    assert r["can_cook"] is False
    assert [x["ingredient"] for x in r["have"]] == ["eggs"]
    assert [x["ingredient"] for x in r["low"]] == ["plain flour"]
    assert [x["ingredient"] for x in r["unsure"]] == ["nutmeg"]
    assert [x["ingredient"] for x in r["missing"]] == ["semi-skimmed milk", "saffron"]
    assert r["missing"][1]["reason"] == "never bought"


def test_check_recipe_can_cook_and_resolves_aliases():
    for i, days in enumerate([30, 20, 10, 1]):
        seed_receipt("eggs", days, f"e{i}", unit="pack")
    add_alias("free range eggs", "eggs")
    r = check_recipe(["Free Range Eggs"])
    assert r["can_cook"] is True and r["have"][0]["item"] == "eggs"


def test_cooking_depletes_stock_and_finishing_moves_it_to_the_list():
    for i, days in enumerate([30, 20, 10, 1]):
        seed_receipt("eggs", days, f"e{i}", unit="pack")
        seed_receipt("butter", days, f"b{i}", unit="unit")
    assert get_stock("eggs")["estimated_state"] == "likely_in_stock"

    r = consume_items([{"name": "eggs", "quantity": 3, "unit": "unit"},
                       {"name": "butter", "quantity": 1, "finished": True},
                       {"name": "saffron"}])
    assert r["consumed"] == 2 and r["marked_finished"] == 1
    assert r["unknown"][0]["name"] == "saffron"

    assert [e["type"] for e in get_events("eggs")][-1] == "consumed"
    # using the last of the butter is what actually puts it on the list
    assert get_stock("butter")["estimated_state"] == "likely_out"
    assert "butter" in [o["name"] for o in what_should_i_order()]
    assert get_stock("eggs")["estimated_state"] == "likely_in_stock"  # merely used, not gone


def test_consumption_does_not_corrupt_repurchase_intervals():
    """Intervals model rebuying. Cooking must not look like a purchase."""
    for i, days in enumerate([30, 20, 10]):
        seed_receipt("eggs", days, f"e{i}", unit="pack")
    before = get_stock("eggs")["median_interval_days"]
    consume_items([{"name": "eggs", "quantity": 2}])
    after = get_stock("eggs")
    assert after["median_interval_days"] == before == 10
    assert after["purchases_observed"] == 3


# --- MCP surface: prompts and resources -------------------------------------
# An agent needs more than tools. These used to be files the client had to find
# on disk, in the right directory, which nothing in the protocol expresses.

onboarding = _fn(server.onboarding)
ingestion = _fn(server.ingestion)
recipes_index = _fn(server.recipes_index)
recipe = _fn(server.recipe)


def test_prompts_carry_the_real_procedures():
    for text, must in ((onboarding(), "Consent"), (ingestion(), "Sender filter first")):
        assert must in text
        assert "missing from this installation" not in text
    # the rule that keeps ingestion honest has to survive being served
    assert "Never guess" in ingestion()


def test_recipes_are_readable_without_filesystem_access():
    index = recipes_index()
    assert "- tesco" in index and "- amazon" in index
    body = recipe("tesco")
    assert "sender_domains" in body and "delivery_receipt" in body


def test_recipe_names_cannot_escape_the_recipes_directory():
    for bad in ("../pyproject", "..", "/etc/passwd", "tesco/../../x", "", "Tesco!"):
        assert "Invalid retailer name" in recipe(bad)
    assert "No recipe for" in recipe("waitrose")


def test_assets_resolve_from_a_checkout_or_an_installed_package():
    assert server._asset("recipes/tesco.yaml") is not None
    assert server._asset("recipes/nope.yaml") is None


# --- Migration safety: strangers hold these files ---------------------------

def test_a_crash_mid_rebuild_leaves_the_database_recoverable(tmp_path, monkeypatch):
    """The rebuilds DROP the only table holding the user's history. If the
    version bump and the rebuild did not commit together, a crash between them
    left a database with no events and a leftover events_v2 — which every
    later init_db() would then die on, permanently."""
    db = tmp_path / "crash.db"
    conn = server._connect(db)
    for stmt in server._statements(server.MIGRATIONS[0]):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO items (name) VALUES ('milk')")
    conn.execute("INSERT INTO events (item_id, type, quantity, unit, occurred_at, source, source_ref)"
                 " VALUES (1, 'bought', 2, 'l', '2024-01-01', 'email', 'o1')")
    conn.commit()
    conn.close()

    broken = server.MIGRATIONS[1].replace("ALTER TABLE events_v2 RENAME TO events;",
                                          "INSERT INTO no_such_table VALUES (1);")
    monkeypatch.setattr(server, "MIGRATIONS", [server.MIGRATIONS[0], broken])
    with pytest.raises(sqlite3.OperationalError):
        server.init_db(db)
    monkeypatch.undo()

    server.init_db(db)  # the failed rebuild rolled back, so this still works
    conn = server._connect(db)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(server.MIGRATIONS)
    conn.close()


def test_two_processes_may_migrate_the_same_database_at_once(tmp_path):
    """The documented topology is two processes over one file, so both can
    start against a v1 database and both decide to migrate."""
    db = tmp_path / "race.db"
    conn = server._connect(db)
    for stmt in server._statements(server.MIGRATIONS[0]):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    code = f"import sys; sys.path.insert(0, {str(Path.cwd())!r})\nfrom homestock import server\nserver.init_db({str(db)!r})"
    procs = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(3)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0]
    conn = server._connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(server.MIGRATIONS)
    conn.close()


def test_a_database_from_a_newer_release_is_refused_loudly(fresh_db):
    """Silently opening it is worse: an older build counts 'corrected' rows as
    purchases and miscounts every estimate without saying so."""
    conn = server._connect(fresh_db)
    conn.execute(f"PRAGMA user_version = {len(server.MIGRATIONS) + 5}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="Upgrade HomeStock"):
        server.init_db(fresh_db)


# --- Provenance: a date you invented is worse than no row -------------------

@pytest.mark.parametrize("source", server.DATED_SOURCES)
def test_dated_sources_must_carry_their_own_date(source):
    r = add_items(items=[{"name": "milk", "quantity": 1, "unit": "l", "line_no": 0}],
                  source=source, source_ref=f"{source}:1")
    assert r["inserted"] == 0
    assert "purchased_at is required" in r["rejected"][0]["reason"]


@pytest.mark.parametrize("source", ["manual", "barcode"])
def test_the_doors_a_person_walks_through_now_may_default_to_today(source):
    r = add_items(items=[{"name": "milk", "quantity": 1, "unit": "l", "line_no": 0}],
                  source=source, source_ref=f"{source}:1")
    assert r["inserted"] == 1
    assert get_events("milk")[0]["occurred_at"] == today.isoformat()


# --- get_events pages, because a full log will not fit a context window -----

def test_get_events_pages_rather_than_returning_the_whole_log():
    for i in range(25):
        seed_receipt("milk", i + 1, f"m{i}")
    page = get_events_page("milk", limit=10)
    assert page["returned"] == 10 and page["total"] == 25 and page["has_more"] is True
    assert page["offset"] == 0
    rest = get_events_page("milk", limit=10, offset=20)
    assert rest["returned"] == 5 and rest["has_more"] is False
    # oldest first, and the pages join up without gaps or repeats
    seen = [e["occurred_at"] for e in
            get_events_page("milk", limit=10)["events"]
            + get_events_page("milk", limit=10, offset=10)["events"]
            + rest["events"]]
    assert seen == sorted(seen) and len(set(seen)) == 25


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 10_000}, {"limit": "10"},
                                    {"limit": True}, {"offset": -1}, {"since": "2026-9-1"}])
def test_get_events_rejects_nonsense_instead_of_answering_wrongly(kwargs):
    assert "error" in get_events_page(**kwargs)


def test_a_recipe_name_cannot_smuggle_a_newline():
    """'$' also matches before a trailing newline in Python; this value is
    interpolated into a filesystem path."""
    assert server._RECIPE_NAME.match("tesco\n") is None
    assert server._RECIPE_NAME.match("tesco") is not None

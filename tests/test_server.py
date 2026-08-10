"""HomeStock server tests. Run: uv run pytest"""

import subprocess
import sys
from datetime import date, timedelta

import pytest

from homestock import server


def _fn(tool):
    return tool.fn if hasattr(tool, "fn") else tool


add_items = _fn(server.add_items)
get_stock = _fn(server.get_stock)
what_should_i_order = _fn(server.what_should_i_order)
void_event = _fn(server.void_event)
record_ingest_run = _fn(server.record_ingest_run)
get_events = _fn(server.get_events)

today = date.today()


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
                        server.MIGRATIONS + ["ALTER TABLE items ADD COLUMN emoji TEXT;"])
    server.init_db(fresh_db)
    conn = server._connect(fresh_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
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

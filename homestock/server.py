"""HomeStock — local-first household inventory for AI agents.

Six MCP tools over an append-only SQLite event log. The server is dumb about
semantics (the agent parses receipts and names items) and smart about
statistics (intervals, thresholds — deterministic math over events).

Run:  uv run python -m homestock     (stdio MCP server)
DB :  $HOMESTOCK_DB or ./homestock.db

Schema lifecycle: init_db() runs once at startup and applies numbered
migrations tracked by SQLite's PRAGMA user_version. Tool-call connections
never touch the schema.

Two-process topology (v1): the LLM client spawns one server process, and the
scheduled ingestion helper may write concurrently through its own process.
WAL mode + busy_timeout make concurrent writers queue instead of erroring.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(os.environ.get("HOMESTOCK_DB", Path.cwd() / "homestock.db"))

VALID_UNITS = ("unit", "g", "kg", "ml", "l", "pack")
VALID_SOURCES = ("email", "manual")
# ISO-8601 UTC: full timestamp or bare date (receipts often have no time)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}Z)?$")

_SCHEMA_V1 = """
CREATE TABLE items (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  category   TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  type        TEXT    NOT NULL CHECK (type = 'bought'),
  quantity    REAL    NOT NULL CHECK (quantity > 0),
  unit        TEXT    NOT NULL CHECK (unit IN ('unit','g','kg','ml','l','pack')),
  price       REAL    CHECK (price IS NULL OR price >= 0),
  location    TEXT,
  occurred_at TEXT    NOT NULL CHECK (occurred_at >= '2015-01-01'),
  source      TEXT    NOT NULL CHECK (source IN ('email','manual')),
  source_ref  TEXT    NOT NULL,
  line_no     INTEGER NOT NULL DEFAULT 0,
  voided      INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Idempotency: per receipt LINE, among live rows only (void-then-reinsert works)
CREATE UNIQUE INDEX idx_events_dedup ON events (source_ref, line_no) WHERE voided = 0;

CREATE TABLE ingest_runs (
  id             INTEGER PRIMARY KEY,
  ran_at         TEXT NOT NULL DEFAULT (datetime('now')),
  window_start   TEXT NOT NULL,
  window_end     TEXT NOT NULL,
  emails_seen    INTEGER NOT NULL,
  events_written INTEGER NOT NULL,
  skipped        TEXT
);
"""

# Append-only. MIGRATIONS[n] moves user_version n -> n+1. Never edit a shipped
# migration: third parties hold homestock.db files, so this list is a public
# contract (CEO plan D24/D27).
MIGRATIONS: list[str] = [_SCHEMA_V1]


def _connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str | None = None) -> None:
    """Apply pending migrations. Runs once at startup, never per tool call."""
    conn = _connect(path or DB_PATH)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for n in range(version, len(MIGRATIONS)):
            conn.executescript(MIGRATIONS[n])
            conn.execute(f"PRAGMA user_version = {n + 1}")
            conn.commit()
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    return _connect(DB_PATH)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _validate_occurred_at(value: str) -> str | None:
    """Return an error string, or None if valid."""
    if not ISO_RE.match(value):
        return f"occurred_at {value!r} is not ISO-8601 UTC (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)"
    if value[:10] < "2015-01-01":
        return f"occurred_at {value!r} predates 2015-01-01"
    if date.fromisoformat(value[:10]) > _today() + timedelta(days=1):
        return f"occurred_at {value!r} is more than 1 day in the future"
    return None


def _canonical_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _item_stats(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT id, category FROM items WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    dates = [
        r["d"]
        for r in conn.execute(
            "SELECT DISTINCT date(occurred_at) AS d FROM events "
            "WHERE item_id = ? AND voided = 0 ORDER BY d",
            (row["id"],),
        )
    ]
    last = conn.execute(
        "SELECT quantity, unit FROM events WHERE item_id = ? AND voided = 0 "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    purchases = len(dates)  # distinct purchase dates: same-day lines add no interval
    median_interval = None
    if purchases >= 2:
        ds = [date.fromisoformat(d) for d in dates]
        intervals = [(b - a).days for a, b in zip(ds, ds[1:])]
        median_interval = statistics.median(intervals)
    days_since = (_today() - date.fromisoformat(dates[-1])).days if dates else None

    if purchases < 2 or not median_interval:
        state = "unknown"
    else:
        ratio = days_since / median_interval
        state = "likely_in_stock" if ratio < 0.75 else "likely_low" if ratio < 1.0 else "likely_out"

    return {
        "name": name,
        "estimated_state": state,
        "days_since_last_purchase": days_since,
        "median_interval_days": median_interval,
        "purchases_observed": purchases,
        "last_quantity": last["quantity"] if last else None,
        "last_unit": last["unit"] if last else None,
    }


mcp = FastMCP("homestock")


@mcp.tool()
def add_items(
    items: list[dict],
    source: str,
    source_ref: str,
    purchased_at: str | None = None,
    location: str | None = None,
) -> dict:
    """Record a purchase (one receipt). Idempotent per (source_ref, line_no).

    Each item: {name, quantity, unit, price?, category?, line_no}.
    source: 'email' (purchased_at required — the receipt has a date) or
    'manual' (purchased_at defaults to today).
    source_ref: retailer ORDER ID where extractable, else email Message-ID;
    manual fallback 'manual:<retailer>:<YYYY-MM-DD>:<total>' (+':2' suffix on
    collision — a nonzero 'ignored' count on a fresh receipt signals one).
    Call get_stock() first and reuse existing item names exactly.
    Returns {inserted, ignored, rejected: [{line_no, reason}]}.
    """
    if source not in VALID_SOURCES:
        return {"inserted": 0, "ignored": 0, "rejected": [{"line_no": None, "reason": f"source must be one of {VALID_SOURCES}"}]}
    if purchased_at is None:
        if source == "email":
            return {"inserted": 0, "ignored": 0, "rejected": [{"line_no": None, "reason": "purchased_at is required for source='email' — the receipt has a date; do not guess"}]}
        purchased_at = _today().isoformat()
    if err := _validate_occurred_at(purchased_at):
        return {"inserted": 0, "ignored": 0, "rejected": [{"line_no": None, "reason": err}]}

    inserted = ignored = 0
    rejected: list[dict] = []
    with get_db() as conn:
        for it in items:
            line_no = it.get("line_no", 0)
            name = _canonical_name(str(it.get("name", "")))
            qty = it.get("quantity")
            unit = it.get("unit")
            price = it.get("price")
            reason = None
            if not name:
                reason = "name is empty"
            elif not isinstance(qty, (int, float)) or qty <= 0:
                reason = f"quantity must be a number > 0, got {qty!r}"
            elif unit not in VALID_UNITS:
                reason = f"unit must be one of {VALID_UNITS}, got {unit!r}"
            elif price is not None and (not isinstance(price, (int, float)) or price < 0):
                reason = f"price must be >= 0 or omitted, got {price!r}"
            if reason:
                rejected.append({"line_no": line_no, "reason": reason})
                continue
            conn.execute(
                "INSERT INTO items (name, category) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET category = COALESCE(items.category, excluded.category)",
                (name, it.get("category")),
            )
            item_id = conn.execute("SELECT id FROM items WHERE name = ?", (name,)).fetchone()["id"]
            cur = conn.execute(
                "INSERT OR IGNORE INTO events "
                "(item_id, type, quantity, unit, price, location, occurred_at, source, source_ref, line_no) "
                "VALUES (?, 'bought', ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, qty, unit, price, location, purchased_at, source, source_ref, line_no),
            )
            if cur.rowcount:
                inserted += 1
            else:
                ignored += 1
    return {"inserted": inserted, "ignored": ignored, "rejected": rejected}


@mcp.tool()
def get_stock(item: str | None = None) -> dict | list[dict]:
    """Current stock estimate. No argument: list all known items as
    {name, category} — call this before add_items to reuse exact names.
    With an item name: naive-baseline estimate with raw provenance
    (estimated_state is 'unknown' below 2 distinct purchase dates)."""
    with get_db() as conn:
        if item is None:
            return [
                {"name": r["name"], "category": r["category"]}
                for r in conn.execute("SELECT name, category FROM items ORDER BY name")
            ]
        stats = _item_stats(conn, _canonical_name(item))
        return stats if stats else {"error": f"unknown item {item!r} — get_stock() lists known names"}


@mcp.tool()
def what_should_i_order() -> list[dict]:
    """Items past their typical repurchase interval: >= 3 distinct purchase
    dates, days_since >= median interval, excluding items past 3x median
    (presumed discontinued). Sorted by overshoot ratio, highest first."""
    out = []
    with get_db() as conn:
        for r in conn.execute("SELECT name FROM items ORDER BY name"):
            s = _item_stats(conn, r["name"])
            if not s or s["purchases_observed"] < 3 or not s["median_interval_days"]:
                continue
            ratio = s["days_since_last_purchase"] / s["median_interval_days"]
            if 1.0 <= ratio <= 3.0:
                out.append({**s, "overshoot": round(ratio, 2)})
    return sorted(out, key=lambda x: -x["overshoot"])


@mcp.tool()
def void_event(source_ref: str, line_no: int | None = None) -> dict:
    """Correction path: void matching event lines (all lines of the receipt if
    line_no omitted). Voided rows are excluded from every estimate; re-inserting
    a corrected line for the same (source_ref, line_no) then works. Returns
    {voided: n}; zero matches is a no-op, not an error."""
    with get_db() as conn:
        if line_no is None:
            cur = conn.execute("UPDATE events SET voided = 1 WHERE source_ref = ? AND voided = 0", (source_ref,))
        else:
            cur = conn.execute(
                "UPDATE events SET voided = 1 WHERE source_ref = ? AND line_no = ? AND voided = 0",
                (source_ref, line_no),
            )
    return {"voided": cur.rowcount}


@mcp.tool()
def record_ingest_run(
    window_start: str,
    window_end: str,
    emails_seen: int,
    events_written: int,
    skipped: list[dict] | None = None,
) -> dict:
    """Ingestion agent plumbing: record a run (heartbeat + audit) after every
    ingestion pass, even one that wrote nothing. skipped: [{source_ref, reason}]
    for receipts/lines the agent could not parse (never guess — skip and log).
    Returns the backfill cursor: earliest_window_start already covered."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ingest_runs (window_start, window_end, emails_seen, events_written, skipped) "
            "VALUES (?, ?, ?, ?, ?)",
            (window_start, window_end, emails_seen, events_written, json.dumps(skipped) if skipped else None),
        )
        cursor = conn.execute("SELECT MIN(window_start) AS c FROM ingest_runs").fetchone()["c"]
    return {"earliest_window_start": cursor}


@mcp.tool()
def get_events(item: str | None = None, since: str | None = None) -> list[dict]:
    """Read-only raw event access (explainability: every estimate is
    recomputable from these rows). Includes voided rows, flagged."""
    q = (
        "SELECT i.name, e.type, e.quantity, e.unit, e.price, e.location, "
        "e.occurred_at, e.source, e.source_ref, e.line_no, e.voided "
        "FROM events e JOIN items i ON i.id = e.item_id WHERE 1=1"
    )
    args: list = []
    if item is not None:
        q += " AND i.name = ?"
        args.append(_canonical_name(item))
    if since is not None:
        q += " AND e.occurred_at >= ?"
        args.append(since)
    q += " ORDER BY e.occurred_at, e.id"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, args)]


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()

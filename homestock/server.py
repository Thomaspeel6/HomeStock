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
VALID_STORAGE = ("pantry", "fridge", "freezer")
# How stale an ingestion heartbeat may get before get_health() calls it stale.
STALE_INGEST_DAYS = 10
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

# v2 — ground truth and shelf life.
#
# Two things the app tier needs that v1 could not express:
#   1. Events the *user* asserts, not the receipt: 'corrected' (an observed
#      quantity, 0 meaning "we're out") and 'discarded' (waste, which is also
#      the signal that a shelf-life estimate was wrong). v1's
#      CHECK (type = 'bought') forbids both, and SQLite cannot ALTER a CHECK —
#      hence a table rebuild rather than an ADD COLUMN.
#   2. Shelf life on items, so perishables can be surfaced before they rot.
#
# The dedup index narrows to type='bought': receipt idempotency is a property
# of receipt lines, and user corrections carry a synthetic source_ref that
# would otherwise collide with each other.
_SCHEMA_V2 = """
CREATE TABLE events_v2 (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  type        TEXT    NOT NULL CHECK (type IN ('bought','consumed','discarded','corrected')),
  quantity    REAL    NOT NULL CHECK (quantity >= 0),
  unit        TEXT    NOT NULL CHECK (unit IN ('unit','g','kg','ml','l','pack')),
  price       REAL    CHECK (price IS NULL OR price >= 0),
  location    TEXT,
  occurred_at TEXT    NOT NULL CHECK (occurred_at >= '2015-01-01'),
  source      TEXT    NOT NULL CHECK (source IN ('email','manual','user')),
  source_ref  TEXT    NOT NULL,
  line_no     INTEGER NOT NULL DEFAULT 0,
  voided      INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO events_v2 (id, item_id, type, quantity, unit, price, location,
                       occurred_at, source, source_ref, line_no, voided, recorded_at)
  SELECT id, item_id, type, quantity, unit, price, location,
         occurred_at, source, source_ref, line_no, voided, recorded_at
  FROM events;

DROP TABLE events;
ALTER TABLE events_v2 RENAME TO events;

CREATE UNIQUE INDEX idx_events_dedup ON events (source_ref, line_no)
  WHERE voided = 0 AND type = 'bought';
CREATE INDEX idx_events_item ON events (item_id, occurred_at);

ALTER TABLE items ADD COLUMN shelf_life_days INTEGER;
ALTER TABLE items ADD COLUMN storage TEXT;
"""

# Append-only. MIGRATIONS[n] moves user_version n -> n+1. Never edit a shipped
# migration: third parties hold homestock.db files, so this list is a public
# contract (CEO plan D24/D27).
MIGRATIONS: list[str] = [_SCHEMA_V1, _SCHEMA_V2]


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


def _interval_confidence(intervals: list[int]) -> tuple[float | None, str]:
    """Coefficient of variation over repurchase intervals, and a label.

    Principle 3 of the PRD says uncertainty is data, not failure — so how
    *regular* a habit is has to travel with the estimate. Milk every 5 days
    like clockwork and milk bought twice at random both produced the same
    unqualified label in v1; they do not now.
    """
    if len(intervals) < 2:
        return None, "low"
    mean = statistics.mean(intervals)
    if not mean:
        return None, "low"
    cv = statistics.pstdev(intervals) / mean
    if len(intervals) >= 4 and cv <= 0.35:
        label = "high"
    elif len(intervals) >= 2 and cv <= 0.60:
        label = "medium"
    else:
        label = "low"
    return round(cv, 3), label


def _item_stats(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute(
        "SELECT id, category, shelf_life_days, storage FROM items WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    # Intervals model *repurchase*, so only 'bought' events count toward them.
    dates = [
        r["d"]
        for r in conn.execute(
            "SELECT DISTINCT date(occurred_at) AS d FROM events "
            "WHERE item_id = ? AND voided = 0 AND type = 'bought' ORDER BY d",
            (row["id"],),
        )
    ]
    last = conn.execute(
        "SELECT quantity, unit FROM events WHERE item_id = ? AND voided = 0 "
        "AND type = 'bought' ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    # The user telling us what they can see beats anything we infer from receipts.
    correction = conn.execute(
        "SELECT quantity, unit, date(occurred_at) AS d FROM events "
        "WHERE item_id = ? AND voided = 0 AND type = 'corrected' "
        "ORDER BY occurred_at DESC, id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()

    purchases = len(dates)  # distinct purchase dates: same-day lines add no interval
    median_interval = cv = None
    confidence = "low"
    if purchases >= 2:
        ds = [date.fromisoformat(d) for d in dates]
        intervals = [(b - a).days for a, b in zip(ds, ds[1:])]
        median_interval = statistics.median(intervals)
        cv, confidence = _interval_confidence(intervals)
    days_since = (_today() - date.fromisoformat(dates[-1])).days if dates else None

    # A correction that postdates the last receipt resets the clock: it is an
    # observation, not an inference, so it also outranks the interval model.
    confirmed = bool(correction and (not dates or correction["d"] >= dates[-1]))
    days_since_observation = days_since
    if confirmed:
        days_since_observation = (_today() - date.fromisoformat(correction["d"])).days

    if confirmed and correction["quantity"] == 0:
        state = "likely_out"
        confidence = "high"
    elif purchases < 2 or not median_interval:
        state = "unknown"
    else:
        ratio = days_since_observation / median_interval
        state = "likely_in_stock" if ratio < 0.75 else "likely_low" if ratio < 1.0 else "likely_out"

    expires_on = None
    if row["shelf_life_days"] and dates:
        expires_on = (
            date.fromisoformat(dates[-1]) + timedelta(days=row["shelf_life_days"])
        ).isoformat()

    return {
        "name": name,
        "category": row["category"],
        "estimated_state": state,
        "confirmed": confirmed,
        "confidence": confidence,
        "interval_variation": cv,
        "days_since_last_purchase": days_since,
        "days_since_observation": days_since_observation,
        "median_interval_days": median_interval,
        "purchases_observed": purchases,
        "last_quantity": last["quantity"] if last else None,
        "last_unit": last["unit"] if last else None,
        "corrected_quantity": correction["quantity"] if confirmed else None,
        "corrected_on": correction["d"] if confirmed else None,
        "shelf_life_days": row["shelf_life_days"],
        "storage": row["storage"],
        "expires_on": expires_on,
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
    """The shopping list. Two ways onto it:

    - The user said they were out (`correct_stock(item, 0)`) — ranked first,
      because an observation beats an estimate.
    - Inference: >= 3 distinct purchase dates, past the median repurchase
      interval, excluding items past 3x median (presumed discontinued).

    Sorted by overshoot ratio, highest first."""
    out = []
    with get_db() as conn:
        for r in conn.execute("SELECT name FROM items ORDER BY name"):
            s = _item_stats(conn, r["name"])
            if not s:
                continue
            # Confirmed out and bought before => needs rebuying, however thin
            # the purchase history is.
            if s["confirmed"] and s["corrected_quantity"] == 0 and s["purchases_observed"] >= 1:
                out.append({**s, "overshoot": 999.0, "reason": "you said you were out"})
                continue
            if s["purchases_observed"] < 3 or not s["median_interval_days"]:
                continue
            ratio = s["days_since_observation"] / s["median_interval_days"]
            if 1.0 <= ratio <= 3.0:
                out.append({**s, "overshoot": round(ratio, 2), "reason": "past its usual cycle"})
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, category, shelf_life_days, storage FROM items WHERE name = ?", (name,)
    ).fetchone()


def _record_user_event(kind: str, item: str, quantity: float, unit: str | None) -> dict:
    """Shared path for the events the user asserts rather than a receipt."""
    name = _canonical_name(item)
    if not isinstance(quantity, (int, float)) or quantity < 0:
        return {"error": f"quantity must be a number >= 0, got {quantity!r}"}
    with get_db() as conn:
        row = _lookup(conn, name)
        if row is None:
            return {"error": f"unknown item {item!r} — get_stock() lists known names"}
        if unit is None:
            prev = conn.execute(
                "SELECT unit FROM events WHERE item_id = ? AND voided = 0 "
                "ORDER BY occurred_at DESC, id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            unit = prev["unit"] if prev else "unit"
        if unit not in VALID_UNITS:
            return {"error": f"unit must be one of {VALID_UNITS}, got {unit!r}"}
        now = _now_iso()
        conn.execute(
            "INSERT INTO events (item_id, type, quantity, unit, occurred_at, source, source_ref) "
            "VALUES (?, ?, ?, ?, ?, 'user', ?)",
            (row["id"], kind, quantity, unit, now, f"user:{kind}:{now}"),
        )
        return {"recorded": kind, "item": name, "quantity": quantity, "unit": unit, "at": now}


@mcp.tool()
def correct_stock(item: str, quantity: float, unit: str | None = None) -> dict:
    """Ground truth from the user: what they can actually see. `quantity=0`
    means "we're out". A correction outranks the interval model and resets
    the estimate's clock, so this is the one call that makes an estimate
    right rather than merely plausible.

    Use it whenever the user volunteers stock in conversation ("we're out of
    milk", "still got two bags of rice"). The item must already be known —
    stock is inferred from purchases, so record the receipt first."""
    return _record_user_event("corrected", item, quantity, unit)


@mcp.tool()
def discard_item(item: str, quantity: float = 1, unit: str | None = None) -> dict:
    """Record waste — food thrown away. Two uses: it removes the item from
    stock, and it is the only signal that a shelf-life estimate was too
    generous. Distinct from correct_stock, which says what is left."""
    return _record_user_event("discarded", item, quantity, unit)


@mcp.tool()
def set_shelf_life(item: str, days: int, storage: str | None = None) -> dict:
    """Teach HomeStock how long an item keeps, so it can be flagged before it
    rots. The agent supplies a sensible default on first purchase (fresh
    chicken ~3 days, milk ~7); the user overrides it. storage is one of
    pantry/fridge/freezer and is advisory — it explains the number."""
    name = _canonical_name(item)
    if not isinstance(days, int) or days <= 0:
        return {"error": f"days must be a positive integer, got {days!r}"}
    if storage is not None and storage not in VALID_STORAGE:
        return {"error": f"storage must be one of {VALID_STORAGE}, got {storage!r}"}
    with get_db() as conn:
        row = _lookup(conn, name)
        if row is None:
            return {"error": f"unknown item {item!r} — get_stock() lists known names"}
        conn.execute(
            "UPDATE items SET shelf_life_days = ?, storage = COALESCE(?, storage) WHERE id = ?",
            (days, storage, row["id"]),
        )
    return {"item": name, "shelf_life_days": days, "storage": storage or row["storage"]}


@mcp.tool()
def get_expiring_soon(within_days: int = 3) -> list[dict]:
    """Perishables at or past their estimated use-by, soonest first. Only
    items with a shelf life set and at least one purchase.

    Two exclusions keep this list honest rather than merely long:
    items the user has confirmed are gone (they cannot spoil), and items
    expired for longer than their own shelf life again — a chicken ten days
    past a three-day use-by was eaten or binned a week ago, and nagging about
    it is how a list stops being read."""
    if not isinstance(within_days, int) or within_days < 0:
        return []
    cutoff = (_today() + timedelta(days=within_days)).isoformat()
    out = []
    with get_db() as conn:
        for r in conn.execute(
            "SELECT name FROM items WHERE shelf_life_days IS NOT NULL ORDER BY name"
        ):
            s = _item_stats(conn, r["name"])
            if not s or not s["expires_on"] or s["expires_on"] > cutoff:
                continue
            if s["confirmed"] and s["corrected_quantity"] == 0:
                continue
            days_left = (date.fromisoformat(s["expires_on"]) - _today()).days
            if days_left < -s["shelf_life_days"]:
                continue
            out.append({**s, "days_left": days_left})
    return sorted(out, key=lambda x: x["expires_on"])


@mcp.tool()
def merge_items(from_item: str, into_item: str) -> dict:
    """Fix name drift: fold every event on `from_item` into `into_item` and
    delete the duplicate. Canonicalisation is the agent's job, so duplicates
    ("milk" vs "semi-skimmed milk") are a matter of when, not if — without
    this the only remedy is hand-editing SQLite.

    Merging is irreversible in one step but loses nothing: the events survive
    under the surviving name, and get_events() still shows them."""
    src, dst = _canonical_name(from_item), _canonical_name(into_item)
    if not src or not dst:
        return {"error": "both item names are required"}
    if src == dst:
        return {"error": "from_item and into_item are the same name"}
    with get_db() as conn:
        a, b = _lookup(conn, src), _lookup(conn, dst)
        if a is None:
            return {"error": f"unknown item {from_item!r}"}
        if b is None:
            return {"error": f"unknown item {into_item!r} — merge into a name that exists"}
        moved = conn.execute(
            "UPDATE events SET item_id = ? WHERE item_id = ?", (b["id"], a["id"])
        ).rowcount
        # The survivor inherits anything it was missing rather than losing it.
        conn.execute(
            "UPDATE items SET category = COALESCE(category, ?), "
            "shelf_life_days = COALESCE(shelf_life_days, ?), storage = COALESCE(storage, ?) "
            "WHERE id = ?",
            (a["category"], a["shelf_life_days"], a["storage"], b["id"]),
        )
        conn.execute("DELETE FROM items WHERE id = ?", (a["id"],))
    return {"merged": src, "into": dst, "events_moved": moved}


@mcp.tool()
def get_health() -> dict:
    """Diagnostics: is HomeStock actually working? Counts and timestamps only —
    never item names, never email content — so the output is safe to paste
    into a bug report.

    `stale` is the one that matters. Ingestion failing silently (expired mail
    auth, a retailer changing template) looks exactly like a quiet week, and
    the product's whole claim is that its answers can be trusted."""
    db = Path(DB_PATH)
    with get_db() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        items = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        ev = conn.execute(
            "SELECT COUNT(*) AS n, SUM(voided) AS v, MIN(date(occurred_at)) AS lo, "
            "MAX(date(occurred_at)) AS hi, "
            "SUM(type = 'bought') AS bought FROM events"
        ).fetchone()
        run = conn.execute(
            "SELECT ran_at, window_end, emails_seen, events_written, skipped "
            "FROM ingest_runs ORDER BY ran_at DESC, id DESC LIMIT 1"
        ).fetchone()
        runs = conn.execute("SELECT COUNT(*) AS n FROM ingest_runs").fetchone()["n"]
    days_since_ingest = None
    if run:
        days_since_ingest = (_today() - date.fromisoformat(run["ran_at"][:10])).days
    return {
        "schema_version": version,
        "db_path": str(db),
        "db_size_bytes": db.stat().st_size if db.exists() else 0,
        "items": items,
        "events": ev["n"],
        "receipt_lines": ev["bought"] or 0,
        "events_voided": ev["v"] or 0,
        "earliest_event": ev["lo"],
        "latest_event": ev["hi"],
        "ingest_runs": runs,
        "last_ingest_at": run["ran_at"] if run else None,
        "last_ingest_events_written": run["events_written"] if run else None,
        "days_since_ingest": days_since_ingest,
        "stale": days_since_ingest is None or days_since_ingest > STALE_INGEST_DAYS,
        "stale_after_days": STALE_INGEST_DAYS,
    }


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()

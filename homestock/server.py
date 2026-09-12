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
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DB_PATH = Path(os.environ.get("HOMESTOCK_DB", Path.cwd() / "homestock.db"))

VALID_UNITS = ("unit", "g", "kg", "ml", "l", "pack")
# Doors into the stock room. 'user' and 'recipe' are written by the tools
# below rather than by add_items, so they are not offered here.
VALID_SOURCES = ("email", "manual", "photo", "barcode", "loyalty")
# Sources whose evidence carries its own date. Defaulting these to today would
# silently corrupt the repurchase intervals every estimate is derived from: a
# loyalty export is years of history, and a photographed till receipt is
# whenever the shopping happened. 'barcode' and 'manual' are the only doors a
# person walks through at the moment of buying, so only they may default.
DATED_SOURCES = ("email", "photo", "loyalty")
VALID_CAPTURE_KINDS = ("receipt_photo", "barcode", "note")
VALID_STORAGE = ("pantry", "fridge", "freezer")
MAX_EVENT_PAGE = 1000   # get_events hard cap; a full log will not fit a context window
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

# v3 — many doors into the stock room.
#
# Receipt emails only ever saw online and delivery orders. Buying milk in a
# corner shop was invisible, so the shopping list confidently asked for milk
# already in the fridge — and a list that is confidently wrong stops being
# read. v3 adds paper receipts, barcodes, loyalty exports and typed notes.
#
# Two tables carry the weight:
#
#   aliases  — every door names things differently. A barcode says "Tesco
#              British Semi Skimmed Milk 2.27L", a receipt line says "TESCO
#              SEMI SKMD MILK", a person says "milk". Left alone those become
#              three items with three wrong repurchase cycles. Canonicalisation
#              is still the model's job; this is where its answers persist, so
#              the same raw string never has to be resolved twice.
#
#   captures — the inbox. A photo or barcode from any device lands here
#              unread, and an agent turns it into items later. Keeps the
#              capture path (which must be instant, offline and phone-shaped)
#              separate from the reading path (which needs a model).
#              Photos are files on disk, not blobs: the DB stays small and
#              copyable, and an agent can just read the path.
_SCHEMA_V3 = """
CREATE TABLE events_v3 (
  id          INTEGER PRIMARY KEY,
  item_id     INTEGER NOT NULL REFERENCES items(id),
  type        TEXT    NOT NULL CHECK (type IN ('bought','consumed','discarded','corrected')),
  quantity    REAL    NOT NULL CHECK (quantity >= 0),
  unit        TEXT    NOT NULL CHECK (unit IN ('unit','g','kg','ml','l','pack')),
  price       REAL    CHECK (price IS NULL OR price >= 0),
  location    TEXT,
  occurred_at TEXT    NOT NULL CHECK (occurred_at >= '2015-01-01'),
  source      TEXT    NOT NULL CHECK (source IN
                ('email','manual','user','photo','barcode','loyalty','recipe')),
  source_ref  TEXT    NOT NULL,
  line_no     INTEGER NOT NULL DEFAULT 0,
  voided      INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO events_v3 (id, item_id, type, quantity, unit, price, location,
                       occurred_at, source, source_ref, line_no, voided, recorded_at)
  SELECT id, item_id, type, quantity, unit, price, location,
         occurred_at, source, source_ref, line_no, voided, recorded_at
  FROM events;

DROP TABLE events;
ALTER TABLE events_v3 RENAME TO events;

CREATE UNIQUE INDEX idx_events_dedup ON events (source_ref, line_no)
  WHERE voided = 0 AND type = 'bought';
CREATE INDEX idx_events_item ON events (item_id, occurred_at);

CREATE TABLE aliases (
  alias      TEXT PRIMARY KEY,
  item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  source     TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_aliases_item ON aliases (item_id);

CREATE TABLE captures (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('receipt_photo','barcode','note')),
  path        TEXT,
  text        TEXT,
  mime        TEXT,
  device      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','done','skipped')),
  note        TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);
CREATE INDEX idx_captures_status ON captures (status, created_at);
"""

# Append-only. MIGRATIONS[n] moves user_version n -> n+1. Never edit a shipped
# migration: third parties hold homestock.db files, so this list is a public
# contract (CEO plan D24/D27).
MIGRATIONS: list[str] = [_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3]


def _connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _statements(script: str):
    """Split a migration into whole statements.

    executescript() cannot be used here: it COMMITs any open transaction before
    it runs, which would throw away the very lock that makes a rebuild safe.
    sqlite3.complete_statement understands quoting, so this is not a naive
    split on ';'.
    """
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip():
        yield buf


def init_db(path: Path | str | None = None) -> None:
    """Apply pending migrations. Runs once at startup, never per tool call.

    Every migration after the first is a destructive table rebuild, and
    strangers hold these files. Two things therefore have to hold:

    * **Atomic.** A crash between `DROP TABLE events` and the version bump
      would leave a database with no event history and a leftover events_v2,
      which no later version could open. The rebuild and the `user_version`
      bump commit together or not at all.
    * **One process at a time.** The documented topology is two processes over
      one file, so both can start against a v1 database and both decide to
      migrate. BEGIN IMMEDIATE takes the write lock *before* we re-read the
      version, so the second process waits out its busy_timeout and then finds
      nothing left to do, rather than replaying a rebuild over a finished one.
    """
    db = path or DB_PATH
    conn = _connect(db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(
                f"{db} is schema v{version}; this build of HomeStock understands up to "
                f"v{len(MIGRATIONS)}. Upgrade HomeStock rather than downgrading the "
                "database — an older build reads newer rows as purchases and would "
                "silently miscount every estimate."
            )
        if version == len(MIGRATIONS):
            return
        # A no-op inside a transaction, so it has to be set before BEGIN.
        # SQLite's documented table-rebuild procedure requires it off.
        conn.execute("PRAGMA foreign_keys = OFF")
        while True:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= len(MIGRATIONS):
                conn.execute("ROLLBACK")
                break
            try:
                for stmt in _statements(MIGRATIONS[version]):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {version + 1}")
                if broken := conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError(f"migration {version + 1} broke a foreign key: {broken}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.close()


def get_db() -> sqlite3.Connection:
    return _connect(DB_PATH)


def _today() -> date:
    return datetime.now(UTC).date()


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


def _resolve(conn: sqlite3.Connection, raw: str) -> str:
    """Canonicalise a raw name, then follow any learned alias to the real item.

    Four input paths mean four vocabularies for the same milk. Resolution
    happens on the way in, so an alias learned once (from a merge, or taught
    by the agent) never has to be applied again downstream."""
    name = _canonical_name(raw)
    row = conn.execute("SELECT i.name FROM aliases a JOIN items i ON i.id = a.item_id "
                       "WHERE a.alias = ?", (name,)).fetchone()
    return row["name"] if row else name


def _captures_dir() -> Path:
    return Path(DB_PATH).parent / "captures"


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
        intervals = [(b - a).days for a, b in pairwise(ds)]
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

    # Where this item sits in its own repurchase cycle, and how many days past
    # due it is. Derived, but derived *here*: the window used to recompute both
    # in JavaScript, which meant the pantry page and an agent could quote
    # different numbers for the same item, and only one copy had tests.
    cycle_position = days_over = None
    if median_interval and days_since_observation is not None:
        cycle_position = round(days_since_observation / median_interval, 3)
        days_over = days_since_observation - round(median_interval)

    return {
        "name": name,
        "category": row["category"],
        "estimated_state": state,
        "confirmed": confirmed,
        "confidence": confidence,
        "interval_variation": cv,
        "days_since_last_purchase": days_since,
        "days_since_observation": days_since_observation,
        "cycle_position": cycle_position,
        "days_over": days_over,
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

    source, and whether purchased_at is required:
      'email'   a receipt email          — REQUIRED, the receipt carries a date
      'photo'   a photographed receipt   — REQUIRED, the paper carries a date
      'loyalty' a Clubcard/Nectar export — REQUIRED, the export carries a date
      'barcode' scanned in the shop      — optional, defaults to today
      'manual'  typed by the user        — optional, defaults to today
    Only the two "happening right now" doors may default. Dating an old paper
    receipt or a three-year loyalty export as today would silently corrupt the
    repurchase intervals that every estimate is derived from.

    source_ref: retailer ORDER ID where extractable, else email Message-ID;
    manual fallback 'manual:<retailer>:<YYYY-MM-DD>:<total>' (+':2' suffix on
    collision — a nonzero 'ignored' count on a fresh receipt signals one).
    Call get_stock() first and reuse existing item names exactly.
    Returns {inserted, ignored, rejected: [{line_no, reason}]}.
    """
    if source not in VALID_SOURCES:
        return {"inserted": 0, "ignored": 0, "rejected": [{"line_no": None, "reason": f"source must be one of {VALID_SOURCES}"}]}
    if purchased_at is None:
        if source in DATED_SOURCES:
            return {"inserted": 0, "ignored": 0, "rejected": [{"line_no": None, "reason": f"purchased_at is required for source={source!r} — the receipt carries a date; do not guess"}]}
        purchased_at = _today().isoformat()
    if err := _validate_occurred_at(purchased_at):
        return {"inserted": 0, "ignored": 0, "rejected": [{"line_no": None, "reason": err}]}

    inserted = ignored = 0
    rejected: list[dict] = []
    with get_db() as conn:
        for it in items:
            line_no = it.get("line_no", 0)
            name = _resolve(conn, str(it.get("name", "")))
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
        stats = _item_stats(conn, _resolve(conn, item))
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


def max_event_id() -> int:
    """Highest event id right now. Bracketing a tool call with this is how a
    chat turn learns exactly which rows it created, without every tool having
    to grow a 'tag what you wrote' parameter."""
    with get_db() as conn:
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def void_event_ids(ids: list[int]) -> int:
    """Void specific events. Used to undo one chat turn.

    Voiding, never deleting: the log is append-only, so an undone correction
    leaves a record that it happened and was withdrawn."""
    ids = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]
    if not ids:
        return 0
    with get_db() as conn:
        placeholders = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE events SET voided = 1 WHERE id IN ({placeholders}) AND voided = 0", ids)
        return cur.rowcount


@mcp.tool()
def get_events(item: str | None = None, since: str | None = None,
               limit: int = 200, offset: int = 0) -> dict:
    """Read-only raw event access (explainability: every estimate is
    recomputable from these rows). Includes voided rows, flagged.

    Oldest first. Scope with `item` and `since` (ISO-8601 UTC, YYYY-MM-DD)
    rather than paging the whole log: a few years of receipts is well over a
    hundred thousand rows, which no context window will hold.

    limit: 1..1000, default 200. offset: page forward from there.
    Returns {events: [...], returned, offset, total, has_more}.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_EVENT_PAGE:
        return {"error": f"limit must be an integer between 1 and {MAX_EVENT_PAGE}, got {limit!r}"}
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return {"error": f"offset must be an integer >= 0, got {offset!r}"}
    if since is not None and (err := _validate_occurred_at(since)):
        return {"error": err.replace("occurred_at", "since")}

    where = " WHERE 1=1"
    args: list = []
    with get_db() as conn:
        if item is not None:
            where += " AND i.name = ?"
            args.append(_resolve(conn, item))
        if since is not None:
            where += " AND e.occurred_at >= ?"
            args.append(since)
        frm = " FROM events e JOIN items i ON i.id = e.item_id" + where
        total = conn.execute("SELECT COUNT(*)" + frm, args).fetchone()[0]
        rows = conn.execute(
            "SELECT i.name, e.type, e.quantity, e.unit, e.price, e.location, "
            "e.occurred_at, e.source, e.source_ref, e.line_no, e.voided"
            + frm + " ORDER BY e.occurred_at, e.id LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return {"events": [dict(r) for r in rows], "returned": len(rows),
            "offset": offset, "total": total, "has_more": offset + len(rows) < total}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, category, shelf_life_days, storage FROM items WHERE name = ?", (name,)
    ).fetchone()


def _record_user_event(kind: str, item: str, quantity: float, unit: str | None) -> dict:
    """Shared path for the events the user asserts rather than a receipt."""
    if not isinstance(quantity, (int, float)) or quantity < 0:
        return {"error": f"quantity must be a number >= 0, got {quantity!r}"}
    with get_db() as conn:
        name = _resolve(conn, item)
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
    if not isinstance(days, int) or days <= 0:
        return {"error": f"days must be a positive integer, got {days!r}"}
    if storage is not None and storage not in VALID_STORAGE:
        return {"error": f"storage must be one of {VALID_STORAGE}, got {storage!r}"}
    with get_db() as conn:
        name = _resolve(conn, item)
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
        dst = _resolve(conn, dst)
        a, b = _lookup(conn, src), _lookup(conn, dst)
        if a is None:
            return {"error": f"unknown item {from_item!r}"}
        if b is None:
            return {"error": f"unknown item {into_item!r} — merge into a name that exists"}
        moved = conn.execute(
            "UPDATE events SET item_id = ? WHERE item_id = ?", (b["id"], a["id"])
        ).rowcount
        # Repoint before the delete, or ON DELETE CASCADE takes them with it.
        conn.execute("UPDATE aliases SET item_id = ? WHERE item_id = ?", (b["id"], a["id"]))
        # The losing name becomes an alias: the drift heals itself next time.
        conn.execute("INSERT OR IGNORE INTO aliases (alias, item_id, source) VALUES (?, ?, 'merge')",
                     (src, b["id"]))
        # The survivor inherits anything it was missing rather than losing it.
        conn.execute(
            "UPDATE items SET category = COALESCE(category, ?), "
            "shelf_life_days = COALESCE(shelf_life_days, ?), storage = COALESCE(storage, ?) "
            "WHERE id = ?",
            (a["category"], a["shelf_life_days"], a["storage"], b["id"]),
        )
        conn.execute("DELETE FROM items WHERE id = ?", (a["id"],))
    return {"merged": src, "into": dst, "events_moved": moved, "alias_learned": src}


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


@mcp.tool()
def add_alias(alias: str, item: str) -> dict:
    """Teach HomeStock that a raw string means an item it already knows.

    Four input paths produce four vocabularies for one thing: a barcode gives
    "tesco british semi skimmed milk 2.27l", a receipt line gives "tesco semi
    skmd milk", a person types "milk". Without this they become three items
    with three wrong repurchase cycles. Resolve a name once and record it here
    rather than resolving it on every ingest."""
    a = _canonical_name(alias)
    if not a:
        return {"error": "alias is required"}
    with get_db() as conn:
        target = _resolve(conn, item)
        row = _lookup(conn, target)
        if row is None:
            return {"error": f"unknown item {item!r} — get_stock() lists known names"}
        if a == target:
            return {"error": "an item is not an alias of itself"}
        # An alias that shadows a real item would silently hide its history.
        if _lookup(conn, a) is not None:
            return {"error": f"{a!r} is itself an item — use merge_items to fold them together"}
        conn.execute("INSERT INTO aliases (alias, item_id, source) VALUES (?, ?, 'agent') "
                     "ON CONFLICT(alias) DO UPDATE SET item_id = excluded.item_id",
                     (a, row["id"]))
    return {"alias": a, "resolves_to": target}


@mcp.tool()
def list_aliases(item: str | None = None) -> list[dict]:
    """Every learned name mapping, or just one item's. Useful for spotting
    canonicalisation going wrong before it poisons the estimates."""
    q = ("SELECT a.alias, i.name AS item, a.source, a.created_at FROM aliases a "
         "JOIN items i ON i.id = a.item_id")
    args: list = []
    with get_db() as conn:
        if item is not None:
            q += " WHERE i.name = ?"
            args.append(_resolve(conn, item))
        return [dict(r) for r in conn.execute(q + " ORDER BY a.alias", args)]


@mcp.tool()
def add_capture(kind: str, text: str | None = None, path: str | None = None,
                device: str | None = None, mime: str | None = None) -> dict:
    """Put something in the inbox to be read later: a photographed paper
    receipt, a scanned barcode, or a typed note ("2 milk, bread, 6 eggs").

    Capture and reading are deliberately separate. Capturing must work
    instantly, offline, one-handed, in a shop — reading needs a model. This is
    how a phone can contribute to a database it cannot reason about.

    Photos are stored as files (see path); the row records where. Nothing here
    is stock yet: an agent calls list_captures(), reads it, calls add_items(),
    then resolve_capture()."""
    if kind not in VALID_CAPTURE_KINDS:
        return {"error": f"kind must be one of {VALID_CAPTURE_KINDS}, got {kind!r}"}
    if not text and not path:
        return {"error": "a capture needs either text or a path"}
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO captures (kind, text, path, device, mime) VALUES (?, ?, ?, ?, ?)",
            (kind, text, path, device, mime),
        )
    return {"capture_id": cur.lastrowid, "kind": kind, "status": "pending"}


@mcp.tool()
def list_captures(status: str = "pending") -> list[dict]:
    """The inbox: things captured from any device and not yet turned into
    items. Read each one, call add_items() with what it contains, then
    resolve_capture(). Pass status='done' or 'skipped' to review history."""
    if status not in ("pending", "done", "skipped", "all"):
        return []
    q = ("SELECT id, kind, text, path, mime, device, status, note, created_at, resolved_at "
         "FROM captures")
    args: list = []
    if status != "all":
        q += " WHERE status = ?"
        args.append(status)
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at, id", args)]


@mcp.tool()
def resolve_capture(capture_id: int, status: str = "done", note: str | None = None) -> dict:
    """Close a capture once its contents are recorded — or mark it 'skipped'
    with a note when it cannot be read. Never guess at a blurry receipt; an
    unreadable capture that is honestly skipped is recoverable, a fabricated
    one is not."""
    if status not in ("done", "skipped", "pending"):
        return {"error": "status must be done, skipped or pending"}
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE captures SET status = ?, note = COALESCE(?, note), "
            "resolved_at = CASE WHEN ? = 'pending' THEN NULL ELSE ? END WHERE id = ?",
            (status, note, status, _now_iso(), capture_id),
        )
    if not cur.rowcount:
        return {"error": f"no capture {capture_id}"}
    return {"capture_id": capture_id, "status": status}


@mcp.tool()
def check_recipe(ingredients: list[str]) -> dict:
    """Can I cook this? Sorts a recipe's ingredients into what you have, what
    is running low, and what you would need to buy.

    The model turns a recipe — pasted text, a URL, a photo of a cookbook —
    into this list of ingredient names. The server only does the set maths
    against stock, because that is the part that must be exactly right."""
    have, low, missing, unknown = [], [], [], []
    with get_db() as conn:
        for raw in ingredients:
            name = _resolve(conn, str(raw))
            s = _item_stats(conn, name)
            if s is None:
                missing.append({"ingredient": raw, "reason": "never bought"})
            elif s["estimated_state"] == "likely_out":
                missing.append({"ingredient": raw, "item": name,
                                "reason": "you said you were out" if s["confirmed"]
                                          else "past its usual cycle"})
            elif s["estimated_state"] == "likely_low":
                low.append({"ingredient": raw, "item": name})
            elif s["estimated_state"] == "unknown":
                unknown.append({"ingredient": raw, "item": name})
            else:
                have.append({"ingredient": raw, "item": name})
    return {"can_cook": not missing, "have": have, "low": low,
            "missing": missing, "unsure": unknown}


@mcp.tool()
def consume_items(items: list[dict]) -> dict:
    """Record cooking. Each item: {name, quantity?, unit?, finished?}.

    Cooking is the largest real depletion event in a kitchen and nothing else
    models it — without this, stock only ever drains by inference. Set
    `finished: true` for anything you used the last of; that also records a
    correction to zero, which is what actually moves it onto the shopping
    list. Unknown items are reported, not invented."""
    consumed, finished, unknown = 0, 0, []
    for it in items:
        name = str(it.get("name", ""))
        qty = it.get("quantity", 1)
        r = _record_user_event("consumed", name, qty if isinstance(qty, (int, float)) else 1,
                               it.get("unit"))
        if "error" in r:
            unknown.append({"name": name, "reason": r["error"]})
            continue
        consumed += 1
        if it.get("finished"):
            _record_user_event("corrected", name, 0, it.get("unit"))
            finished += 1
    return {"consumed": consumed, "marked_finished": finished, "unknown": unknown}


# --- Prompts and resources -------------------------------------------------
#
# The tools are only half of what an agent needs. Until now the ingestion
# procedure lived in prompts/ and the retailer recipes in recipes/, which meant
# an MCP client had to also have filesystem access, in the right directory, to
# do the job — a requirement nothing in the protocol expresses. Serving them
# over MCP makes the server self-contained: connect it, and everything needed
# to run ingestion arrives with it.

# \Z, not $: in Python $ also matches before a trailing newline, so "tesco\n"
# would pass a check whose whole job is to keep this value out of a path.
_RECIPE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}\Z")


def _asset(rel: str) -> Path | None:
    """Find a shipped file, whether running from a checkout or an installed
    wheel (where pyproject force-includes these under the package)."""
    here = Path(__file__).resolve().parent
    for base in (here, here.parent):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


def _read_asset(rel: str, missing: str) -> str:
    path = _asset(rel)
    return path.read_text(encoding="utf-8") if path else missing


@mcp.prompt()
def onboarding() -> str:
    """First run: consent, backfill the user's receipt history, and give them
    the pantry reveal. Follow this before any ingestion."""
    return _read_asset("prompts/onboarding.md",
                       "prompts/onboarding.md is missing from this installation.")


@mcp.prompt()
def ingestion() -> str:
    """The scheduled ingestion procedure: which emails to read, how to record
    them, and the rules that keep it from guessing. Run this on a schedule, or
    as catch-up before answering questions about stock."""
    return _read_asset("prompts/ingestion.md",
                       "prompts/ingestion.md is missing from this installation.")


@mcp.resource("homestock://recipes")
def recipes_index() -> str:
    """Retailers HomeStock knows how to read, and nothing else is ever read.
    Each name resolves at homestock://recipes/{retailer}."""
    d = _asset("recipes/TEMPLATE.md")
    names = sorted(f.stem for f in d.parent.glob("*.yaml")) if d else []
    if not names:
        return "No recipes are installed."
    lines = ["Retailer recipes available (homestock://recipes/<name>):", ""]
    lines += [f"- {n}" for n in names]
    return "\n".join(lines)


@mcp.resource("homestock://recipes/{retailer}")
def recipe(retailer: str) -> str:
    """One retailer's recipe: which sender domains to trust, which email type
    carries the true line items, and how to read them. Data, never code."""
    if not _RECIPE_NAME.match(retailer or ""):
        return f"Invalid retailer name {retailer!r}."
    return _read_asset(f"recipes/{retailer}.yaml",
                       f"No recipe for {retailer!r} — see homestock://recipes for the list.")


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()

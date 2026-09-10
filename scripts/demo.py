"""Seed a demo household (6 months of realistic shopping) and show what the
agent sees: the pantry reveal + what to order.

    uv run python scripts/demo.py            print the reveal
    uv run python scripts/demo.py --serve    ...then open the pantry window
"""

import os
import sys
import tempfile
from datetime import date, timedelta

os.environ.setdefault("HOMESTOCK_DB", os.path.join(tempfile.mkdtemp(), "demo.db"))

from homestock import server

server.DB_PATH = os.environ["HOMESTOCK_DB"]
server.init_db()

def fn(tool):
    """FastMCP wraps each tool; the plain function is under .fn."""
    return tool.fn if hasattr(tool, "fn") else tool


add_items, get_stock, order = fn(server.add_items), fn(server.get_stock), fn(server.what_should_i_order)
set_shelf_life, expiring = fn(server.set_shelf_life), fn(server.get_expiring_soon)
record_ingest_run = fn(server.record_ingest_run)

today = date.today()

# (name, unit, qty, cadence_days, days_since_last_purchase, shelf_life_days)
HOUSEHOLD = [
    ("semi-skimmed milk", "l", 2, 5, 6, 7),
    ("eggs", "pack", 1, 9, 4, 21),
    ("sourdough bread", "unit", 1, 6, 2, 4),
    ("whole chicken", "kg", 1.4, 12, 13, 3),
    ("toilet roll", "pack", 1, 24, 31, None),
    ("dishwasher tablets", "pack", 1, 45, 12, None),
    ("oat flat white oat milk", "l", 1, 7, 8, 10),
    ("bin bags", "pack", 1, 40, 47, None),
    ("butter", "unit", 1, 14, 5, 30),
    ("bananas", "kg", 1, 5, 1, 6),
]

n = 0
for name, unit, qty, cadence, last, shelf in HOUSEHOLD:
    day = last
    while day <= 182:
        n += 1
        add_items(
            items=[{"name": name, "quantity": qty, "unit": unit, "line_no": 0}],
            source="email",
            source_ref=f"demo-order-{name.replace(' ', '-')}-{day}",
            purchased_at=(today - timedelta(days=day)).isoformat(),
        )
        day += cadence
    if shelf:
        set_shelf_life(name, shelf, storage="fridge" if shelf <= 10 else "pantry")

record_ingest_run(window_start=(today - timedelta(days=7)).isoformat(),
                  window_end=today.isoformat(), emails_seen=4, events_written=6)

print(f"Backfill complete: {n} receipts ingested across 6 months.\n")
print("=" * 62)
print("  YOUR PANTRY, PROBABLY  —  built from receipts, not typing")
print("=" * 62)

facts = []
for name, *_ in HOUSEHOLD:
    s = get_stock(name)
    if s["median_interval_days"] and s["median_interval_days"] <= 7:
        facts.append(f'You buy {name} every ~{int(s["median_interval_days"])} days.')
loo = get_stock("toilet roll")
facts.append(f'{loo["purchases_observed"]} packs of toilet roll in 6 months.')
print("\n".join(f"  * {f}" for f in facts[:4]))

print("\n  In stock (probably):")
for name, *_ in HOUSEHOLD:
    s = get_stock(name)
    if s["estimated_state"] == "likely_in_stock":
        print(f"    [ok]  {name:<28} bought {s['days_since_last_purchase']}d ago, cycle ~{s['median_interval_days']:.0f}d")

print("\n  Getting low:")
for name, *_ in HOUSEHOLD:
    s = get_stock(name)
    if s["estimated_state"] == "likely_low":
        print(f"    [~]   {name:<28} bought {s['days_since_last_purchase']}d ago, cycle ~{s['median_interval_days']:.0f}d")

print("\n  USE SOON:")
for e in expiring(within_days=3):
    when = "past use-by" if e["days_left"] < 0 else f"{e['days_left']}d left"
    print(f"    [!]   {e['name']:<28} {when} ({e['storage']}, keeps ~{e['shelf_life_days']}d)")

print("\n  WHAT SHOULD I ORDER?")
for o in order():
    print(f"    [!]   {o['name']:<28} {o['days_since_last_purchase']}d since last, "
          f"~{o['median_interval_days']:.0f}d cycle  (overshoot {o['overshoot']}x)")

print()
print(f"  Database: {server.DB_PATH}")
print("  Every number above is recomputable from get_events(). No magic.")

if "--serve" in sys.argv:
    from homestock import ui

    print()
    ui.main()

"""Seed a demo household (6 months of realistic shopping) and show what the
agent sees: the pantry reveal + what to order. Run: uv run python scripts/demo.py"""

import os
import sys
import tempfile
from datetime import date, timedelta

os.environ.setdefault("HOMESTOCK_DB", os.path.join(tempfile.mkdtemp(), "demo.db"))

from homestock import server  # noqa: E402

server.DB_PATH = os.environ["HOMESTOCK_DB"]
server.init_db()

fn = lambda t: t.fn if hasattr(t, "fn") else t
add_items, get_stock, order = fn(server.add_items), fn(server.get_stock), fn(server.what_should_i_order)

today = date.today()

# (name, unit, qty, cadence_days, days_since_last_purchase)
HOUSEHOLD = [
    ("semi-skimmed milk", "l", 2, 5, 6),
    ("eggs", "pack", 1, 9, 4),
    ("sourdough bread", "unit", 1, 6, 2),
    ("whole chicken", "kg", 1.4, 12, 13),
    ("toilet roll", "pack", 1, 24, 31),
    ("dishwasher tablets", "pack", 1, 45, 12),
    ("oat flat white oat milk", "l", 1, 7, 8),
    ("bin bags", "pack", 1, 40, 47),
    ("butter", "unit", 1, 14, 5),
    ("bananas", "kg", 1, 5, 1),
]

n = 0
for name, unit, qty, cadence, last in HOUSEHOLD:
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

print("\n  WHAT SHOULD I ORDER?")
for o in order():
    print(f"    [!]   {o['name']:<28} {o['days_since_last_purchase']}d since last, "
          f"~{o['median_interval_days']:.0f}d cycle  (overshoot {o['overshoot']}x)")

print()
print(f"  Database: {server.DB_PATH}")
print("  Every number above is recomputable from get_events(). No magic.")

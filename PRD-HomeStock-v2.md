# Product Requirements Document: HomeStock (v2 — Local Agent Connector)

**Status:** Draft v2.0 — supersedes v0.1 (consumer app framing)
**Date:** 10 August 2026

> Note: §8 (Data Flow) was revised post-draft in the 2026-08-10 design session —
> ingestion is now zero-touch (a scheduled agent watches receipt emails; manual drop
> is fallback only). See the approved design doc:
> `~/.gstack/projects/HomeStock/Thomas-unknown-design-20260810-094038.md`

---

## 1. Summary

HomeStock is a local-first database of what is physically in your home, exposed to AI agents via MCP. It runs entirely on the user's own machine — a SQLite file and a small MCP server — with no cloud backend, no accounts, and no inference costs.

The agent does the hard work: reading receipts, canonicalising item names, reasoning about what to reorder. HomeStock does the boring work: storing state, validating writes, answering queries. That division is the whole design.

## 2. What Changed From v1, And Why

v1 was a consumer app: receipt parsing pipelines, per-retailer templates, OCR, prediction engines, a recipe suggester, a pricing model to cover LLM costs.

Nearly all of that is now unnecessary. Agents can already parse receipts, canonicalise product names, estimate shelf life, and suggest recipes. Building those features means competing with the model, badly, at your own expense. The gap agents actually have is *state*: an agent can read your calendar, email, files and bank, but has no idea what is physically in your house.

v2 fills only that gap.

| v1 | v2 |
|---|---|
| Consumer app with its own UI | MCP server; the agent is the UI |
| Retailer-specific email/DOM parsers | No parsers — agent submits structured items |
| Hosted OCR + LLM parsing pipeline | Zero inference; user's own LLM does it |
| Prediction engines as core features | Agent reasons over stored data |
| Cloud database, accounts, auth | SQLite file; filesystem is the permission model |
| Pricing model to cover per-user costs | Near-zero running cost; open core later |

## 3. Problem Statement

Agents cannot act usefully on the physical household because no machine-readable record of it exists. Creating one manually is prohibitively tedious — the reason every fridge-tracking app fails is that inventory entry is work, and the work never ends.

The product must therefore produce a useful household record with as close to zero user effort as possible.

## 4. Principles

1. **The server is dumb on purpose.** Schema, validation, storage, query. No parsing, no inference, no business logic that a model does better.
2. **Never ask the user to inventory anything.** Stock is inferred from purchases; corrections are optional.
3. **Uncertainty is data, not failure.** Confidence is a first-class field, not something to hide behind a clean number.
4. **Costs nothing to run.** No infrastructure means no bill and no maintenance treadmill.
5. **Data stays on the user's machine** unless they explicitly choose otherwise.

## 5. Target User (v1: me)

Built first for the author's own household, against real weekly shopping. If it isn't useful to one person for a month, nothing else matters. Growth path: technical self-hosters → packaged installer → optional hosted sync tier.

## 6. Core Concepts

- **Location** — a place things are stored. `Home A / Fridge`, `Home A / Pantry`, `Home B / Freezer`. Hierarchical, user-defined.
- **Item** — a canonical thing (`whole chicken`, `semi-skimmed milk`). Named by the agent on write, not by a licensed product database.
- **Stock record** — `(location, item, quantity, unit, confidence, last_updated, source)`. The current best estimate.
- **Event** — an immutable log entry: bought, consumed, discarded, corrected. Stock is derived from events; events are never edited.
- **Confidence** — how much to trust the quantity, degraded by time since last observation and by how variable that item's usage is.

## 7. MCP Tool Surface

**Read**
- `get_inventory(location?, category?, low_stock_only?)` — current stock estimates with confidence
- `get_item_history(item, since?)` — purchase and consumption events for one item
- `get_consumption_rate(item)` — inferred usage rate and typical repurchase interval
- `get_expiring_soon(location?, within_days)` — perishables approaching estimated expiry
- `search_items(query)` — fuzzy lookup against known items

**Write**
- `add_items(location, items[], source, purchased_at?)` — record a purchase; the agent supplies canonicalised names, quantities, units, and optional shelf-life estimates
- `consume_item(item, location, quantity?)` — record usage, including "used the last of it"
- `discard_item(item, location, reason?)` — record waste (also a signal that shelf-life estimates are wrong)
- `correct_stock(item, location, quantity)` — user-observed truth; resets confidence to high
- `set_shelf_life(item, days, storage_type)` — agent-supplied or user-corrected default

**Admin**
- `create_location(name, parent?, storage_type)`
- `list_locations()`

## 8. Data Flow

**Adding stock (no parsers, ever)**
1. User drops a receipt — photo, PDF, forwarded email text — into their LLM client
2. The model reads it, canonicalises the line items, estimates shelf life where relevant
3. The model calls `add_items` with structured data
4. HomeStock validates and stores it

If a retailer changes their receipt format, nothing breaks, because nothing in HomeStock knows what a Tesco receipt looks like.

**Removing stock**
- Explicit: `consume_item` via conversation ("I just used the last of the milk")
- Implicit: repurchase of a consumable implies the previous unit was consumed
- Inferred: quantity decays toward zero over the item's modelled consumption interval, with confidence decaying alongside it

**The core insight:** consumption is never observed directly, but repurchase interval *is* the consumption rate. Someone rebuying milk every five days is telling you their usage without ever being asked. The system therefore models rates and treats current stock as a derived estimate, not a fact.

## 9. Cold Start

The product must be useful within minutes of install, with no data entry.

- **Retroactive backfill.** On setup, the user drops in whatever order history they can export or screenshot — the agent parses months of purchases in one session. Enough to model rates immediately.
- **Pre-filled, not blank.** From history alone, infer a plausible standing pantry (bought olive oil roughly every ten weeks, last bought three weeks ago → probably has olive oil). The user corrects, rather than fills.
- **Track the vital few.** High-turnover repeat purchases — milk, bread, eggs, chicken, loo roll — are most of the value and the easiest to model. The jar of cumin is explicitly out of scope.
- **Degrade honestly.** An answer of "probably two to four portions of chicken, bought Tuesday, use by Friday" is genuinely useful and far cheaper to produce than precision.

## 10. Technical Design

- **Storage:** single SQLite file. Portable, backed up by copying, no server process to babysit.
- **Server:** MCP server in Python or TypeScript, target ~300 lines for v1. Spawned locally by the LLM client.
- **Auth:** none. The filesystem is the permission model.
- **Network:** none required. Nothing leaves the machine except what the user's LLM client already transmits.
- **Schema:** append-only event log plus a derived stock view, so any estimate can be recomputed and explained.

## 11. MVP Scope

**In:**
- SQLite schema (locations, items, events, derived stock)
- MCP server with the read/write tools in §7
- Confidence decay over time
- Consumption-rate inference from repurchase intervals
- Manual setup via config file (author's own use)

**Out (deliberately):**
- Any receipt parser, OCR, or email integration
- Recipe suggestions, meal planning, reorder automation — all agent-side
- Multi-home sync, sharing, mobile access
- Accounts, auth, hosted anything
- Web UI

## 12. Success Criteria

**v1 is successful if the author still uses it after one month.** Specifically:
- Adding a weekly shop takes under a minute of interaction
- "What food do I have left?" returns an answer that matches reality closely enough to act on
- "What should I order?" catches at least one thing that would otherwise have been forgotten
- The database survives a month without manual repair

Growth metrics are deliberately deferred. There is no point measuring adoption of something not yet proven useful to one person.

## 13. Cost Model

Running cost is approximately a domain name. No inference, no hosting, no storage, no OCR, no email API. The user's existing LLM subscription does the expensive part.

The cost that matters is maintenance, and the parser-free design is what controls it: there is no retailer-specific code to rot.

## 14. Monetisation (Deferred)

Self-hosted software is hard to charge for directly, so v1 charges nothing. If it grows, open core is the natural fit: the local server stays free, and the paid layer covers the things that genuinely require a server —

- Sync between multiple homes
- Shared household inventory between partners
- Mobile access while at the shops

These are the differentiated features anyway, and the only ones where a hosted component is justifiable to a user who chose self-hosting.

## 15. Risks & Open Questions

| Risk | Notes |
|---|---|
| Estimates drift without ground truth | Mitigated by rate modelling and confidence decay, not eliminated. Needs real-use validation. |
| Installation friction | Editing JSON config is a wall for non-technical users. Packaging (Docker one-liner or bundled desktop app) is the growth unlock, not cloud. |
| Agents write inconsistent item names | Canonicalisation quality is outsourced to the model. May need a light alias table if duplicates proliferate. |
| Infrastructure is early | Demand for household state from agents may not arrive for a while. Being useful to one person now is the hedge. |
| No moat | The schema is copyable. The data is the asset, and it belongs to the user by design — which is the point, but means defensibility is thin. |

## 16. Open Decisions

- Python or TypeScript for the server
- Whether quantity decay should be linear or step-wise at the modelled repurchase interval
- How aggressively to auto-consume on repurchase versus waiting for explicit confirmation
- Whether to expose raw events to the agent, or only derived stock

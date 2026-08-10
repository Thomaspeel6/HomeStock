# HomeStock

**A database for AIs to use for people's houses.** Local-first household
inventory: one SQLite file, one small MCP server, zero cloud. Your AI agent
reads your shopping receipt emails and keeps a live estimate of what's in your
home — what you have, what's running low, what to order — with no manual entry,
ever.

The design bet: every pantry app dies because it treats inventory as facts that
must be kept correct. HomeStock treats inventory as a **probabilistic estimate**
derived from an append-only purchase log — "probably 2–4 portions of chicken,
bought Tuesday" — and expects drift instead of denying it. Repurchase interval
*is* consumption rate.

## Quickstart

```bash
git clone https://github.com/Thomaspeel6/HomeStock && cd HomeStock
uv run pytest -q            # 13 tests
uv run python -m homestock  # stdio MCP server (DB at ./homestock.db, or $HOMESTOCK_DB)
```

`.mcp.json` registers the server for Claude Code in this directory. For other
MCP clients, point them at `uv run python -m homestock`.

Then, in your agent: follow [`prompts/onboarding.md`](prompts/onboarding.md) —
consent, backfill your receipt history, get your pantry reveal. Scheduled
ingestion uses [`prompts/ingestion.md`](prompts/ingestion.md).

## Tools

| Tool | Purpose |
|---|---|
| `add_items(items[], source, source_ref, purchased_at?, location?)` | Record a receipt. Idempotent per `(source_ref, line_no)` — re-ingestion can never double-count. |
| `get_stock(item?)` | No arg: known items. With arg: estimate + raw provenance (`likely_in_stock/low/out/unknown`). |
| `what_should_i_order()` | Items past their median repurchase interval (≥3 purchases, ≤3× median). |
| `void_event(source_ref, line_no?)` | Corrections: void, then re-insert the fixed line. |
| `record_ingest_run(...)` | Ingestion heartbeat + backfill cursor. |
| `get_events(item?, since?)` | Raw event log — every estimate is explainable. |

## Privacy

Plain English, because this matters:

- **What HomeStock can see:** receipt emails from shops you approve — read-only,
  sender-filtered. Your agent never reads anything else for HomeStock.
- **Where your data lives:** one file, `homestock.db`, on your computer. Copy it
  to back it up. Delete it to erase everything. That's the whole model.
- **What leaves your machine: nothing.** The server makes no network calls — no
  telemetry, no accounts, no cloud. (Your AI client processes your conversations
  under its own privacy policy, exactly as it already does.)
- **When you need help:** run diagnostics locally and share them only if you
  choose. Diagnostic output contains version numbers and ingestion statistics,
  never email content.

## Retailer recipes (community)

The ingestion agent learns retailers from [`recipes/`](recipes/) — small YAML
files describing which email carries the real line items and how to read them.
**Data, never code. No parsers.** If your shop isn't covered, copy
[`recipes/TEMPLATE.md`](recipes/TEMPLATE.md) and open a PR — see
[CONTRIBUTING.md](CONTRIBUTING.md). Launch recipes: Tesco (GB), Amazon (GB).

## Project docs

- Product spec: [`PRD-HomeStock-v2.md`](PRD-HomeStock-v2.md)
- Deferred work: [`TODOS.md`](TODOS.md)
- Roadmap: milestone A (this repo) → B (onboarding + eval harness) →
  B′ (one-click bundle, background digest — gated on a 30-day unattended
  validation run)

## Status

Alpha, macOS-first, built for the author's own household first. If it isn't
useful to one person for a month, nothing else matters.

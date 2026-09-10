# HomeStock

Local-first household inventory. Reads shopping receipt emails and keeps a live
estimate of what's in a home — what you have, what's running low, what's about
to go off. No manual entry, no cloud, no accounts. One SQLite file the user owns.

Two surfaces over the same file: a **pantry window** for people
(`homestock/ui.py`) and an **MCP server** for agents (`homestock/server.py`).

**Read before making product or architecture decisions:**
[PRD-HomeStock-v3.md](PRD-HomeStock-v3.md) (current — the app),
then [PRD-HomeStock-v2.md](PRD-HomeStock-v2.md) (the engine's rationale).
`PRD-Home-Stock-Room.md` is v0.1 and **abandoned** — a cloud consumer app with
OCR pipelines and per-retailer parsers. Do not build from it.

## Status

Alpha, macOS-first. Engine and pantry window built and tested (31 tests, CI on
macOS / Python 3.11–3.13). The `.app` bundle, built-in email ingestion, and
PyPI release are specified in PRD v3 but **not built** — installation still
needs a terminal.

## Architecture

```
homestock.db  (SQLite, WAL)  <--  homestock/server.py   12 MCP tools, stdio
                             <--  homestock/ui.py       local HTTP, 127.0.0.1
```

Two processes writing one file is the intended topology. WAL + `busy_timeout`
makes writers queue instead of erroring — see `test_two_process_concurrent_writes`.

- **Event log is append-only.** Stock is *derived*, never stored. Events are
  voided (`voided = 1`), never edited or deleted.
- **Idempotency** is `(source_ref, line_no)` among live `bought` rows, so
  re-ingesting a receipt can never double-count.
- **Estimates** come from repurchase intervals: median interval vs. days since
  last observation. A user correction is an *observation* — it outranks the
  model and resets the clock.
- **Confidence** is the coefficient of variation over intervals. Surfaced, not
  hidden.

## Non-negotiables

1. **The server is dumb on purpose.** Schema, validation, storage, deterministic
   math. Parsing, naming and reasoning belong to the model. PRs adding
   retailer-specific parsing code are declined — that code is what rots.
2. **Never ask the user to inventory anything.** Stock is inferred from
   purchases; corrections are optional and always one tap.
3. **Uncertainty is data, not failure.** Show the reasoning behind every number.
4. **Nothing leaves the machine.** No telemetry, no accounts, no network calls.
   `get_health()` returns counts and dates only — never item names.
5. **Migrations are append-only.** `MIGRATIONS` in `homestock/server.py` is a
   public contract; strangers hold these database files. Never edit a shipped
   migration. Every schema change needs an upgrade-preserves-data test.

## Privacy & compliance

Grocery data is sensitive under UK/EU GDPR — it reveals health, religion,
household composition, addiction. The architecture is what makes the privacy
claim true, not a policy page. Email access stays read-only and
sender-filtered. Any use of this data beyond the app's core function would need
explicit, separate opt-in, never bundled into a ToS.

## Where the risk actually is

- **`recipes/amazon.yaml` is unverified** and flags that Amazon stripped line
  items from confirmation emails around 2023. If true, half the launch retailer
  coverage is zero. Verify before building anything on top.
- **No eval harness yet.** Recipes are hand-validated, which does not scale past
  a handful — and PRD v3's two ingestion paths make it blocking.
- **Item-name drift** is handled by `merge_items`, but canonicalisation quality
  is still outsourced to the model and determines whether estimates feel useful.
- **Silent ingestion failure** looks exactly like a quiet week. `get_health()`
  and the UI's stale banner exist for this; nothing must be allowed to hide it.

## Working in this repo

- `uv run pytest -q` must pass. New code paths need tests, including failure paths.
- `uv run python scripts/demo.py --serve` seeds a fake household and opens the UI.
- CI asserts the exact MCP tool list — adding a tool means updating
  `.github/workflows/ci.yml`.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool.
When in doubt, invoke the skill.

- Product ideas/brainstorming → /office-hours
- Strategy/scope → /plan-ceo-review
- Architecture → /plan-eng-review
- Design system/plan review → /design-consultation or /plan-design-review
- Full review pipeline → /autoplan
- Bugs/errors → /investigate
- QA/testing site behavior → /qa or /qa-only
- Code review/diff check → /review
- Visual polish → /design-review
- Ship/deploy/PR → /ship or /land-and-deploy
- Save progress → /context-save
- Resume context → /context-restore

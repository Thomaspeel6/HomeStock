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

Alpha, macOS-first. Engine, pantry window, and capture from phone or laptop
built and tested (57 tests; CI on macOS and Linux, Python 3.11–3.13). The `.app` bundle,
built-in email ingestion, and PyPI release are specified in PRD v3 but **not
built** — installation still needs a terminal.

## Architecture

```
homestock.db  (SQLite, WAL)  <--  homestock/server.py   19 tools + 2 prompts
                                                        + recipe resources, stdio
                             <--  homestock/ui.py       local HTTP + capture
                                                        127.0.0.1, or LAN if asked
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
- **Aliases** absorb the fact that four input paths name things four ways.
  Resolution happens on the way in (`_resolve`), so one item keeps one
  repurchase cycle. `merge_items` records an alias so drift cannot recur.
- **Captures are an inbox, not stock.** Capture must be instant and offline;
  reading needs a model. An agent works the queue and closes each one — an
  unreadable photo is skipped with a reason, never guessed at.

## Non-negotiables

1. **The server is dumb on purpose.** Schema, validation, storage, deterministic
   math. Parsing, naming and reasoning belong to the model. PRs adding
   retailer-specific parsing code are declined — that code is what rots.
2. **Never ask the user to inventory anything.** Stock is inferred from
   purchases; corrections are optional and always one tap.
3. **Uncertainty is data, not failure.** Show the reasoning behind every number.
4. **Nothing leaves the machine.** No telemetry, no accounts, no outbound calls.
   `get_health()` returns counts and dates only — never item names.
   LAN mode (`--lan`) is opt-in and never the default: it is the one place
   where a mistake exposes a household to its own network. Unpaired devices
   read nothing; pairing codes expire and burn after five wrong guesses (only
   failures count, under a lock — a racy counter is not a limit); the cookie
   proves *which device*, the header proves *which page*, and writes require
   the header. `TOKEN` and `DEVICE_SECRET` must stay **different values**: the
   separation is only real if presenting the cookie's value as the write header
   fails. Loopback is exempt from pairing (the laptop must be able to read its
   own pairing code) but never from the `Host` allowlist, which is what keeps
   DNS rebinding from making a public page same-origin with the window.
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
- `uv run ruff check .` must pass too; config is in `pyproject.toml`.
- CI asserts the exact MCP surface — tools, prompts and resource templates —
  so adding any of them means updating `.github/workflows/ci.yml`. It also
  builds the wheel and checks `prompts/` and `recipes/` are inside it; they are
  served over MCP, so a wheel without them is a broken install.
- **The UI must not load a webfont or any remote asset.** A request to a font
  CDN would leak that this household runs HomeStock, from a product whose
  headline claim is that nothing leaves the machine. System fonts only, no
  remote images, no CDN anything. The design gets its character from treatment:
  cards on a tinted ground, mono tabular figures, and colour spent only where
  something wants acting on — a row that is simply fine gets no marker at all.
  Both themes are defined as tokens on `:root`, redefined under
  `prefers-color-scheme: dark` and again under `[data-theme="dark"]`.
- **The window never recomputes the server's arithmetic.** `cycle_position` and
  `days_over` come from `_item_stats()`; the JavaScript formats them and
  nothing more. Two copies meant the page and an agent could disagree about the
  same item, and only one copy had tests.
- Public-facing docs a stranger reads first, in order: `README.md`,
  `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CHANGELOG.md`.
  Keep the README honest about what is *not* built — the gap between the idea
  and the code is where open-source projects usually mislead people.

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

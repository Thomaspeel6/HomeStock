# Contributing

Thanks for looking. This is a small, opinionated project, so it's worth knowing
what it wants before you spend an evening on it.

Everyone taking part is expected to follow the
[code of conduct](CODE_OF_CONDUCT.md). One rule from it applies to almost every
contribution here: **redact before you post.** No names, addresses, card
digits, order numbers or account identifiers — in an issue, a recipe example, a
test fixture, or a screenshot.

## The most valuable contribution: a retailer recipe

Coverage for a shop we can't read yet. A recipe is a small YAML file describing
which email carries the real line items and how to read it. **Data, never code.
No parsers.**

1. Copy `recipes/TEMPLATE.md` to `recipes/<retailer>.yaml` and fill it in.
2. **Redact your example.** Three to five item lines and the order-id line is
   plenty.
3. The single most important field is `canonical_email`. For a grocer that
   substitutes items it must be the email reflecting what was actually
   *delivered*, not what was ordered — otherwise the inventory records food
   that never arrived.
4. Open a PR.

Don't want to write YAML? [Open an issue with a redacted
sample](../../issues/new?template=retailer.yml) instead — that's most of the
work, and someone else can finish it.

Until the eval harness lands (see [TODOS.md](TODOS.md)), a maintainer validates
recipes by hand against real receipts. After that, recipes ship with example
receipts and expected extractions, scored automatically.

## Getting set up

```bash
git clone https://github.com/Thomaspeel6/HomeStock && cd HomeStock
uv run pytest -q                          # 81 tests
uv run ruff check .                        # lint
uv run python scripts/demo.py --serve      # a fake household, in the real UI
```

Both must be green before you open a PR; CI runs them on macOS and Linux across
Python 3.11–3.13.

Useful while working:

```bash
uv run homestock-ui              # the pantry window, loopback only
uv run homestock-ui --lan        # ...also reachable from a phone
uv run homestock                 # the MCP server on stdio
HOMESTOCK_DB=/tmp/scratch.db uv run homestock-ui    # don't touch your real kitchen
```

## How the thing is arranged

Read [CLAUDE.md](CLAUDE.md) first — it's the architecture in one page. Then
[PRD-HomeStock-v3.md](PRD-HomeStock-v3.md) for what the product is trying to be
and what is deliberately not built.

```
homestock/server.py    the MCP server: schema, 19 tools, 2 prompts, resources
homestock/ui.py        the pantry window and the phone capture page
prompts/               procedures an agent follows (served over MCP)
recipes/               retailer recipes (served over MCP)
tests/                 test_server.py and test_ui.py
```

## House rules

These aren't style preferences; each one is load-bearing.

1. **The server stays dumb on purpose.** Schema, validation, storage,
   deterministic maths. Parsing, naming and reasoning belong to the model. PRs
   adding retailer-specific parsing code will be declined with love — that code
   is exactly what rots when a retailer changes a template, and avoiding it is
   the whole maintenance argument.
2. **Never ask the user to inventory anything.** Stock is inferred from
   purchases. Corrections are optional and always one click.
3. **Uncertainty is data, not failure.** If you add a number to the UI, add the
   sentence that justifies it. An estimate nobody can audit is worth nothing.
4. **Nothing leaves the machine.** No telemetry, no accounts, no outbound
   requests — and that includes webfonts and CDN assets in the UI. A request to
   a font CDN would leak that a household runs HomeStock, from a product whose
   headline claim is the opposite.
5. **Migrations are append-only.** `MIGRATIONS` in `homestock/server.py` is a
   public contract, because strangers hold these database files. Never edit a
   shipped migration; append a new one, and add a test proving an older
   database upgrades with its rows intact (see
   `test_v1_database_upgrades_to_latest_preserving_data`).
6. **New code paths need tests, including the failure paths.** The interesting
   tests here are the ones asserting something is *refused*.
7. **Adding an MCP tool, prompt or resource means updating
   `.github/workflows/ci.yml`**, which asserts the exact surface. An agent's
   instructions are written against those names, so a rename is a breaking
   change.

## Good first issues

- A retailer recipe for a shop you use.
- **Verify the Amazon recipe.** `recipes/amazon.yaml` is marked `provisional`
  with a note that Amazon stripped itemised details from confirmation emails
  around 2023. Nobody has confirmed this against a real 2026 inbox, and half
  the launch coverage depends on the answer. One receipt settles it.
- Anything in [TODOS.md](TODOS.md) marked **S** for effort.

## Opening a PR

Keep it to one thing. Explain what was wrong and how you know it's fixed —
"tests pass" is the floor, not the answer. The
[PR template](.github/PULL_REQUEST_TEMPLATE.md) has the checklist.

If you're unsure whether an idea fits, open an issue first and ask. Better than
finding out after you've written it.

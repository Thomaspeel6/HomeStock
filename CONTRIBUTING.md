# Contributing

The most valuable contribution is a **retailer recipe** — coverage for a shop
we don't read yet.

## Adding a recipe

1. Copy `recipes/TEMPLATE.md` into `recipes/<retailer>.yaml` and fill it in.
2. **Redact your example.** No names, addresses, or payment details — 3-5 item
   lines and the order-id line are enough.
3. The single most important field: `canonical_email`. For grocers that
   substitute items, it must be the email reflecting what was *delivered*.
4. Open a PR. CI runs the test suite on every PR; a maintainer validates the
   recipe by hand against real receipts until the eval harness ships
   (milestone B), after which recipes ship with example receipts and expected
   extractions that are scored automatically.

## Code contributions

- `uv run pytest -q` must pass. New code paths need tests — including the
  failure paths.
- The server stays dumb on purpose: schema, validation, storage, deterministic
  math. Parsing, naming, and reasoning belong to the agent. PRs that add
  retailer-specific parsing code will be declined with love.
- Schema changes are **append-only migrations** in `homestock/server.py`
  (`MIGRATIONS`) — never edit a shipped migration; strangers hold these
  database files.

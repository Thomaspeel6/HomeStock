## What this changes

<!-- One or two sentences. What was wrong or missing, and what now happens. -->

## How you know it works

<!-- `uv run pytest -q` passing is the floor, not the answer. For a bug fix,
     what reproduces it before and passes after? For a recipe, which real
     receipts did you check it against? -->

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uvx ruff check .` passes
- [ ] New code paths have tests, including the failure paths
- [ ] No real personal data anywhere in the diff — no names, addresses, card
      digits, order numbers or account identifiers, in code, tests, fixtures
      or screenshots
- [ ] If this touches the schema: a new entry appended to `MIGRATIONS`, no
      edits to a shipped one, and a test proving an older database upgrades
      with its rows intact
- [ ] If this adds an MCP tool, prompt or resource: `.github/workflows/ci.yml`
      updated, since CI asserts the exact surface
- [ ] If this touches the UI: no webfonts, no CDN assets, no outbound requests
      of any kind

## Anything you're unsure about

<!-- Genuinely useful. Say where you'd want a second opinion. -->

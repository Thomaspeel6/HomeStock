# Changelog

Notable changes to HomeStock. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [semver](https://semver.org/spec/v2.0.0.html).

Database migrations are **append-only** and every release upgrades older
`homestock.db` files in place. A version that could not do that would be
called out here in capital letters.

## [Unreleased]

### Added

- **A pantry window.** `homestock-ui` serves a local page showing what to buy,
  what to use soon, and what is probably in the house — each row explaining
  the reasoning behind its guess. Corrections are one click.
- **Capture from a phone.** `homestock-ui --lan` pairs a phone over your own
  wifi with a six-digit code, so you can photograph a paper receipt, scan a
  barcode, or type what you bought. Off by default; see [SECURITY.md](SECURITY.md).
- **Four input paths.** Receipt email, photographed paper receipt, barcode and
  typed note, all landing in an inbox for an agent to read
  (`add_capture`, `list_captures`, `resolve_capture`).
- **Ground truth.** `correct_stock` (0 means "we're out") and `discard_item`.
  A correction outranks the interval model and resets its clock — previously
  there was nowhere for "we're out of milk" to go.
- **Perishables.** `set_shelf_life` and `get_expiring_soon`.
- **Cooking.** `check_recipe` sorts ingredients into have / low / missing;
  `consume_items` records the cooking and, with `finished: true`, that there
  is none left.
- **Aliases.** `add_alias` / `list_aliases`, and `merge_items` now records one
  automatically, so four input paths converge on one item instead of four.
- **Diagnostics.** `get_health` — counts and dates only, safe to paste into an
  issue. Reports `stale` when no receipts have been read for ten days.
- **Confidence** on every estimate, from the coefficient of variation over
  repurchase intervals.
- **Prompts and resources over MCP.** `onboarding` and `ingestion` as prompts;
  `homestock://recipes/{retailer}` as resources. An agent no longer needs
  filesystem access in the right directory to run ingestion.

### Changed

- The window was redesigned around a ledger treatment: monospaced tabular
  figures, ruled sections, and colour spent only where something needs
  attention. The shopping list gained a gauge showing where an item sits in
  its own repurchase cycle.
- `what_should_i_order` now surfaces anything you said you were out of, ranked
  above inferred items, on as little as one prior purchase.
- `get_expiring_soon` stops listing things expired longer ago than their own
  shelf life — a chicken ten days past a three-day use-by was dealt with a
  week ago.
- The wheel now ships `prompts/` and `recipes/`, which it previously did not;
  an installed copy had neither.

### Fixed

- **Binning something now marks it gone**, not merely wasted, so the row leaves
  "Use soon" instead of nagging about food already in the bin.
- **A pairing cookie can no longer authorise a write.** Cookies ride along with
  cross-site requests; only the custom header proves the request came from our
  own page. Reads accept the cookie, writes require the header.
- The pages now ship a doctype and viewport meta. Without it a phone laid them
  out at 980px and zoomed out — on the one device the capture page exists for.

## [0.1.0] — 2026-08-10

Initial release. Six MCP tools over an append-only SQLite event log: record a
receipt, read a stock estimate, get a shopping list from repurchase intervals,
void a mis-read line, log an ingestion run, read the raw event log.

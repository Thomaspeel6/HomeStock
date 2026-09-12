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

### Security

- **The pairing cookie is no longer the write token.** They were the same
  string, so the documented rule — the cookie says which device, the header
  says which page — was a comment rather than a mechanism, and anything that
  leaked a "read-only" cookie leaked full write access. Two independent
  secrets now, and the cookie is `HttpOnly`.
- **Requests must arrive under a `Host` we recognise.** Without that check the
  loopback window was open to DNS rebinding: a page on the public web whose
  name resolves to `127.0.0.1` is same-origin with us, so it could read the
  whole grocery history and lift the write token out of the page it was
  allowed to fetch. The custom-header rule only ever stopped *cross*-origin
  callers.
- **Five wrong guesses now actually burn the code.** The counter was an
  unlocked read-modify-write under a threading server, so concurrent guesses
  lost increments and bought themselves extra tries against a six-digit
  secret. It is locked, the boundary is exact, and only failures count — a
  correct code no longer burns an attempt, so the second phone in a household
  can still pair.
- **Hostile credentials no longer crash the handler.** `compare_digest` raises
  on non-ASCII text, and three call sites were fed raw header and body bytes,
  so one high byte would take down any route before authentication.
- Loopback is exempt from pairing, so LAN mode no longer locks the laptop out
  of the page displaying its own pairing code.

### Fixed

- **Migrations commit atomically and one process at a time.** The rebuilds
  `DROP` the only table holding a user's history; a crash between that and the
  version bump left a database no later version could open, and two processes
  starting together could both apply them. Verified by tests that kill a
  migration mid-rebuild and that race three processes at one file.
- **A database written by a newer release is refused loudly** instead of being
  opened by an older build that reads corrections as purchases.
- **`purchased_at` is required for `photo` and `loyalty` too.** Only `manual`
  and `barcode` — the doors a person walks through at the moment of buying —
  may default to today. A photographed till receipt or a three-year loyalty
  export dated today silently corrupts the repurchase intervals every estimate
  rests on.
- **`get_events` paginates** (default 200, cap 1000) and validates `since`. A
  four-year log returned 168k rows — 35 MB of JSON — into the agent's context
  on a single call.
- The recipe-name check anchors with `\Z`, not `$`, which in Python also
  matches before a trailing newline — on a value interpolated into a path.
- The window no longer promises that deleting one file erases everything; the
  photographed receipts live beside it in `captures/`.
- The test suite derives its dates in UTC, as the server does. It failed for
  any contributor behind UTC, and for the author between midnight and 01:00.

### Also fixed

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

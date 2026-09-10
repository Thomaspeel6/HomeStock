# TODOS

Deferred work with context. Source of truth for "later" — if it isn't here, it doesn't exist.

## P0 — Verify the Amazon recipe against a real inbox
**What:** Confirm whether Amazon GB confirmation emails still carry itemised line items.
**Why:** `recipes/amazon.yaml` is `status: provisional` with a KNOWN RISK that Amazon stripped
item details from confirmation emails around 2023. If that holds, half the launch retailer
coverage is zero and no amount of app polish fixes it.
**Context:** Blocks PRD v3 §8. Cheapest possible test: open one real Amazon receipt.
**Effort:** S. **Depends on:** nothing — do this first.

## P0 — Answer the validation question
**What:** Did HomeStock actually run for a month on the author's own shopping? `get_health()`
now reports it: `days_since_ingest`, `receipt_lines`, `stale`.
**Why:** PRD v2 §12 made this the gate for everything. It came due 2026-09-10 and was never measured.
**Effort:** S. **Depends on:** nothing.

## P1 — Built-in ingestion (IMAP first, then Gmail OAuth)
**What:** Code that fetches receipt mail on a schedule, per PRD v3 §6 — the app owns OAuth,
sender filter, fetching, cursor and skip logging; the model still reads the receipt.
**Why:** The prompt-driven loop is instructions, not software. It cannot serve a non-technical user.
**Context:** IMAP app-password rail first — it ships without anyone's approval. Gmail read-only
scope is restricted and needs a Google security assessment for public distribution.
**Effort:** L. **Depends on:** eval harness (two paths, no scoring, is how both rot).

## P1 — Eval harness (milestone B)
**What:** Recipes ship with example receipts and expected extractions, scored automatically.
**Why:** Was a nice-to-have; PRD v3's two ingestion paths make it blocking. Hand-validation by a
maintainer does not survive past a handful of recipes, and recipes are the one contribution asked for.
**Effort:** M. **Depends on:** nothing.

## P1 — macOS .app bundle
**What:** Signed, notarised, drag-to-Applications; bundled Python; WebView window; launchd
scheduling; auto-registration as an MCP server with Claude Desktop/Code.
**Why:** PRD v3's whole premise. Installation friction is the growth unlock, not cloud.
**Context:** Browser-tab fallback is acceptable for the first build, not for shipping — a product
that opens in a browser tab reads as a website.
**Effort:** L. **Depends on:** built-in ingestion (an app that cannot fetch mail has nothing to schedule).

## P2 — PyPI release
**What:** Tag v0.2.0 so `release.yml` fires; developer install becomes `uvx homestock-mcp`.
**Why:** The workflow is written and has never run. Cheapest adoption unlock in the repo.
**Effort:** S.

## P2 — Spec-first protocol play
**What:** Publish the HomeStock schema + tool surface as an open "household-state" spec with HomeStock as the reference implementation.
**Why:** Claims the "author of the standard" position; turns thin defensibility into deliberate openness.
**Context:** Deferred from the 2026-08-10 CEO review (EXPANSION mode). Premature at zero third-party users — standards follow traction. Revisit when strangers run HomeStock.
**Effort:** M (mostly writing + evangelism). **Depends on:** milestone B′ shipped, real external users.

## P2 — Multi-home / shared-household sync
**What:** Two machines, one household inventory (partners sharing a pantry; the founder + parents).
**Why:** The differentiated feature that genuinely needs a server; the open-core candidate from PRD-HomeStock-v2.md §14.
**Context:** Naive SQLite-over-file-sync risks corruption; needs a real design (event-log merge is the natural shape — append-only logs merge well). Unchanged from PRD deferral.
**Effort:** L. **Depends on:** stable schema (see migration-policy question in the CEO plan).

## P2 — Email providers beyond Gmail
**What:** Outlook/iCloud/IMAP provider matrix for the ingestion loop.
**Why:** Parents-grade audience won't all be on Gmail.
**Context:** Gmail only at launch (CEO plan, 2026-08-10). The IMAP app-password rail built as the Google plan-B partially covers generic IMAP already.
**Effort:** M. **Depends on:** first-run consent UX (B′).

## P3 — Windows support
**What:** Background execution + bundle packaging for Windows.
**Why:** Non-technical audience is heavily Windows.
**Context:** v1 is macOS-only by explicit decision (CEO plan, 2026-08-10) — founder's and parents' machines are Macs. Q1's background-execution answer is macOS-specific (launchd); Windows needs its own (Task Scheduler).
**Effort:** M. **Depends on:** Q1 resolution on macOS first.

## P3 — Weekly digest, if cut at the gate
**What:** Background ingestion + weekly "running low" notification.
**Why:** The product talks first.
**Context:** ACCEPTED CONDITIONAL in the CEO plan; lands here only if background execution (Q1) has no cheap resolution by the ~2026-09-15 gate, in which case catch-up-on-use ships as v1 behavior.
**Effort:** L.

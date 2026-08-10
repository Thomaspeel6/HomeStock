# TODOS

Deferred work with context. Source of truth for "later" — if it isn't here, it doesn't exist.

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

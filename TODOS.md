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

## P1 — Migrate to the MCP SDK v2
**What:** `mcp` 2.x renames FastMCP to MCPServer (`from mcp.server.mcpserver import MCPServer`)
and changes other APIs. `pyproject.toml` pins `mcp>=1.26,<2` to keep v1 running.
**Why:** v1 will not be maintained forever, and the pin is what stands between this repo and
every future SDK fix.
**Context:** Attempted during the 2026-09-12 maintenance pass and backed out: it is a real
migration, not a version bump. Every `@mcp.tool()` decorator, the `.fn` unwrapping that
`homestock/ui.py` and both test files rely on to call tools directly, the two prompts and the
resource template all need checking against the v2 API. Migration guide:
https://py.sdk.modelcontextprotocol.io/v2/migration/
**Effort:** M. **Depends on:** nothing — but it wants its own PR and a full CI run, since the
MCP surface assertion in `ci.yml` is the only thing proving the tool surface survived.

## P1 — Port the engine to TypeScript
**What:** Move the whole engine — migrations, estimate maths, confidence, idempotency, the
chat layer and the local HTTP server — from Python to TypeScript. The Mac app then bundles
Node instead of Python, and the MCP server ships as `npx homestock-mcp`.
**Why:** Every packaging problem this project has hit was Python distribution, not Python.
macOS ships 3.9 and the engine needs 3.11+; `pydantic_core` is a compiled extension whose
wheel is built per interpreter version, which is what made the app bundle 50MB and what caused
the 2026-09-12 launch bug; the `mcp` 2.x bump breaks at import. `node:sqlite` is built into
Node 22.5+ (verified on Node 24), so a TS port has **zero native dependencies** and that whole
class of bug disappears. MCP's TypeScript SDK is also the reference implementation and has the
better-trodden path for the remote-HTTP transport the Claude connector needs.
**Context:** Decided 2026-09-12. It is all-or-nothing: porting only the tool surface would
leave the arithmetic in Python and the tools in TS — two implementations of the same maths,
which is the bug fixed twice already (the browser recomputing the repurchase cycle, and a third
copy of the threshold in CSS) and what non-negotiable 1 in CLAUDE.md forbids.
The **105 tests are the specification**: they are written as behaviours ("a crash mid-rebuild
leaves the database recoverable", "the cookie value is not accepted as the write header"), so
porting against them is far safer than a blind rewrite. Do not lose the subtle work: migration
atomicity under BEGIN IMMEDIATE, the pairing lock, the Host allowlist, chat write-undo.
**Context for the app:** the Swift UI talks only to the local HTTP JSON API, so it is
unaffected as long as the port keeps that contract. Keep `/api/state`, `/api/action`,
`/api/capture`, `/api/chat`, `/api/models`, `/api/providers`, `/api/pairing`, `/api/undo` and
the handshake line identical.
**Effort:** XL — the largest single piece of work proposed so far, bigger than the Mac app.

## P1 — Sparkle auto-update for the Mac app
**What:** Wire Sparkle into HomeStock.app so installed copies update themselves, and have
`scripts/release.sh` sign each DMG and regenerate `appcast.xml`.
**Why:** Without it every update is a manual re-download, and the Gatekeeper first-launch
dance repeats each time. Sparkle removes the quarantine flag on updates it installs, so users
do it exactly once.
**Context:** Attempted 2026-09-12 and backed out. Sparkle via SwiftPM compiles the entire
framework from source — over ten minutes added to every build for everyone — and the framework
then needs embedding by hand into a bundle assembled by `app/build.sh`, plus an `@rpath` entry.
Options: use Sparkle's prebuilt XCFramework release rather than the source package, or move the
app to an Xcode project like JoeBro (which has this working). The release side is already
prepared: `release.sh` looks for `.sparkle-tools/` and writes the appcast when present.
An EdDSA signing key still needs generating, and **backing up** — lose it and existing installs
can never accept another update.
**Effort:** M. **Depends on:** nothing.

## P1 — Loyalty-export import (Clubcard / Nectar)
**What:** Import an itemised purchase history from a UK GDPR data request.
**Why:** Probably the fastest route from empty to useful — years of history in one file,
legally clean, user-initiated, no scraping and no OAuth. Directly attacks the cold-start
problem that PRD v2 §9 calls the make-or-break.
**Context:** Shape is unknown until someone actually requests theirs; likely CSV. The model
maps columns and calls add_items with source='loyalty', which the schema already accepts.
**Effort:** M. **Depends on:** one real export to look at.

## P2 — Barcode product lookup (Open Food Facts)
**What:** Turn a scanned barcode into a product name and pack size locally.
**Why:** Today a barcode reaches the inbox as bare digits for the model to name. Works, but a
cached OFF dump would make it exact and offline.
**Context:** Deliberately deferred in PRD v3 §7 — adds a dependency for a marginal gain.
**Effort:** M.

## P2 — HTTPS for LAN mode
**What:** Self-signed certificate so phone capture is not plain HTTP.
**Why:** Anyone already on the wifi who can sniff traffic can read a capture in flight.
**Context:** Accepted limitation in PRD v3 §6b — a trust prompt is worse onboarding for a
threat most home networks do not face. Revisit for shared or workplace networks.
**Effort:** M.

## P1 — Built-in ingestion (IMAP first, then Gmail OAuth)
**What:** Code that fetches receipt mail on a schedule, per PRD v3 §6 — the app owns OAuth,
sender filter, fetching, cursor and skip logging; the model still reads the receipt.
**Why:** The prompt-driven loop is instructions, not software. It cannot serve a non-technical user.
**Context:** IMAP app-password rail first — it ships without anyone's approval. Gmail read-only
scope is restricted and needs a Google security assessment for public distribution.
**Effort:** L. **Depends on:** eval harness (two paths, no scoring, is how both rot).

## P2 — A small model fine-tuned for these tools
**What:** LoRA fine-tune a 3–4B model on HomeStock's tool surface, ship it as GGUF on
Ollama/HuggingFace, and let the app offer it as a download.
**Why:** Observed 2026-09-12 — asked "what am I about to waste?", llama3.2:3b called
`discard_item`, a destructive tool, and narrated a tool call in its reply text. gemma4:e4b
handled the same question correctly. Tool use is where small models fail, and it is the whole
interaction model here, so a model tuned for it is the difference between the local option
being usable and being a demo.
**Context:** The tools are *deterministic*, so training traces can be auto-verified by
executing them and checking the result — most fine-tuning datasets cannot be. That makes the
data cheap and honest. Pick a base whose licence permits redistribution: Qwen2.5 is Apache 2.0
and clean; Llama 3.2's community licence has attribution and naming rules; Gemma has its own
terms. Worth choosing deliberately, having just moved to FSL to control redistribution.
**Effort:** L. **Depends on:** the eval harness — you cannot tell whether a fine-tune helped
without measuring, and the eval doubles as the trace verifier.

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

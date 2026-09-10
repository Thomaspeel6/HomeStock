# Product Requirements Document: HomeStock (v3 — The App)

**Status:** Draft v3.0 — extends v2, supersedes its "the agent is the UI" decision
**Date:** 10 September 2026

---

## 1. Summary

HomeStock tells you what food and household supplies you have, what you need to
buy, and what to eat before it goes off — built automatically from your shopping
receipt emails, with nothing typed in and nothing uploaded.

v2 built the correct engine and shipped it as an MCP server. v3 puts a window on
it and an installer around it, so the audience stops being "people who can
configure an MCP client" and starts being "people who buy groceries."

## 2. What Changed From v2, And Why

v2's bet was that the agent is the whole interface: the server stays dumb, the
model does the reasoning, and the user talks to their LLM. For one technical
user with Claude already configured, that was right, and it is why the engine is
only ~350 lines.

It does not survive contact with everyone else, for two reasons:

1. **Most people have no MCP client.** The interface cannot be a thing you have
   to install a developer tool to reach.
2. **The ingestion loop was a markdown file.** `prompts/ingestion.md` describes
   a procedure an agent is supposed to follow, on a schedule, with mail access.
   That is a set of instructions, not software. It cannot be relied on by
   someone who does not know what an agent is.

| v2 | v3 |
|---|---|
| The agent is the only interface | An app window; the agent is a second interface |
| Ingestion is a prompt an agent follows | Ingestion is code the app owns, with the prompt path kept |
| `git clone` + `uv` | Double-click a `.app` |
| No email integration at all (§11) | Built-in Gmail, read-only, sender-filtered |
| Correcting the record means editing receipts | Correcting reality is one tap |

**What does not change, and is not negotiable:** no retailer parsers, no cloud,
no accounts, no telemetry, one SQLite file the user owns. Those are what keep
the maintenance cost near zero and the privacy claim honest. v3 walks back v2's
*integration* purity, not its *architecture* purity.

## 3. Positioning

The v2 README opened with "a database for AIs to use for people's houses." That
is an accurate developer sentence for a category nobody shops for. Nobody wants
a household database.

**The promise:** *Never run out, never throw food away. HomeStock reads your
shopping receipts and keeps track for you.*

**The differentiator, stated plainly:** every competitor in this category is a
cloud app whose business model is your shopping data. HomeStock's data never
leaves your computer. In a category this sensitive — grocery history reveals
health, religion, household composition, addiction — that is not a footnote,
it is the product's strongest claim, and it is only credible because the
architecture makes it structurally true rather than a policy promise.

## 4. Two Surfaces, One File

```
              homestock.db  (SQLite, WAL, on the user's disk)
                    |
        +-----------+-----------+
        |                       |
   HomeStock.app           MCP server
   (a person looks         (an agent calls
    at their kitchen)       12 tools)
```

Both are already proven to coexist: WAL plus `busy_timeout` makes concurrent
writers queue rather than error, covered by `test_two_process_concurrent_writes`.

Neither surface is subordinate. A user who never installs an AI client gets a
complete product. A user who has Claude gets conversation on top of the same
file — "what's for dinner with what I've got?" — which is where the ceiling is
much higher than any UI we would build.

## 5. The App

### 5.1 The window

One scrolling page, three lists, no navigation, no tabs, no settings buried
anywhere. See `docs/img/pantry-window.png`.

- **Buy these** — the shopping list. Items past their usual repurchase cycle,
  plus anything the user said they were out of (ranked first: an observation
  beats an estimate). One action: *Already have it*.
- **Use soon** — perishables at or near their estimated use-by. Actions:
  *Used it* / *Binned it*. Binned is not just bookkeeping; it is the only
  signal that a shelf-life estimate was too generous.
- **Probably in the house** — everything else, with the reasoning shown in
  plain words ("bought 5d ago, usually every 14d").

Design rules:
- **Every row explains itself.** No number appears without the sentence that
  justifies it. The product's core claim is that its guesses are worth acting
  on; a guess you cannot audit is worth nothing.
- **Low-confidence rows are visually quieter**, not hidden. Uncertainty is data.
- **A stale banner is louder than any estimate.** Ingestion silently failing
  looks exactly like a quiet week. If receipts have not been read in 10 days,
  say so above everything else.
- **Nothing nags forever.** An item ten days past a three-day use-by was dealt
  with a week ago; a list that keeps mentioning it stops being read.

### 5.2 First run

The install *is* the onboarding — there is no separate setup screen.

1. Double-click. The app opens on an empty kitchen that explains itself.
2. **Consent, in plain English, before any mail is touched:** exactly which
   retailers, read-only, how far back, and that nothing is uploaded. A clear
   yes is required; silence is not consent. Declining leaves a working app —
   the manual path (photograph a receipt) still works.
3. Backfill runs visibly, month by month.
4. **The reveal** — the moment the product earns its place. Three or four true,
   specific facts from their own history ("you buy milk every 5 days", "47
   packs of toilet roll this year", "Tuesday is your shop day"), then the
   pantry, then one actionable line. This is the screenshot people send to
   other people, and it is the only marketing the product gets.

`prompts/onboarding.md` already scripts these beats for the agent path; the app
performs the same script in code.

### 5.3 Packaging (macOS first)

- `.app` bundle, signed and notarised, drag-to-Applications.
- Ships its own Python — the user never sees `uv`, `pip`, or a terminal.
- The window is the system WebView pointed at the local UI server. A browser
  tab is the acceptable fallback for the first build; it is not the shipping
  answer, because a product that opens in a browser tab reads as a website, and
  a website is exactly what we are promising this is not.
- Background ingestion via `launchd`, hourly, with catch-up on wake.
- **Auto-registers itself as an MCP server** with Claude Desktop and Claude Code
  if either is present. This is the bridge between the two surfaces and it must
  require zero JSON editing.

Windows is deferred (TODOS P3) and is a real gap for a non-technical audience —
`launchd` has no Windows equivalent and the whole background story needs
rebuilding on Task Scheduler.

## 6. Ingestion: Both Paths, User's Choice

| | Built-in (app) | Agent-driven (prompt) |
|---|---|---|
| Who fetches mail | HomeStock, via Gmail OAuth | The user's AI client |
| Who reads the receipt | An LLM | An LLM |
| Who calls the tools | HomeStock | The agent |
| Schedule | `launchd`, hourly | Whenever the agent runs |
| Audience | Everyone | MCP users, non-Gmail mail, tinkerers |

Both paths write through the same tools, hit the same idempotency key
(`source_ref`, `line_no`), and record the same heartbeat, so they cannot corrupt
each other and a user can switch or run both.

**What the app owns in code:** OAuth, the sender-domain filter, fetching,
scheduling, the backfill cursor, and skip logging. **What stays with the model:**
reading the email body and returning line items. That division is the whole
maintenance argument — retailers change their templates constantly, and there is
no retailer-specific code to rot on either path.

**Cost:** two paths to keep correct with no eval harness yet. This makes the
eval harness (§8) a dependency of v3, not a nice-to-have.

**Gmail API verification** is a real, unbudgeted obstacle: read-only Gmail scope
is restricted, and Google requires a security assessment for public
distribution. The IMAP app-password rail (already noted in TODOS as the Google
plan-B) is the fallback and should be built first, because it needs no approval
from anyone.

## 7. What v3 Ships

**In:**
- The pantry window: three lists, one-tap corrections, stale warning *(built)*
- Ground truth in the schema: `correct_stock`, `discard_item` *(built)*
- Perishables: `set_shelf_life`, `get_expiring_soon` *(built)*
- Confidence from interval variance, surfaced in the UI *(built)*
- `merge_items` for name drift, `get_health` for diagnostics *(built)*
- Built-in ingestion (IMAP first, Gmail OAuth second)
- `.app` bundle, notarised, with `launchd` scheduling and MCP auto-registration
- The first-run reveal, in code
- PyPI release so the developer path is `uvx`, not `git clone`

**Out:**
- Windows (TODOS P3)
- Multi-home sync and shared households (TODOS P2) — still the most expensive
  item on the roadmap and still in tension with the zero-infrastructure
  principle. Mobile read-only access while standing in a shop is probably worth
  more to more people, for far less.
- Recipe suggestion — belongs to the agent, which does it better than we would
- Any hosted component, account, or telemetry

## 8. Dependencies This Creates

1. **The eval harness (milestone B) becomes blocking.** Two ingestion paths and
   community recipes cannot both be correct on a maintainer's hand-checking.
2. **The Amazon recipe must be verified.** It is still `status: provisional`
   with a `KNOWN RISK` that Amazon stripped line items from confirmation emails
   around 2023. If that holds, half the launch retailer coverage is zero, and no
   amount of app polish fixes it. Verify before building anything else.
3. **Schema stability.** Third parties will hold `homestock.db` files. The
   append-only migration rule and its regression test
   (`test_v1_database_upgrades_to_v2_preserving_data`) are now load-bearing.

## 9. Success Criteria

v2's criterion — the author still uses it after a month — was never measured;
`get_health()` now makes it measurable, and that measurement is the gate for
everything in §7 that is not yet built.

v3 succeeds if a non-technical person who has never heard of MCP installs it,
connects their mail, and is still opening it a month later. Concretely:

- Install to first pantry view in under five minutes, no terminal
- The reveal produces at least one fact the user did not know about themselves
- The shopping list catches something they would have forgotten, in week one
- Nothing in the window is ever wrong in a way the user cannot correct in one tap

## 10. Open Decisions

- IMAP-first or wait for Gmail verification (recommend IMAP-first: it ships)
- WebView library versus browser fallback for the first notarised build
- Whether the reveal is generated by a bundled model call or purely by SQL over
  the user's history (SQL is free, offline, and probably good enough)
- Whether "pantry" is the right frame at all: the schema is not food-specific,
  and printer ink, dog worming tablets and lightbulbs are the same problem

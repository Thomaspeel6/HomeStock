# HomeStock

Automatic household inventory system. Builds a live picture of what a household owns
(groceries, toiletries, household goods) by parsing receipts from grocery deliveries and
online orders — not manual entry. Predicts when items run low or expire, supports multiple
homes/locations per user, and powers downstream features like recipe suggestions and
auto-reordering.

**Full product spec:** [PRD-Home-Stock-Room.md](PRD-Home-Stock-Room.md) — read it before
making product/architecture decisions.

## Status

Pre-code. The repo currently contains only the PRD (Draft v0.1). No stack, no scaffolding,
no build tooling chosen yet.

## Core concepts

- **Ingestion** — two input paths that normalize to a common event schema:
  - Email: OAuth (Gmail/IMAP), read-only, filtered to known retailer sender domains, per-retailer HTML parsing templates.
  - Manual upload: photo (OCR) or PDF, same normalization pipeline as email.
- **Item resolution** — fuzzy-match raw receipt strings (e.g. `TESCO WHL CHKN 1.2KG`) to a
  canonical product table. Unmatched items trigger a one-time user confirmation that is then
  learned per retailer.
- **Multi-home data model** — `household_id` → many `location_id` (e.g. Home A – Fridge,
  Home A – Pantry). Inventory state keyed by `(location_id, item_id, estimated_qty, last_updated)`.
- **Prediction (two distinct models)**:
  - Behavioral reorder model for non-perishables (order-cadence forecasting).
  - Shelf-life/spoilage model for perishables (rules-based, adjusted by storage location).
- **Downstream** — recipe suggester (near-expiry weighted) and reorder/basket pre-fill (stretch).

Common ingestion event schema:

```
{ household_id, location_id, item_raw_name, item_canonical_id,
  quantity, unit, price, date, source /* "email" | "upload" */ }
```

## MVP scope (v1)

In: email parsing for 2–3 retailers (start Tesco + Amazon), manual upload + OCR, single-location
inventory view, basic non-perishable reorder prediction, basic perishable expiry flagging
(static shelf-life table).

Deferred to v2+: multi-home support, recipe suggester, learned shelf-life model, more retailers,
any advertiser/monetization data layer.

## Privacy & compliance (non-negotiable)

Grocery/consumption data is sensitive under UK/EU GDPR (reveals health, household composition,
lifestyle).

- Any data use beyond core app function requires **explicit, separate opt-in** — never bundled
  into general ToS.
- Model "shareable with third parties" as a **per-user flag on the schema**, not a structural
  assumption, so it can be enabled later without a redesign.
- Email access scoped as narrowly as possible (read-only, specific senders), clearly explained.
- Right to delete: users can purge inventory history and email connection entirely.

## Key risks to keep in mind

- No public receipt APIs — email parsing is brittle to retailer template changes; needs ongoing maintenance.
- OCR accuracy on paper receipts likely needs a manual-correction UI.
- Item-matching quality determines whether predictions feel useful; likely the biggest long-term data asset.
- Multi-home location inference errors can silently corrupt two inventories at once.
- Any data monetization needs legal review first.

## Open decisions

- Which retailers to prioritize for email parsing at launch.
- Build vs. buy for OCR (Google Vision / AWS Textract vs. custom).
- Whether delivery-address location inference is reliable enough to skip manual assignment.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore

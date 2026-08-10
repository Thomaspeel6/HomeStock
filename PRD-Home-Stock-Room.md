# Product Requirements Document: HomeStock

**Status:** Draft v0.1
**Author:** [Your name]
**Date:** 24 July 2026

---

## 1. Summary

HomeStock is an automatic household inventory system. It builds a live picture of what a household owns — groceries, toiletries, household goods — by parsing receipts from grocery deliveries and online orders, rather than requiring manual entry. It predicts when items will run low or expire, supports multiple homes/locations per user, and powers downstream features like recipe suggestions and auto-reordering.

## 2. Problem Statement

People don't know what they have until they run out (or find it rotten). Manually tracking a home inventory is tedious enough that almost no one does it consistently. Meanwhile, most of this data already exists in receipts and order confirmations — it's just not captured. This problem compounds for people managing more than one home (second homes, rentals, elderly relatives, shared family houses), where "what's in the other fridge" is currently untrackable.

## 3. Goals

| Goal | Metric |
|---|---|
| Reduce food waste | % of tracked perishables consumed before expiry estimate |
| Reduce forgotten reorders (running out) | # of "out of stock" surprises reported per user per month |
| Passive data capture | % of inventory added via receipt parsing vs. manual entry |
| Multi-home utility | % of multi-home users actively viewing >1 location per week |

## 4. Non-Goals (v1)

- Building a retailer marketplace or checkout integration
- Real-time barcode/computer-vision fridge scanning
- Advertiser data marketplace (deferred — see §9 Privacy)

## 5. Target Users

- **Primary:** Householders who grocery shop online (Tesco, Ocado, Amazon Fresh, Deliveroo) and want less manual tracking.
- **Secondary:** People managing multiple properties (second home, elderly parent's house, adult children's shared flat) who need visibility without being physically present.
- **Tertiary:** Health/budget-conscious users who want a recipe suggester driven by what's actually in the house.

## 6. User Stories

1. As a user, I connect my email inbox so receipts from known retailers are automatically detected and parsed.
2. As a user, I can photograph or upload a paper/PDF receipt to add its items manually.
3. As a user, I can assign an order/receipt to a specific home/location if I manage more than one.
4. As a user, I see a live inventory per location, with quantity estimates and freshness/expiry status.
5. As a user, I get a notification when an item is predicted to run out or expire soon.
6. As a user, I can ask "what can I cook with what I have" and get recipe suggestions weighted toward near-expiry items.
7. As a user, I can correct a misidentified item once, and the system remembers the mapping.

## 7. System Architecture

### 7.1 Ingestion Layer
- **Email monitoring path:** OAuth connection to Gmail/IMAP; filters scoped to known retailer sender domains; per-retailer HTML parsing templates.
- **Manual upload path:** photo (OCR) or PDF upload; same normalization pipeline as email.
- Both output a common event schema:
```
{
  household_id,
  location_id,
  item_raw_name,
  item_canonical_id,
  quantity,
  unit,
  price,
  date,
  source  // "email" | "upload"
}
```

### 7.2 Item Resolution
- Fuzzy-match raw receipt strings (e.g. "TESCO WHL CHKN 1.2KG") against a canonical product table.
- Unmatched items trigger a one-time user confirmation ("what is this?"), which is then learned for future receipts from that retailer.

### 7.3 Multi-Home Data Model
- `household_id` → many `location_id` (e.g. Home A – Fridge, Home A – Pantry, Home B – Fridge).
- Inventory state: `(location_id, item_id, estimated_qty, last_updated)`.
- Location is either user-selected at receipt-assignment time, or inferred from the delivery address on the receipt.

### 7.4 Prediction Engines (two distinct models)
- **Behavioral reorder model** (non-perishables: toothpaste, cleaning products): predicts next purchase from historical order cadence per item, per household (moving average / exponential smoothing).
- **Shelf-life/spoilage model** (perishables: chicken, milk, produce): rules-based from food safety shelf-life data, adjusted by storage location (fridge vs. freezer materially changes shelf life).

### 7.5 Downstream Features
- Recipe suggester: queries inventory for near-expiry, low-quantity items across a chosen location.
- Reorder suggestions/basket pre-fill (stretch goal, retailer-dependent).

## 8. MVP Scope

**In scope for v1:**
- Email receipt parsing for 2–3 major retailers (start with Tesco, Amazon)
- Manual receipt upload + OCR
- Single-location inventory view
- Basic non-perishable reorder prediction
- Basic perishable expiry flagging (static shelf-life table, not learned)

**Deferred to v2+:**
- Multi-home support
- Recipe suggester
- Learned/adaptive shelf-life model
- Additional retailer integrations
- Any advertiser/monetization data layer

## 9. Privacy & Compliance

Grocery and consumption data can reveal health conditions, household composition, and lifestyle — sensitive under UK/EU GDPR. Key requirements:
- Explicit, separate opt-in consent for any data use beyond core app function (e.g. advertiser data sharing), never bundled into general terms of service.
- Data model should treat "shareable with third parties" as a per-user flag bolted onto the schema, not a structural assumption, so it can be enabled later without a redesign.
- Email inbox access should be scoped as narrowly as possible (ideally read-only, filtered to specific senders) and clearly explained to users.
- Right to delete: users must be able to purge inventory history and email connection entirely.

## 10. Risks & Open Questions

| Risk | Notes |
|---|---|
| No public receipt APIs from retailers | Relying on email parsing is brittle to template changes; needs ongoing maintenance |
| OCR accuracy on paper receipts | May require a manual-correction UI to stay usable |
| Item-matching quality | Determines whether predictions feel useful or annoying; likely the biggest long-term data asset |
| Multi-home inference errors | Wrong location assignment silently corrupts two inventories at once |
| Privacy/regulatory risk if monetizing data | See §9 — needs legal review before any advertiser feature |

## 11. Success Metrics (post-launch)

- % of receipts successfully auto-parsed without manual correction
- Weekly active inventory views per household
- Reduction in self-reported "ran out unexpectedly" incidents
- Retention of multi-home users specifically (proxy for differentiated value)

## 12. Open Decisions Needed

- Which retailers to prioritize for email parsing at launch
- Build vs. buy for OCR (Google Vision / AWS Textract vs. custom model)
- Whether location inference from delivery address is reliable enough to skip manual assignment

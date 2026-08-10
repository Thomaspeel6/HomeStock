# Recipe template

A recipe teaches the ingestion agent how to read one retailer's receipt emails.
It is **data, never code** — no parsers, no scripts. Copy this into
`recipes/<retailer>.yaml`, fill it in, and open a PR.

```yaml
retailer: <name>                # e.g. tesco
country: <ISO code>             # e.g. GB
sender_domains:                 # ONLY emails from these senders are ever read
  - <domain>
canonical_email:                # which email type carries the true line items
  type: <order_confirmation | delivery_receipt | invoice>
  why: >
    One sentence. For grocers with substitutions, this must be the email that
    reflects what was actually delivered, not what was ordered.
order_id:                       # how to find the source_ref
  location: <where in the email the order number appears>
  fallback: message-id          # if no order id is extractable
line_items:
  location: <where the item table/list lives in the email>
  notes: >
    Anything odd: multibuy lines, deposit lines, substitution markers,
    loyalty discounts that appear as negative lines (skip them), etc.
example: |
  <REDACTED sample of the line-item section — strip names, addresses,
   payment details. Keep 3-5 item lines and the order id line.>
```

Rules:
- The example MUST be redacted. No addresses, no names, no card digits.
- One retailer per file. Regional variants get their own file (tesco-ie.yaml).
- Until the eval harness ships, a maintainer validates recipes by hand against
  their own receipts. After that, include example receipts and expected output.

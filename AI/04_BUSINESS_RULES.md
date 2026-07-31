# Business Rules

## Core Rules

- Fair distribution by hours
- Suppliers = fallback only
- Vehicle type must match

---

## Event Rules

- Same Event ID = same event
- Multiple stages possible

---

## Fairness

NOT:
- number of jobs

BUT:
- total occupied time

---

## AI Rules (Planned)

- Optimize travel
- Balance workload
- Interpret day notes

---

## Same Driver Column (Excel)

- Planner can paste the same text (usually the Event text) into the
  "Same Driver" column on several rows to flag "one driver should do all
  of these, back and forth, if possible."
- Overlapping times between rows sharing the same flagged value are
  allowed for the same driver -- not treated as a conflict.
- If one driver truly cannot cover the whole flagged group (hours run
  out, or a row needs a vehicle type they're not licensed for), the
  system brings in as few additional drivers as possible rather than a
  fixed time-based or type-based split rule.
- Same reuse-first idea applies if the group falls through to a hired
  supplier unit instead of an in-house driver.

---

## Planner Overrides

Planner always wins over system
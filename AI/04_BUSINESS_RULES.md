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

## Driver Hours

- A driver's daily overtime is hard-capped at MAX_OVERTIME_HOURS_PER_DAY
  (currently 2 hours), on top of their working_hours_per_day baseline --
  regardless of how much monthly overtime allowance they have left. A
  driver cannot be given, for example, a 22-hour day just because their
  monthly budget has room for it.
- A blank "Max overtime/month" is treated as 0 overtime allowed, same as
  an explicit 0 -- not as "unlimited."

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
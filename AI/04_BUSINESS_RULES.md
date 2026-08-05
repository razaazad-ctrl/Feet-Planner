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

- A driver's normal day is `working_hours_per_day` (e.g. 9). This is now
  also a hard MINIMUM: if a driver is used at all on a given day, they
  must reach at least this many hours that day (added 2026-08-03, HR-005).
  Enforced by a repair pass after normal allocation, since the engine
  can't know a driver's full-day total until the day's last job is
  considered -- see AI_CONTEXT.md Section 6.
- A driver's daily ceiling is `max_working_hours_per_day` (e.g. 12),
  planner-set per driver -- on top of `working_hours_per_day`, regardless
  of how much monthly overtime allowance they have left. A driver cannot
  be given, for example, a 22-hour day just because their monthly budget
  has room for it. (This field replaces the old hardcoded
  `MAX_OVERTIME_HOURS_PER_DAY = 2.0` constant, which had no UI to change
  it -- fixed 2026-08-03, see HR-002/NEW-002 in the scheduling rules spec.)
  Left blank, it falls back to `working_hours_per_day` (zero daily
  overtime), not "unlimited."
- A blank "Max overtime/month" is treated as 0 overtime allowed, same as
  an explicit 0 -- not as "unlimited."

---

## Shift (redesigned 2026-08-03)

- The planner never fixes an exact shift-start clock time before
  planning. They just mark a driver "Morning", "Evening", or leave it
  blank (no restriction). Evening = 12:00 onward.
- The driver's actual first-job time each day comes out of whatever the
  plan produces, and is only known/announced after planning -- never
  chosen in advance.
- No automatic 15-day-morning/15-day-evening rotation, and no
  off-day-triggered transition rule. Explicitly rejected by the project
  owner: the planner decides who's on which shift, day to day, and the
  software does not compute or remember any rotation.

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
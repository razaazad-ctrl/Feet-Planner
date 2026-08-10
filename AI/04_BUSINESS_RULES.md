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

## Shift (redesigned 2026-08-03, refined 2026-08-09)

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
- **Refined 2026-08-09: the Morning/Evening window only gates a driver's
  FIRST job of the day, not every job.** Confirmed directly against a
  real human-planned day: a driver marked "Morning" was routinely given
  an afternoon job as a natural continuation of a day already under way
  (e.g. 07:00-15:00 then, after a short gap, 16:00-19:00) -- the previous
  behavior (checking every single job against the window) would have
  wrongly refused that second job. The project owner's own framing: "if
  a driver is in morning shift 07:00 that means his shift will end 16:00
  if the 12hr max field is empty... he can definitely get a job or two
  after 12:00." So the real rule is: once a driver has ANY job already on
  a given day, later jobs that same day are governed purely by the
  normal overlap/hour-ceiling rules, not re-checked against the shift
  window -- the window only decides which half of the day their very
  first job can fall in. See AI_CONTEXT.md Section 6 ("Shift
  enforcement") and CHANGELOG_AI.md Phase 16 for the full technical
  writeup.

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
- A fresh group's opening driver is picked by projecting the group's
  TOTAL hours onto each candidate, not just the opening row's duration
  (added 2026-08-06, see CHANGELOG_AI.md Phase 14) -- reduces (but does
  not eliminate) the chance of one driver being locked into a large group
  purely because they were idle for its first row.
- **RESOLVED 2026-08-09** (was an open concern raised 2026-08-06): on a
  real day, removing every "Same Driver" value entirely made results
  WORSE (12 unresolved vs. fewer with grouping in place), confirming the
  feature is genuinely load-bearing, not just a fairness obstacle -- and
  the alphabetical-order driver-search artifact was separately fixed in
  Phase 14 (now least-occupied-first). The remaining concern -- that
  grouping was "ruling" the allocation rather than assisting it -- turned
  out to trace to two separate, real algorithm gaps rather than the
  grouping feature itself being wrong: (1) `_swap_repair` could only
  displace a single ungrouped job to make room for something else, never
  a whole Same-Driver group, so a group stuck in the wrong place had no
  way to get unstuck; (2) group members that never got pre-merged into
  one `PlanningUnit` (see `build_planning_units`) had no incentive to
  land on the same driver even when nothing was stopping them. Both
  fixed 2026-08-09 -- see CHANGELOG_AI.md Phase 16 for the full
  technical detail, including the new `allocate_by_solver()` strategy
  that resolves this class of problem directly via constraint
  programming rather than heuristic patching.

---

## Planner Overrides

Planner always wins over system
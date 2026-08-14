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

- **Measured against duty SPAN, not summed job duration (corrected
  2026-08-10, Phase 21).** A driver's hard floor/ceiling is checked
  against the time from their FIRST job's start to their LAST job's end
  on a given day -- not the sum of their individual job durations. A
  driver with three jobs totalling 8 summed hours but spread across a
  10-hour window has a 10-hour day for hard-rule purposes, not an
  8-hour one. Summed job duration ("hours worked") has NO hard rule of
  its own at all -- it is used only for fairness/balance (spreading
  workload evenly across drivers), never to decide whether an assignment
  is legal. Confirmed directly by the project owner with a concrete
  example (a driver with 2h+3h+3h=8h of summed work across a day should
  never be blocked from that day being legal, nor forced to pick up more
  work just to reach 9h of SUMMED duration). This resolves what had been
  an explicitly open question in this project since 2026-08-03 (spec
  OPT-001, "the duty-span question") -- see CHANGELOG_AI.md Phase 21 for
  the full technical writeup across every allocation strategy.
- A driver's normal day is `working_hours_per_day` (e.g. 9). This is
  also a hard MINIMUM, measured by span as above: if a driver is used at
  all on a given day, their span must reach at least this many hours
  (added 2026-08-03, HR-005). Enforced by a repair pass after normal
  allocation, since the engine can't know a driver's full-day span until
  the day's last job is considered -- see AI_CONTEXT.md Section 6. This
  minimum does NOT apply at all if any of the driver's jobs that day
  belong to a "Same Driver" flagged group (see the exemption under "Same
  Driver" column below) -- confirmed via a real driver in ground-truth
  data who legitimately worked a 5-hour span because his whole day was
  one flagged group.
- A driver's daily ceiling is `max_working_hours_per_day` (e.g. 12),
  planner-set per driver -- on top of `working_hours_per_day`, regardless
  of how much monthly overtime allowance they have left. A driver cannot
  be given, for example, a 22-hour SPAN just because their monthly budget
  has room for it. (This field replaces the old hardcoded
  `MAX_OVERTIME_HOURS_PER_DAY = 2.0` constant, which had no UI to change
  it -- fixed 2026-08-03, see HR-002/NEW-002 in the scheduling rules spec.)
  Left blank, it falls back to `working_hours_per_day` (zero daily
  overtime), not "unlimited."
- A blank "Max overtime/month" is treated as 0 overtime allowed, same as
  an explicit 0 -- not as "unlimited."
- **Monthly overtime is also measured against SPAN, not summed duration
  (confirmed directly by the project owner, correcting an interim guess
  made mid-fix that it should track summed "actual worked" hours instead
  -- it does not).** `working_hours_per_day` is the total legal daily
  hours; any SPAN beyond it on a given day is that day's overtime,
  deducted from `max_overtime_hours_per_month` -- the exact same duty-span
  concept as the daily ceiling above, not a separate one. See
  CHANGELOG_AI.md Phase 21.
- **Two new derived fields requested 2026-08-10, BUILT 2026-08-14 (Phase
  24)** on the Drivers tab: "Balance Overtime / month"
  (`max_overtime_hours_per_month` minus overtime actually used so far
  this month, via `db.get_driver_month_overtime_hours`) and "Balance
  hours / month" (`total_hours_per_month_target` minus total SPAN-hours
  logged so far this month, via the new `db.get_driver_month_span_hours`).
  Each field reads zero only when its OWN source field is blank --
  Balance Overtime/month is gated by `max_overtime_hours_per_month`
  being blank, Balance hours/month independently by
  `total_hours_per_month_target` being blank (confirmed with the project
  owner; the two are NOT coupled to each other, since most regular
  drivers leave `total_hours_per_month_target` blank -- it's mainly for
  temp drivers -- and coupling both fields to that one blank would have
  made Balance Overtime/month useless for the common case). Both are
  recomputed live from `finalized_jobs` each time a driver is selected on
  the Drivers tab (same pattern the existing "hours logged this month"
  label already used) -- the underlying numbers only change when a day
  is actually Finalized, but there's no separate caching/persistence
  layer. Built only after fixing two pre-existing bugs found while
  scoping this work (Phase 23): a live `NameError` in
  `_fill_gaps_with_unresolved_jobs`, and `get_driver_month_overtime_hours`
  itself still measuring summed job duration instead of duty SPAN,
  contradicting Phase 21. See `CHANGELOG_AI.md` Phases 23-24 for the
  full writeup.

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
# CHANGELOG_AI.md — Fleet Planner

Major architectural and logical changes only, in chronological order.
Not a line-by-line diff log — see git history (once this is committed)
for that level of detail going forward.

## Phase 0 — Requirements discovery and architecture decision

- Original ask: a fleet-planning tool for a catering company, initially
  discussed generically as "software or a site" (web vs desktop unclear).
- Domain requirements gathered: real Excel planner format examined (a
  finished example PDF was provided), revealing supplier naming
  conventions (`"1"` suffix for a second unit, `"SAME"` prefix for
  reuse), event-ID chains across multiple job rows, driver duty-time
  rosters, vehicle handoffs between shifts.
- **Architecture decision: single-user standalone desktop app**, not a
  multi-user web system. Reasoning: only one planner uses the actual
  software; supervisor and drivers only ever see the *exported file*,
  distributed externally. This eliminated the case for hosting/accounts/
  real-time sync entirely.
- Stack chosen: Python + PySide6 (Qt) + SQLite, packaged for Windows via
  PyInstaller (packaging itself deferred — not done yet, see
  `NEXT_SESSION.md`).

## Phase 1 — Master data foundation

- Built `db.py` schema v1: `drivers`, `driver_rules`, `suppliers`,
  `supplier_rules`, `vehicles`, `off_day_log`, `comp_days`.
- Built `rules_parser.py`: regex-based recognition of free-text rule
  lines (`"Shift start: 07:00 AM"`, `"Max duty hours: 8"`, etc.) into
  structured `(rule_type, parsed_value)` pairs, with unrecognized lines
  falling back to `"custom"` (AI-context only).
- Built `entity_rules_widget.py`: a generic Drivers/Suppliers screen —
  list + free-text rule lines, using the parser above. This became the
  original Drivers and Suppliers tabs.
- Built `vehicles_tab.py`: simple CRUD, no rule-line concept (vehicles
  never used free-text rules).
- **Real bug found and fixed:** case-sensitive uniqueness allowed
  "DEEPAK DEWAN" and "Deepak Dewan" as separate drivers. Fixed with
  `COLLATE NOCASE` on `drivers.name`, `suppliers.name`, `vehicles.plate`.
- **UX gap found and fixed:** vehicles couldn't be edited after creation
  (Notes/Type were add-only). Added `db.update_vehicle` and an Edit
  dialog.
- Feature added on request: **exclusion toggle** ("don't use tomorrow")
  for drivers/suppliers/vehicles, with visual demotion (color change +
  sort to bottom of list) so a planner can't forget to check it.
- Feature added on request: **serial numbers** on Drivers/Suppliers
  lists, matching the numbering Vehicles already showed via its table's
  row headers.
- Feature added on request: **Settings PIN**, framed explicitly as
  friction-prevention, not real security.

## Phase 2 — Excel import and deterministic allocation engine v1

- Built `excel_import.py` v1, assuming a single combined
  `"21-Mar-2026 02:00 - 06:00"` cell format (matching the sample PDF).
- Built `allocation_engine.py` v1: `DriverProfile`, `VehicleProfile`,
  `SupplierUnitProfile` (a *pre-named* unit model at this stage —
  suppliers required manually typing `"Unit: PINK PEPPER CHILLER TRUCK
  #1 (5 Ton Chiller Truck)"` rule lines ahead of time).
- Fairness model established from the start: **hours occupied, not job
  count** — explicitly requested, because a driver waiting on-site for
  one long event shouldn't be treated as "less busy" than one doing
  several short trips.
- In-house-first, supplier-fallback priority established.
- Built `plan_day_tab.py` v1: upload, day-notes box, Run Planning,
  results table (SR/Time/Event/Type/Pickup/Assigned-To/Note columns —
  no separate Vehicle column yet).
- Tested against synthetic data and the sample PDF's real numbers;
  correctly demonstrated hour-cap enforcement, off-day skipping with
  planner override, and fair supplier-overflow distribution.

## Phase 3 — AI/Maps layer and decision history

- Clarified design principle: **Run Planning and AI Review stay as two
  separate steps**, not merged, explicitly to keep the deterministic
  engine debuggable in isolation while it was still being hardened. A
  combined one-click shortcut was discussed as a *future* addition once
  the engine is proven — not built.
- Built `maps_client.py`: Google Routes API wrapper,
  `routingPreference: TRAFFIC_AWARE`, departure time passed as the
  actual job's end time (so travel estimates differ meaningfully by
  time of day, not just distance).
- Built `ai_review.py`: system prompt establishes the AI never
  re-checks hard rules (trusts the engine), only reasons about
  event-chain stay-vs-cycle decisions and day-notes overrides, and must
  respond in a fixed JSON suggestion schema.
- Built `settings_tab.py`: API key entry with "Test" buttons (each
  makes one minimal real request) for both Anthropic and Google Maps.
- Feature added on request: **decision-history digest**, specifically
  to answer "does the AI get smarter over time" honestly (no — the
  model is stateless per call) while still letting the *system*
  reflect accumulated real decisions without unbounded cost growth.
  Two-tier design: `decision_log` (full, local, never sent) +
  `preference_digest` (small, refreshed periodically, the only thing
  sent). Verified via test that a second refresh with no new decisions
  makes zero API calls, and old decisions are never resent on
  subsequent refreshes.
- Suggestions upgraded from read-only text to individual **Accept/Reject
  cards**, each logging to `decision_log` on click.

## Phase 4 — Real-file bug fixes (round 1)

Triggered by the user testing with a real Excel file
(`FMS_28th_FEB_2026107.xlsx`, 128 rows) and a real reference PDF
(`FMS_21st_MARCH_2026.pdf`, 90 rows).

- **Critical bug fixed:** the real export format has **separate `START
  DATE` and `TIME` columns**, not one combined cell. `excel_import.py`
  was rewritten (`_parse_date_value`, `_parse_time_range_only`,
  `_combine_date_and_time`) to handle both the real format and the old
  combined-cell format as a fallback. Fixed parsing from 0/128 rows to
  125/128 (the remaining 3 were legitimate footer/note rows).
- **Bug fixed:** footer/note rows (duty-time roster, "vehicles in
  workshop" list) were being treated as failed job rows instead of
  skipped. Fixed by requiring SR# to parse as a plain integer.
- **Bug fixed:** vehicle-type text with embedded newlines (from wrapped
  Excel cells) broke exact-match comparisons. Added `_clean_text()`
  normalization.
- **Gap fixed:** the assigned vehicle plate was never shown in the
  results table, even though the engine always supported a driver using
  different vehicles across a day. Added `Job.assigned_vehicle_plate`
  and a dedicated "Vehicle / Unit" table column.
- Feature added: **Locations tab** — short-code to real-address mapping,
  with an `exact`/`approximate` confidence flag threaded through to the
  AI review layer's travel-time reasoning.
- Feature added: **PIN gating rebuilt properly.** The original
  implementation switched to the Settings tab and reverted on a wrong
  PIN, which could flash contents briefly. Rebuilt to disable the tab
  entirely (`setTabEnabled(False)`) while locked, with a corner "Unlock
  Settings" button as the only entry point.
- Feature added: **sorting and filtering** on the Plan a Day results
  table (click-to-sort columns, dropdown filter by driver/supplier,
  correctly grouping `"SAME X"` entries with their base label `"X"`).
- Feature added: **Export Filled Excel** — loads the *original* uploaded
  workbook via `openpyxl` (preserving all formatting/styles/other
  columns) and writes only the Vehicle and Driver cells for matched
  rows. Verified byte-for-byte that an untouched column (`Event`) was
  identical before/after, and header formatting was preserved.

## Phase 5 — Major supplier and driver-rules redesign

Triggered by the user reporting that suppliers weren't being picked up
at all, and explaining the real desired behavior in detail.

- **Root cause found:** the original supplier model required
  pre-declaring individual named units (`"Unit: PINK PEPPER CHILLER
  TRUCK #1 (Type)"`) via free-text rule lines matched by regex. The
  user's actual phrasing (`"12 Seated Bus 100/hour"`) never matched any
  recognized pattern, so it silently became AI-context instead of a
  hard rule — meaning zero supplier units were ever built for
  allocation.
- **Design pivot (major):** suppliers were rebuilt around **structured
  rate/type/availability offerings** (`supplier_offerings` table: one
  row per vehicle type a supplier provides, with rate/hour and daily
  max). Individual unit naming/numbering became something the **engine
  decides dynamically at planning time**, not something the planner
  pre-declares — matching the real-world pattern the user described.
- Naming convention confirmed explicitly by the user (not guessed):
  1st hire = plain supplier name; 2nd hire = `"NAME 1"`; reuse of an
  existing hire = `"SAME <label>"`. Priority: **reuse before hiring
  new** (minimize headcount for the day), tiebreaking new hires by
  lowest cross-day cumulative historical hours (`finalized_jobs`).
- **Design pivot (major, same root cause class):** driver hard rules
  were also rebuilt around **structured fields** instead of free-text
  rule lines, because the same fragile-regex problem had independently
  caused a driver's hour cap to silently go unenforced. New structured
  columns: `working_hours_per_day`, `shift_start`, `off_days`,
  `max_overtime_hours_per_month`, `total_hours_per_month_target`,
  `license_types`. Free-text rule lines (`driver_rules`,
  `supplier_rules`) were *not removed* — explicitly rescoped to
  "AI context notes only," shown separately in the UI with that label.
- New dedicated tabs built to replace the generic
  `entity_rules_widget.py` for this purpose: `drivers_tab.py`,
  `suppliers_tab.py`. `entity_rules_widget.py` left in the codebase but
  no longer wired into `main_window.py`.
- **Overtime model clarified and implemented:** monthly, not daily —
  `max_overtime_hours_per_month = None`/blank means unlimited overtime;
  `0` means none allowed at all; a positive number is a real monthly
  budget checked against per-day excess-over-baseline summed from
  `finalized_jobs` history (`db.get_driver_month_overtime_hours`).
- **New table: `finalized_jobs`**, populated by a new "Finalize Day"
  button. This is the historical basis for both the monthly overtime
  check above and cross-day supplier fairness
  (`db.get_supplier_cumulative_hours`). Re-finalizing a date overwrites
  (deletes + reinserts) rather than duplicating.
- Migration system introduced (`_MIGRATIONS` list, additive `ALTER
  TABLE`) specifically to stop requiring the user to delete their
  database file on every schema change going forward.

## Phase 6 — Second round of real-file validation and bug fixes

Triggered by the user reporting that in-house drivers were each only
getting one delivery per day, and that shift-start times weren't being
respected (a driver with a 6 PM shift start was assigned a 10 AM job).

- **Critical bug found and fixed:** `shift_start` was a real column,
  correctly saved by the UI, correctly read by SQL — but had been
  **omitted from the `DriverProfile` dataclass entirely** during the
  Phase 5 rewrite (a copy/paste gap), and `build_driver_profiles` never
  read it into the profile. The enforcement function
  (`_job_is_before_shift_start`) was written but had nothing to check
  against. Fixed by adding the field to the dataclass and reading it in
  `build_driver_profiles`; verified with a direct before/after test
  (6 PM shift correctly rejects a 10 AM job, correctly accepts a 7 PM
  job).
- **Real-data testing method established:** the user pushed an actual
  `fleetplanner.db` snapshot plus a real day's `UNPLANNED.xlsx` /
  `PLANNED.xlsx` pair to a public GitHub repository
  (`razaazad-ctrl/Feet-Planner`) specifically so the assistant could
  clone it and test against real data directly, rather than relying on
  descriptions of bugs. This became the primary validation method for
  the rest of the session.
- **Root-cause diagnosis of "1 driver, 1 delivery":** running the actual
  engine against the real database revealed the "bug" was almost
  entirely **a data configuration issue, not a code defect**:
  - Every one of 11 drivers had the *identical* `license_types` value
    (`"5 Ton Chiller Truck (with lift)"`), so 95 of 125 real jobs
    (dry trucks, buses, vans, everything else) could never go in-house
    regardless of algorithm quality.
  - Real vehicle scarcity: only 4 in-house chiller trucks existed, and
    2 were legitimately locked for the whole day by one genuine 12-hour
    event (verified by checking the actual job's event/additional-info
    text — a real "Ramadan Boxes Delivery" outside-catering job, not a
    data error).
  - A missing driver (`VENUGOPAL`) appeared in the planned reference but
    didn't exist in the database at all.
  - A vehicle-type text mismatch (`"23 Seater Bus"` entered vs `"23
    Seated Bus"` required by the job) caused a silent zero-match — not
    a code bug, but confirmation that exact-string matching has this
    real failure mode in practice.
- A script was built (and run, not just designed) to **extract suggested
  `license_types` per driver directly from the real `PLANNED.xlsx`**,
  by grouping actual historical Driver→Vehicle-Type pairs. Applied to a
  test copy of the database and re-compared: match rate against the
  reference improved from 4% to 7% (modest, confirming license types
  were a real factor but not the sole explanation — vehicle scarcity and
  the missing-driver/text-mismatch issues account for the rest).
- **Supplier-numbering convention re-confirmed, not changed:** the user
  initially seemed to want per-supplier numbering behavior to vary (some
  suppliers numbered, some not), based on the reference file showing
  `AL SADAT HEAVY TRUCK` repeated with no numbers while `AL WASL` showed
  numbers. After discussion, the user clarified the reference file
  itself is simply an inconsistent manual artifact, and the **existing
  dynamic-numbering engine behavior is correct and should not change** —
  no code change resulted from this thread, only clarification.

## Cumulative net effect of the two structured-field pivots

The single biggest recurring lesson across this project, stated
explicitly because it should inform all future work: **a rule is not
"implemented" until it is (a) stored in a structured, unambiguous
format, (b) read into the in-memory runtime object used during
allocation, and (c) actually checked inside `allocate()`'s candidate
loop.** Two separate real bugs in this project (the free-text hour-cap
failure, and the shift_start dataclass omission) each satisfied only
two of those three steps and silently failed at the third. Any review of
new or existing rule logic should explicitly verify all three steps are
present, not just that the field exists in the database and the UI.

## Phase 7 — "Same Driver" Excel column

- New requirement: a "Same Driver" column in the daily request Excel
  file, which the planner pastes the same text onto for every row they
  want one driver to handle back-and-forth, rather than as independent
  jobs for fairness/overlap purposes.
- Before implementing, checked the requirement against the real
  `PLANNED.xlsx`/`UNPLANNED.xlsx` files (pulled from the GitHub repo
  referenced in `NEXT_SESSION.md`) rather than guessing the split logic
  in the abstract. Found the historical human-planned assignments split
  flagged groups differently case by case (sometimes one driver did the
  whole group, sometimes split by vehicle-type license, sometimes with
  two rows for the same driver having identical overlapping times) --
  confirmed this ambiguity with the project owner before writing any
  code, per Rule 16.
- Two design questions were explicitly confirmed with the project owner:
  (1) overlapping times within a flagged group are allowed for the same
  driver, not treated as a conflict; (2) when one driver can't cover the
  whole group, the engine picks the fewest additional drivers possible
  rather than a hard-coded time-based or vehicle-type-based split.
- Implemented in `excel_import.py` (`same_driver_key` field, "same
  driver" header recognized) and `allocation_engine.py` (group-scoped
  overlap relaxation via a `group_key` tag on every busy interval;
  reuse-before-new-driver preference; same treatment extended to the
  supplier-hire fallback pass).
- While implementing, found that writing `[Same Driver group]` into
  `assignment_note` would have leaked into the exported Excel file's
  Driver cell, because `export.py` derived the driver name by string-
  parsing `assignment_note` (`.replace("In-house: ", "")`). Fixed by
  adding a dedicated `Job.assigned_driver_name` field that `export.py`
  now uses directly, so `assignment_note` is free to carry extra
  human-readable context without risk to the exported file. Verified
  with a byte/value-level diff of the exported file against the
  original confirming every column except Driver/Vehicle is unchanged.
- Tested with: (a) the real `UNPLANNED.xlsx` run through the real
  database's actual drivers/vehicles/suppliers (`fleetplanner.db`) --
  all 9 flagged groups in that file resolved to a single driver each,
  since the current real driver roster happens to hold multi-type
  licenses, consistent with the known data-quality note in
  `NEXT_SESSION.md` about under-specified license types; (b) a
  synthetic test suite (`test_same_driver.py`) with deliberately
  single-type-licensed drivers to force and verify an actual split,
  overlap relaxation, and a regression check that unflagged overlapping
  jobs still conflict as before; (c) `test_same_driver_supplier.py`
  confirming the same reuse/overlap behavior when a flagged group falls
  through to the supplier pass.
- **Found but explicitly NOT fixed in this session:** the real cloned
  repository's `allocation_engine.py` does not actually contain the
  `shift_start` dataclass field, `build_driver_profiles` population, or
  `_job_is_before_shift_start()` enforcement that this very documentation
  package (`AI_CONTEXT.md` Section 6/9, this file) describes as already
  fixed. `shift_start` does exist in `db.py`'s schema and in
  `drivers_tab.py`'s UI, but the engine currently never reads or enforces
  it -- meaning a driver's shift start time is not actually a hard
  constraint in the code as it stands in this repo snapshot, contrary to
  what the documentation says. Left untouched since it's outside what was
  asked this session and it wasn't clear whether this GitHub snapshot
  predates a fix already present in the planner's actual local copy of
  the app -- flagged to the project owner directly instead of assuming
  either way.

## Phase 8 — Hard-rule audit: shift_start (re-fixed) and working_hours_per_day (real bug found)

- The project owner confirmed directly that `shift_start` was not being
  enforced in their live local copy either — not just this GitHub
  snapshot — so the discrepancy flagged at the end of Phase 7 was a real,
  live bug. Fixed properly this time, following the exact three-step
  pattern this project's own docs warn about: added
  `DriverProfile.shift_start`, populated it in `build_driver_profiles`,
  and added `_parse_shift_start_time()` / `_job_is_before_shift_start()`,
  called in the candidate-filtering loop in `allocate()`. Verified with a
  dedicated test (`test_shift_start.py`): a driver whose shift starts at
  11 PM is correctly refused a 10 AM job; the same driver correctly gets
  jobs starting after their shift begins; drivers with no `shift_start`
  configured are correctly unrestricted; parsing handles both "07:00 AM"
  and "18:00" style text and fails open (not blocking) on garbage input.
- At the project owner's request, `license_types` and
  `working_hours_per_day` were also audited for the same
  field-exists-but-unenforced failure pattern:
  - `license_types` (`_driver_qualifies_for_type`, `_type_matches`) was
    confirmed correctly implemented already — tested with a driver
    licensed for the wrong type and a driver with no license types
    configured at all; both correctly refused. No code change needed.
  - `working_hours_per_day` had a real bug: the entire hours-check block
    in `allocate()` was gated on BOTH `working_hours_per_day` AND
    `max_overtime_hours_per_month` being set, so a driver with a daily
    limit configured but a blank overtime cap could be given unlimited
    hours in a single day with zero enforcement. Proven with a test
    giving one driver 21.5 hours in one day, zero rejections. This is a
    genuine business-rule ambiguity (does blank overtime mean "no limit
    at all" or "no overtime allowed"?), not just a code bug, so it was
    confirmed with the project owner before fixing rather than guessed:
    blank overtime cap is now treated the same as an explicit `0`.
    Verified with `test_license_and_hours.py`, including a check that a
    job landing exactly at the daily cap is still correctly assignable
    (no off-by-one over-restriction from the fix).
- Full regression run after all three fixes: real `UNPLANNED.xlsx` +
  real `fleetplanner.db` still resolves to the same 39 in-house / 0
  supplier / 5 unresolved split as before these fixes (i.e. no real job
  in the current dataset was actually relying on the bugs to get
  assigned), and the exported file was re-verified byte/value-identical
  to the original outside the Driver/Vehicle columns.

## Phase 9 — Hard daily overtime ceiling (real symptom reported by the project owner)

- The project owner reported, from real live use, that a driver was
  still being given absurd hours — specifically jobs from 7 AM to 5 AM
  the next day (~22 hours) — despite the shift_start and
  working_hours_per_day fixes from Phase 8. Investigated and found a
  deeper design gap, not just an empty-database issue: the monthly
  overtime bucket (`month_overtime_so_far + today's overtime <=
  max_overtime_hours_per_month`) has no concept of a per-day sub-limit.
  Most real drivers here are configured with 60h/month overtime
  allowance; with `finalized_jobs` history empty, `month_overtime_so_far`
  starts at 0 for everyone, so a single day could consume 13+ hours of
  that 60-hour budget in one sitting with nothing to stop it.
- This is a genuine business-rule decision (how many overtime hours is
  safe/legal in ONE day?), not something to invent per Rule 16, so it was
  confirmed with the project owner before implementing: 2 hours/day.
- Fixed by adding `MAX_OVERTIME_HOURS_PER_DAY = 2.0` as a module-level
  constant in `allocation_engine.py`, checked in the candidate loop
  BEFORE the monthly-bucket logic -- this caps the CUMULATIVE overtime a
  driver can accrue across however many jobs they pick up in one day,
  not just a single job, regardless of how much monthly budget remains.
- Tested with `test_daily_overtime_ceiling.py`: reproduces the exact
  reported 7 AM -> 5 AM scenario and confirms it's now rejected; also
  confirms a day landing exactly at the 11h ceiling (9h baseline + 2h
  overtime) is still assignable, and 1 minute past it is correctly
  rejected.
- Re-ran the full real-data pipeline after this fix: the real
  `UNPLANNED.xlsx` + real `fleetplanner.db` result changed materially
  from the Phase 8 baseline -- 29 in-house / 5 supplier / 10 unresolved
  (was 39 / 0 / 5 before this fix). This is the CORRECT and expected
  consequence of enforcing a real hard rule that was previously silently
  bypassed -- some jobs that were only "resolvable" because a driver was
  illegally over-worked now correctly fall to a supplier or go
  unresolved instead. Confirmed no driver in this run now exceeds 11h in
  a single day. Export byte/value-diff re-verified clean (Driver/Vehicle
  columns only).
- Currently a single global constant, not per-driver configurable --
  flagged in `NEXT_SESSION.md` in case a future request needs per-driver
  daily overtime limits (would need a new db column + UI field +
  dataclass field, same three-step pattern as every other hard rule
  here).
  conversation into a permanent documentation package
  (`AI_CONTEXT.md`, `ARCHITECTURE.md`, `DATABASE.md`, this file,
  `NEXT_SESSION.md`, `AI_INDEX.json`) intended to fully replace this
  conversation as the onboarding source for any future AI assistant
  session working on this repository.

## Phase 10 — Shift redesign + daily hours made fully configurable (2026-08-03)

- The project owner requested two related reworks in one session,
  explicitly framed as "let's untangle this working hours":
  1. **Shift.** The existing `shift_start` model (an exact clock time,
     fixed by the planner *before* planning, enforced as an absolute
     wall) was flagged as backwards -- in real operation the planner
     never commits to an exact time in advance, they just decide per
     driver whether that driver runs mornings or evenings, and the
     actual first-job time is only known -- and announced to the driver
     -- after the plan is built. A rigid 15-day-morning/15-day-evening
     rotation with automatic transitions was discussed first, then
     explicitly rejected by the project owner once it became clear the
     planner would be picking morning/evening manually anyway: "if the
     planner is deciding which driver to bring in morning and evening we
     dont have to track this."
  2. **Overtime.** The monthly overtime cap (`max_overtime_hours_per_month`)
     was already solid (see Phase 8/9), but the daily ceiling was still
     the Phase 9 hardcoded `MAX_OVERTIME_HOURS_PER_DAY = 2.0` constant
     with no UI. The project owner asked for two real driver-editable
     fields instead: `working_hours_per_day` (already existed, e.g. 9)
     and a new `max_working_hours_per_day` (e.g. 12), both hard rules,
     replacing the constant entirely. They additionally asked for a hard
     *minimum* -- a driver used at all on a day must reach at least
     `working_hours_per_day` that day -- and, after being told this can't
     be a simple per-job filter (the engine assigns jobs one at a time
     and doesn't know a driver's full-day total until the day's last job
     is considered), explicitly asked for "full rework now to enforce as
     a hard minimum" rather than deferring it.
- **Shift implementation:** `drivers.shift_start` column left in place
  but deprecated (no longer read); new `drivers.shift_period` column
  (`'morning'` / `'evening'` / `NULL`). `DriverProfile.shift_start`
  replaced with `DriverProfile.shift_period`; `_parse_shift_start_time`/
  `_job_is_before_shift_start` removed, replaced by
  `_job_matches_shift_period()` (window check: morning = before 12:00,
  evening = 12:00 onward -- `SHIFT_PERIOD_EVENING_CUTOFF_HOUR` constant
  if this ever needs to change). Drivers tab: free-text "Shift start"
  `QLineEdit` replaced with a `QComboBox` (No restriction / Morning /
  Evening). Stale free-text rule-line example ("Shift start: 07:00 AM")
  removed from both `rules_parser.py` (the pattern itself) and
  `entity_rules_widget.py` (the UI tip text), since it no longer matches
  how shift works and was actively misleading.
- **Overtime implementation:** new `drivers.max_working_hours_per_day`
  column; `DriverProfile.max_working_hours_per_day` field. The daily
  ceiling check in `allocate()` now uses
  `d.max_working_hours_per_day if d.max_working_hours_per_day is not None
  else d.working_hours_per_day` -- i.e. blank falls back to zero daily
  overtime (fail-closed), matching the existing precedent for a blank
  monthly cap, rather than reopening the Phase 9 unlimited-single-day
  bug. Drivers tab gained a "Max working hours per day" field alongside
  the existing "Working hours per day" one.
- **New minimum-hours hard rule:** added
  `allocation_engine._repair_minimum_daily_hours()`, run in a loop (up to
  5 passes) after the normal allocation pass completes. For every
  (day, driver) left with a non-zero, under-minimum total: tries to move
  ALL of that driver's jobs for that day to another qualifying driver
  with spare room (checking the same license/off-day/shift/overlap/daily-
  ceiling/monthly-overtime rules as the main pass). If every job can
  move, the move is committed (freeing the short driver entirely, giving
  the day to whoever absorbed it). If even one job can't move, the whole
  day is released to unresolved with an explicit note rather than left
  as an illegal partial day. Deliberately scoped to skip jobs inside a
  "Same Driver" group (planner-flagged pairings left alone) and does not
  attempt a supplier fallback for a released day (flagged for manual
  review instead -- moving cost to a supplier is a planner decision, not
  one the repair pass should make silently).
  - **Known limitation, disclosed to and accepted by the project owner
    at the time:** this is a best-effort greedy heuristic, not a global
    optimizer -- it can leave a fixable case unfixed if an earlier move
    in the same pass used up room that would have fixed a later one
    (the 5-pass loop mitigates but doesn't eliminate this). It has only
    been validated against small synthetic scenarios (1-3 jobs, 1-2
    drivers) so far, not a real day's full job volume, because (as with
    the Phase 8/9 work) the real `finalized_jobs`/driver-configuration
    data needed for a meaningful large-scale test isn't available yet.
- **Tests:** `tests/test_shift_start.py` deleted, replaced by
  `tests/test_shift_period.py` (window-check unit tests + full
  `allocate()` morning/evening/no-restriction scenarios).
  `tests/test_daily_overtime_ceiling.py` rewritten for the two-field
  model (12h ceiling, fail-closed blank default, an under-minimum solo
  day correctly released, and -- the key new case -- a genuine repair-
  pass success where a short day is moved onto a driver with spare room
  instead of being left unresolved). `tests/test_license_and_hours.py`
  had one fixture adjusted (a job changed from 8h to 9h) so it keeps
  testing what it originally intended (the daily-ceiling behaviour)
  without tripping the new minimum-hours rule at the same time --
  documented inline in the test file itself. Full existing suite
  (`test_same_driver.py`, `test_same_driver_supplier.py`) re-run
  unchanged and still passing, confirming the "Same Driver" group logic
  and supplier reuse were unaffected by this rework.
- Considered and explicitly decided *against* changing the general
  driver-selection fairness ranking (least-occupied-driver-first, see
  Rule 5/`SE-003`) to a "pack the already-active driver first" strategy,
  even though that would reduce how often the new minimum-hours repair
  pass has to do any work. Reason: `test_same_driver.py`'s regression
  case (G3 forced split) explicitly depends on least-occupied-first
  fairness to prove a licensed-for-both driver doesn't monopolize a
  group unrelated to the one they're already busy with. Changing the
  ranking would have silently broken that guarantee. The repair pass
  exists specifically so the minimum-hours rule doesn't require touching
  this fairness logic at all.
- Not done this session (explicitly scoped out, flagged as follow-ups):
  reporting each driver's actual first-job time back to them once a day
  is finalized (spec SS-003 in the scheduling rules doc -- the data
  exists in a finalized day, just isn't surfaced in `export.py`/
  `digest_generator.py` yet), and showing month-to-date overtime-so-far
  on the Drivers tab (spec NEW-006 -- `get_driver_month_overtime_hours`
  already computes this correctly, the tab just shows total hours
  instead right now).
- All AI documentation (`AI_CONTEXT.md`, `ARCHITECTURE.md`,
  `DATABASE.md`, `BUSINESS_RULES.md`, this file, `NEXT_SESSION.md`,
  `AI_INDEX.json`) and the separate numbered scheduling-rules spec
  (bumped to v4) updated to match, per Rule 14/17.

## Phase 11 — Gap-filling fix + a real repair-pass bug found and fixed (2026-08-03, same day as Phase 10)

- The project owner tested Phase 10's build directly against a real
  UNPLANNED.xlsx and reported the exact symptom OPT-001/002/003 (spec)
  had already flagged as a known gap: a driver was given a 13:00-15:00
  job and a 22:00-01:00 job (5h actual work across a 12h span, 7h idle
  in the middle) while a 16:00-20:00 job that would have fit neatly in
  that gap was left completely unassigned.
- **First attempt (later reverted):** tried adding a gap-fill preference
  directly inside the main greedy candidate-selection loop. Testing it
  immediately showed it was structurally dead code: jobs are processed
  strictly in start-time order, so by the time an earlier-starting job
  that would sit inside a gap is being considered, the LATER of that
  gap's two bounding jobs has never been assigned to anyone yet (it
  hasn't been reached in the sorted iteration). A "does this driver have
  a booking both before AND after this job" check can therefore never be
  true in that position. Reverted in favor of a post-pass instead (kept
  as an explanatory comment in the code so a future session doesn't
  re-attempt the same dead end).
- **Actual fix:** `allocation_engine._fill_gaps_with_unresolved_jobs()`,
  a new post-pass that runs once after the main greedy loop (and after
  suppliers) but before the HR-005 minimum-hours repair pass. For every
  job still fully unresolved, it checks every driver for a genuine
  bounded gap (`_driver_has_bounded_gap_fit()`: an existing job before
  AND one after, with the normal travel buffer) that the job fits into,
  respecting every other hard rule, and assigns it there instead of
  leaving it unresolved. Runs before HR-005's pass deliberately: filling
  a gap adds hours to that driver, which can itself resolve an
  under-minimum day without HR-005 needing to reassign anything.
  Deliberately does NOT reclaim a job already given to a supplier back to
  an in-house driver's newly-available gap -- unwinding a `SupplierHire`
  safely (renumbering, freeing capacity) is separate, riskier work not
  attempted this session; only fully-unresolved jobs (no driver AND no
  supplier) are considered.
- **A real, independent bug was found and fixed in `_repair_minimum_daily_hours`
  (the HR-005 pass from Phase 10) while building the test for the above.**
  It computed a snapshot of every driver's job list ONCE at the start of
  the pass (`by_day_driver`), then iterated over that static snapshot.
  When fixing one under-minimum driver required moving their jobs onto a
  second driver, that move correctly updated the real job/driver
  objects -- but the SECOND driver's entry in the snapshot was never
  refreshed, so when the pass later got to "fix" that second driver
  (using the now-stale snapshot showing their OLD, pre-move job list), it
  moved jobs around based on outdated information. Caught directly by
  `tests/test_gap_filling.py`: two drivers' jobs visibly ping-ponged back
  and forth between them across iterations instead of settling on a
  single, sensible assignment. Fixed by recomputing each (day, driver)
  pair's actual current job list and total hours FRESH, directly from
  `jobs`, immediately before processing it -- never from a snapshot taken
  before this pass started.
- **A second, latent correctness bug was fixed in the same function while
  in there:** when multiple jobs belonging to one under-minimum driver
  were being moved to the same replacement driver in one batch, each
  job's feasibility (overlap, daily ceiling, monthly overtime) was
  checked against that replacement driver's real `occupied_seconds` /
  `busy_intervals` only -- NOT accounting for the other job(s) already
  tentatively planned for the same driver earlier in that same batch.
  Two moves could therefore each look individually legal but jointly
  push the replacement driver over their daily ceiling. This hadn't
  manifested as a visible test failure yet (the specific scenario tested
  didn't happen to cross a ceiling), but was a real latent risk given how
  central this function now is. Fixed by tracking tentative
  hours/intervals added within a single batch (both for drivers and for
  vehicles) and checking new candidates against `real + tentative`,
  committing nothing until the whole batch is confirmed feasible.
- New test: `tests/test_gap_filling.py` -- reproduces the exact reported
  scenario (confirms the gap gets filled onto the SAME driver, not a
  fresh idle one) and confirms gap-filling still refuses to fire when it
  would break the daily ceiling. Full existing suite re-run unchanged and
  still passing.
- **Open, explicitly not decided this session:** whether a driver's
  DUTY SPAN (first job to last job, including idle time) should count
  toward daily/monthly hour limits instead of (or alongside) summed job
  duration. Explained the tradeoff to the project owner (span-based is
  more realistic about unavailability during a gap, but hits hour
  ceilings faster than actual hours worked and may not match driver pay)
  -- left open pending seeing how much the gap-fill fix above reduces
  this scenario on its own. See spec OPT-001 and `NEXT_SESSION.md`.
- Scheduling rules spec bumped to v5; `AI_INDEX.json`,
  `NEXT_SESSION.md`, `ARCHITECTURE.md`, and this file updated per
  Rule 14/17. (`AI_CONTEXT.md`, `DATABASE.md`, `BUSINESS_RULES.md` were
  checked and didn't need changes for this specific fix -- no schema or
  business-rule change this time, purely an algorithm fix -- see
  `NEXT_SESSION.md` for the still-open duty-span policy question, which
  WILL need those files updated once decided.)

## Phase 12 — Real PLANNED.xlsx study: hour-accounting bug, NEW-004, HR-005 refinement, TB-001 (2026-08-03, same day as Phase 10/11)

- The project owner supplied a real, human-planned PLANNED.xlsx (44 rows,
  11 drivers, 13 vehicles, 8 "Same Driver" flagged events) and asked for
  a detailed study: why isn't the software following the 9h min/12h max
  rule, and can the engine reproduce this exact real-world output from
  scratch (same job-to-driver grouping structure; driver NAMES don't need
  to match)?
- **Root cause confirmed exactly as the project owner suspected:**
  `occupied_seconds` was a naive SUM of every row's duration, including
  when two rows in the same "Same Driver" group are the exact same time
  slot (two simultaneous pickups on one truck for two different orders --
  confirmed as routine in the real file, not an edge case). One real
  driver showed ~17h "occupied" against ~11h of true work. This was
  flagged as a known, deliberate simplification in the module docstring
  since an earlier session ("if this ever needs to be exact... that is a
  deliberate follow-up") -- this session is that follow-up, prompted by
  real data proving it necessary rather than a guess.
- **Fix:** added `_merged_hours(intervals)` -- returns the TRUE UNION of
  a set of time intervals, so overlapping/touching intervals count once.
  Replaced every direct read/write of `occupied_seconds` (main loop,
  gap-filler, minimum-hours repair pass) with calculations through this
  function: `occupied_seconds` is now always recomputed from
  `busy_intervals` via `_merged_hours()` immediately after any change,
  never incremented/decremented directly. Projected-hours checks (before
  committing a job) now compute `_merged_hours(existing + [candidate])`
  instead of `existing_sum + candidate_duration`.
- **NEW-004 resolved** ("Driver Only" jobs): the real file has a genuine
  row of this type (SR52, a Driver Only admin task, successfully
  hand-assigned by the human with no vehicle). Added
  `_vehicle_type_needs_vehicle(vehicle_type_required)` -- False for
  "Driver Only" (case-insensitive) -- and special-cased it across the
  main pass, gap-filler, and repair pass to skip vehicle-matching
  entirely for this type.
- **HR-005 (minimum-hours repair pass) refined against real data.** The
  reconstruction showed several real drivers legitimately ending their
  day under 9h -- always because their whole day (or the shortfall
  portion of it) was inside a "Same Driver" flagged group. The pass's
  scope note ("doesn't touch grouped jobs") was correct, but its
  total-hours CALCULATION had a gap: it only summed a driver's UNGROUPED
  jobs, so a driver's grouped-job hours were invisible to the minimum
  check entirely -- meaning a driver with 3h ungrouped + 8h grouped
  (11h true, fine) could have been incorrectly flagged as an 3h
  under-minimum day, or a driver whose shortfall was entirely inside a
  group could have had their ungrouped remainder pointlessly moved
  (which could never fix anything, since the group's hours are what's
  driving the shortfall). Fixed: `total_hours` is now the true merged
  total across EVERY job the driver has that day; if ANY of those jobs
  are grouped, the day is now correctly recognized as
  unfixable-without-touching-a-protected-group and left alone entirely.
- **TB-001, a new finding, found and resolved in the same investigation:**
  after the two fixes above, 42 of 44 real rows auto-assigned in-house
  with zero supplier fallback. The remaining 2 both traced to the same
  pattern: the human ran two completely unrelated orders (different
  event IDs, no "Same Driver" flag) back-to-back with zero gap for one
  driver -- rejected by the engine's 30-minute default travel buffer,
  since that relaxation previously only applied within a flagged group.
  Raised with the project owner, who confirmed directly: a planner-set
  end time already accounts for travel back to base (e.g.
  05:00-08:00 → 08:00-11:00 for the same driver is intentional; 08:00 IS
  the chosen return-to-base time, not a shorthand needing a buffer added
  on top). `DEFAULT_TRAVEL_BUFFER_MINUTES` changed from 30 to 0 --
  adjacent (zero-gap) jobs for the same driver/vehicle are no longer a
  conflict regardless of group flag; genuine time overlap still is.
  Re-running the reconstruction after this change: **all 44 real rows
  auto-assign in-house, zero unresolved, zero supplier, zero hard-rule
  violations.** Future work flagged, not built: once live Google Maps
  travel-time lookups are wired in (the project owner's stated plan),
  gaps between jobs at genuinely different locations should be checked
  against real drive time instead of trusting manual timing -- a
  distance-aware replacement for this flat constant.
- **Reconstruction methodology** (repeatable for future validation): the
  real file's driver/vehicle assignments were stripped; jobs rebuilt
  keeping only time, vehicle-type-required, and the "Same Driver"
  grouping text; a comparable 11-driver pool built with license types
  inferred from what each real driver was observed driving (2 drivers
  configured 9h/9h no-overtime, matching the 2 real drivers who happened
  to land exactly on 9h true hours; the rest 9h/12h with a 60h/month
  overtime allowance); run through `allocate()` from scratch. Every
  "Same Driver" group split only where the real file's own group also
  required a type-driven split (cross-checked the vehicle types within
  each split group to confirm), matching the existing, previously-tested
  "fewest drivers, split only when forced" behavior (`test_same_driver.py`).
- New tests: `tests/test_hour_accounting.py` (direct `_merged_hours()`
  unit tests, the exact duplicate-simultaneous-pickup scenario from the
  real file, and a Driver-Only assignment test) and
  `tests/test_travel_buffer.py` (zero-gap back-to-back unrelated orders
  now assignable; genuine overlap still correctly rejected). Full
  existing suite re-run unchanged and still passing throughout every fix
  in this phase.
- Scheduling rules spec bumped to v7 (HR-002 addendum, NEW-004 resolved,
  HR-005 addendum, TB-001 added then resolved same day, new testing
  addendum with the full reconstruction methodology and result).
  `AI_INDEX.json` and `ARCHITECTURE.md` updated to match (new function
  entries, corrected stale "default 30" / "still sums" references found
  along the way -- some of these were stale even before this session and
  got corrected opportunistically). `NEXT_SESSION.md` updated to remove
  the now-resolved HR-005/OPT-002/003 "not validated against real data"
  caveats and replace with the real validation result.
## Phase 13 — Same-Driver two-vehicles-at-once bug, confirmed and fixed (2026-08-03, same day as Phase 10-12)

- The project owner supplied a real software-output test file (post-v7,
  after the hour-accounting/NEW-004/HR-005/TB-001 fixes) comparing actual
  output against expected, specifically for "Same Driver" grouping. Found
  a concrete, provable bug: one driver was assigned a 10 Ton Chiller
  Truck 23:00-00:00 AND a 20 Seater Bus 23:00-01:00 SIMULTANEOUSLY, both
  rows sharing the same flagged group -- physically impossible, exactly
  the "one driver can't drive 2 vehicles at once" pattern the project
  owner described.
- **Root cause:** the overlap-relaxation that lets a "Same Driver"
  group's rows overlap in time (needed for the legitimate case -- one
  truck doing two simultaneous pickups for two different orders) was
  keyed PURELY on the group tag matching (`ignore_group_key=group_key`
  in `_overlaps_with_buffer`), with no check on whether it was actually
  the same vehicle/type involved. It relaxed the conflict check for ANY
  two rows sharing a group tag, including ones needing genuinely
  different vehicles.
- **Fix:** in the main allocation loop's driver-candidate filter, the
  relaxation is now conditional per-candidate-driver: it only applies if
  the driver's already-established vehicle for that group
  (`group_vehicle_by_driver[(group_key, driver.id)]`) is the same TYPE as
  what the current row needs, or if they have no established vehicle for
  that group yet. If a driver's group vehicle is a Chiller Truck and the
  next row in that group needs a Bus, the overlap check now runs
  NORMALLY for that driver -- if they're already busy at that time (as in
  the real bug), they're correctly excluded from candidacy, and the group
  naturally splits into two consistent driver-vehicle threads instead
  (matching the real ground-truth pattern from the earlier PLANNED.xlsx
  study -- one driver handles a group's Chiller-type rows, another
  handles its Bus-type rows).
- Confirmed the fix doesn't regress the legitimate cases: (1) genuine
  simultaneous SAME-vehicle-type orders in one group still both assign to
  one driver (the original point of the feature), and (2) a driver
  picking up a DIFFERENT vehicle type within the same group at a
  NON-overlapping time is still allowed (matches real ground-truth
  behavior seen previously, e.g. a driver doing Bus jobs all day plus one
  later Chiller job for the same event).
- New test: `tests/test_same_driver_vehicle_consistency.py` -- covers all
  three cases (the bug reproduction, the legitimate simultaneous case,
  and the legitimate non-overlapping switch case). Full existing suite
  re-run unchanged and still passing.
- **Also investigated, NOT confirmed as a bug, NOT fixed:** the project
  owner also flagged drivers ending up with very short days (e.g. a
  single 2h job) while other jobs sat unresolved. Checked one concrete
  case (a driver with only a 2h Chiller/DryTruck-type job assigned) against
  that day's unresolved jobs (Open Truck, 10 Ton Chiller, 14 Seater Bus,
  20 Seater Bus types) -- none appeared to match that driver's likely
  license, so this may be a correct license-mismatch outcome rather than
  a bug. The underlying architecture gap is real regardless and separate
  from the vehicle-consistency bug above: `_fill_gaps_with_unresolved_jobs()`
  (Phase 11) only helps a driver with a genuine BOUNDED gap (booking
  before AND after); it does nothing for a driver who is simply
  underutilized with wide-open capacity and no bracketing bookings.
  Logged as NEW-007 in the scheduling rules spec (now v8) -- flagged for
  the project owner's confirmation before building, since extending
  gap-fill to cover wide-open capacity (not just bounded gaps) is a real
  scope increase, not a quick tweak.
- Scheduling rules spec bumped to v8 (SD-004 added and resolved, NEW-007
  added as an open, unconfirmed question). `AI_INDEX.json`'s `allocate()`
  algorithm_summary updated to reflect the SD-004 fix.
## Phase 14b — Specialist-reservation ranking (NEW-007 sharpened and fixed) (2026-08-03, same day as Phase 10-13)

- Following up on the NEW-007 discussion from Phase 13, the project owner
  sharpened the diagnosis into a concrete principle: a driver licensed for
  ONLY one vehicle type is a non-substitutable resource for that type, and
  the engine should reserve their hours for it rather than spend them on
  work a more broadly-licensed driver could equally cover. Real example:
  a driver licensed ONLY for "10 Ton Chiller Truck" had that day's full
  set of Chiller requests (11h true/merged work) fit cleanly in their
  hour rule -- but an earlier, non-exclusive Chiller job landing on them
  first could burn just enough capacity to push a later exclusive request
  to supplier/unresolved, even with an idle qualifying driver available.
- **Fix:** for an ungrouped job with multiple qualifying candidates, the
  main loop's ranking now prefers the more broadly-licensed ("generalist")
  candidate over a narrowly-licensed ("specialist") one:
  `min(candidates, key=lambda d: (-len(d.license_types), d.occupied_seconds))`
  -- license breadth first, existing hours-fairness as the tiebreak.
  Confirmed this can't push anyone over their own ceiling: the hard-rule
  filter already excludes any candidate who'd violate their daily/monthly
  limit BEFORE this ranking runs, so a generalist is only ever preferred
  up to their own legal limit, then naturally drops out of candidacy.
- **First attempt applied this to ALL candidates including a "Same
  Driver" group's first-ever assignment, and it backfired** -- caught
  immediately by testing: since neither driver is in
  `group_drivers[group_key]` yet for a group's opening row, both fall
  into the same candidate pool this ranking touches, and it stole the
  group's first job away from the specialist toward the generalist,
  fragmenting a block of work that should have started and stayed on one
  driver (confirmed via `tests/test_specialist_reservation.py` failing
  with the exclusive block split across both drivers instead of staying
  on the specialist). Fixed by scoping the new ranking to ungrouped jobs
  only -- grouped jobs (both starting a new group and continuing an
  existing one) keep the original plain least-occupied ranking, which
  already handles group consolidation correctly on its own.
- **Side finding, not addressed:** while building the regression test,
  found a real interaction with HR-005 (the daily minimum-hours rule,
  Phase 10) -- if a generalist is given just one small ungrouped job via
  this new preference and nothing else fills their day, HR-005 tries to
  move that job elsewhere for being under-minimum, and releases it to
  unresolved if no one has room -- turning an "assignable but short" day
  into "unresolved," arguably a worse outcome. Worked around in the test
  by giving the generalist enough other same-day work to independently
  clear their own minimum, which cleanly isolates what this phase's fix
  is actually about. Not fixed -- logged in the spec (NEW-007) as tied to
  the still-open OPT-001 duty-span/unresolved-policy question.
- New test: `tests/test_specialist_reservation.py` -- reproduces the
  project owner's exact real scenario (shared job → generalist, full
  exclusive block → specialist, zero unresolved). Full existing suite
  re-run unchanged and still passing throughout.
- Scheduling rules spec bumped to v9 (NEW-007 updated with the sharper
  diagnosis, the fix, the group-scoping caveat, and the HR-005 side
  finding). `AI_INDEX.json`'s `allocate()` algorithm_summary updated to
  mention the specialist-reservation ranking and its group-scoping.

## Phase 14 — Hour-fairness fix: group-leader look-ahead + widened HR-005 repair pass (2026-08-06)

- The project owner reported the software wasn't reproducing the balance
  seen in a real human-planned PLANNED.xlsx: some drivers landed at 12h,
  others at 2h, on the same day, even after the SD-004 vehicle-consistency
  fix and the 0-minute travel buffer were already in place.
- **Root cause, confirmed by cloning the real repo and running `allocate()`
  against the real `UNPLANNED.xlsx` + `fleetplanner.db`, not guessed:**
  84% of that day's rows (37/44) carried a non-blank "Same Driver" value
  (one group per event). `_repair_minimum_daily_hours()` (HR-005,
  Phase 10) unconditionally skipped ANY driver-day containing a grouped
  job, on the reasoning (confirmed correct in Phase 12) that a group's own
  hours are sometimes genuinely the cause of a shortfall and shouldn't be
  force-split. But with the vast majority of rows grouped, that skip meant
  the daily-minimum safety net almost never ran at all -- whichever driver
  a group happened to land on (via the old "least occupied AT THIS
  INSTANT" pick for the group's first row) kept that group's hours
  regardless of how unbalanced the result was, with nothing downstream
  able to touch it.
- Confirmed with the project owner (two changes approved together, per
  Rule 16):
  1. **Look-ahead group-leader selection (SD-005).** A NEW group's opening
     row now picks its driver by projecting `occupied_seconds + the
     group's total merged hours` (precomputed once per group before the
     main loop, via the existing `_merged_hours()`), not just the opening
     row's own duration. Previously a driver could be picked for a group
     purely because they were idle for that ONE row, then end up carrying
     an 11-hour event nobody else had a chance to share, with no way to
     reconsider once picked (SD-002/SD-003 lock a group to its first
     qualifying driver). A group's second-and-later rows are unaffected --
     they still prefer the group's already-established driver first, per
     SD-002/SD-003.
  2. **HR-005 repair pass widened to move grouped days, not just skip
     them.** A driver's whole grouped day can now be moved to a single
     other qualifying driver (never split -- the "same driver" instruction
     is still honored), with SD-004 vehicle-type consistency enforced on
     the new driver via a new helper, `_established_group_vehicle_type()`.
     If no single driver can legally take the group, the day is still
     released to unresolved exactly as before -- that outcome is real and
     expected sometimes, not a bug.
- **A real second bug was found and fixed while building this, before it
  ever reached the project owner:** widening the repair pass to move whole
  groups caused genuine thrashing on the real dataset -- a short group
  would be moved onto a driver with room, but that driver's own day (now
  including the freshly-received group) could itself look under-minimum
  to the very next pass, so a DIFFERENT group would get moved onto them
  next, and so on; groups visibly bounced between drivers across the
  5-pass loop instead of settling (caught directly by tracing driver state
  across passes against the real file, not a synthetic guess). Fixed with
  a `settled_job_ids` stability guard, threaded through all 5 passes of
  one `allocate()` call: once a job has been moved by the repair pass, its
  identity is recorded and it is never moved again for the rest of that
  run. This trades a small amount of theoretical optimality for a
  guaranteed, deterministic stop -- consistent with this function's
  existing "best-effort heuristic, not a global optimizer" framing.
- **A third, smaller issue found the same way:** the repair pass's search
  for a new driver picked the FIRST feasible match in `driver_pool`'s
  natural (alphabetical) order, not the most-free one. On the real data
  this meant a driver who simply hadn't had their own under-minimum day
  resolved YET in the same pass could look "busy" (still holding their
  original jobs) and get skipped in favor of a driver who happened to
  have just been freed moments earlier by an unrelated fix, purely due to
  processing order. Fixed by searching candidates in
  least-occupied-hours-first order instead of list order.
- **Real-data result** (`UNPLANNED.xlsx` + real `fleetplanner.db`, same
  file used in Phase 12): driver-hour spread went from 2.0h-16.0h(summed;
  11.0h merged max) before this fix to 4.0h-11.0h after, with most
  drivers landing exactly at their 9h minimum. Real PLANNED.xlsx (human
  reference) spread was 5.0h-12.0h across 11 drivers for comparison. Not
  yet full parity -- a few drivers still land under 9h and two end up
  fully idle in this run -- but the specific reported symptom (12h vs 2h
  drivers on the same day) is substantially resolved.
- Full existing test suite (`test_daily_overtime_ceiling.py`,
  `test_gap_filling.py`, `test_hour_accounting.py`,
  `test_license_and_hours.py`, `test_same_driver.py`,
  `test_same_driver_supplier.py`, `test_same_driver_vehicle_consistency.py`,
  `test_shift_period.py`, `test_specialist_reservation.py`,
  `test_travel_buffer.py`) re-run unchanged and still passing throughout
  every fix in this phase. (`test_shift_start.py` remains a pre-existing
  dead leftover from before the shift_period redesign -- its imports
  reference functions removed in an earlier session; unrelated to this
  work, flagged for cleanup.)
- **Two real, separate data-quality issues surfaced by this investigation,
  NOT fixed here (would need project-owner confirmation + are data fixes,
  not code fixes, per the project's established convention -- see
  NEXT_SESSION.md "Common mistakes to avoid"):**
  1. `drivers.license_types` for at least one driver contains a literal
     embedded newline before "(with lift)" in
     `"4.2 Ton Double cabin Open Truck\n(with lift)"`, while the job side
     is newline-normalized via `excel_import._clean_text()`. This is the
     same "Seated" vs "Seater" class of silent zero-match failure
     documented in AI_CONTEXT.md Section 6/9 -- it accounts for at least
     3 of this run's 8 non-fairness-related unresolved rows.
  2. Very few drivers hold a "10 Ton Chiller Truck (with lift)" license in
     the current database, causing several more unresolved rows that
     aren't a fairness or algorithm issue -- genuine roster/licensing
     scarcity.
- **Open concern raised by the project owner immediately after this
  phase, not yet resolved:** removing every "Same Driver" value entirely
  from a real test file made results WORSE (12 unresolved), confirming
  the feature is genuinely load-bearing -- but the project owner feels
  grouping is currently "ruling" the allocation rather than assisting it,
  and separately wants driver selection to be merit-based (a pre-computed
  ranking consulted by the engine) rather than today's implicit
  alphabetical-order artifact in the repair pass's candidate search. See
  NEXT_SESSION.md for the fuller discussion -- nothing decided or built
  yet, deliberately, per Rule 16.
- Scheduling rules spec bumped to v10 (SD-005 added, HR-005/SE-003/VAL-004
  updated with real before/after numbers, NEW-008 added). `AI_CONTEXT.md`
  (Overtime model, Same Driver column, bug list item 13, Section 12) and
  `ARCHITECTURE.md` (group-leader selection pseudocode, HR-005 post-pass
  description) updated to match, per Rule 14/17.

## Phase 15 — Two new allocation strategies (allocate_by_merit, allocate_by_anchor), idle-driver rescue, and a full data-layer cleanup (2026-08-06, same day as Phase 14)

Triggered by the project owner raising two structural concerns right after
Phase 14: (1) "Same Driver" grouping now feels like it's "ruling" the
allocation rather than assisting it, though deleting it entirely made
results WORSE (12 unresolved) -- confirming it's genuinely load-bearing;
(2) driver selection is effectively alphabetical in places, not
merit-based. Rather than patch `allocate()` further, the project owner
asked for real alternative strategies to be built and compared against
real data -- this phase is that work, done incrementally (Rule 13),
alongside the existing `allocate()`, never replacing it.

### `allocate_by_merit()` -- shift-partitioned, event-diverse seeding

Confirmed design, in order: (1) pre-merge Same-Driver row PAIRS (never
3+) sharing a vehicle type and start/end within 1h into one internal
`PlanningUnit`, turning the genuinely-simultaneous case into a single
decision instead of a runtime overlap-relaxation special case -- export
still shows every original row separately; (2) partition into
morning/evening driver and job pools, solved mostly independently; (3)
SEED each driver's first job diversified by EVENT, not raw job order --
prefer covering a new event over a second job from one someone else
already started, confirmed by the project owner as valuable because it
keeps an event's footprint on as few drivers as possible, making it
easier to hand to a supplier later if needed; license-scarcity ALWAYS
overrides seeding -- the only qualified driver takes a job immediately,
confirmed explicitly ("they take it immediately if there is no conflict
with his timing and ceiling"); (4) FILL everything else, least-occupied
first. New dataclass `PlanningUnit` (jobs: list of 1 or 2 Job objects,
start_dt, end_dt, vehicle_type_required, same_driver_key, event_id) and
helper `build_planning_units()` implement the pre-merge.

Real-data result (`UNPLANNED.xlsx` + `fleetplanner.db`): WORSE than the
Phase 14 baseline -- 16 unresolved vs. 12, because the aggressive
event-diverse seeding spends driver availability faster than the
rearrangement stage can recover it. Disclosed honestly to the project
owner rather than oversold. Kept in the codebase as a real, tested
alternative, not wired into the UI.

### `allocate_by_anchor()` -- anchor-first-and-last-job, most-constrained drivers first

A second idea from the project owner, built the same session: instead of
hoping a good day shape falls out of one-job-at-a-time decisions, size
each driver's day intentionally. For each driver (narrowest license
first, via `_driver_ordered_most_constrained_first` -- so specialists
claim their anchors before generalists absorb whatever's left): give them
their earliest-available qualifying job as the FIRST anchor, then compute
their target finish time (first job's end + their ceiling, via
`_driver_ceiling()`) and search for whichever remaining job ends closest
to -- but not after -- that target as the LAST anchor. Confirmed with the
project owner: `working_hours_per_day` is the floor once a driver is used
at all; `max_working_hours_per_day` is the ceiling; left blank, the
ceiling equals `working_hours_per_day` itself (a fixed day, not a range)
-- confirmed against the real `PLANNED.xlsx`, where the only two drivers
landing at exactly 9h are precisely the two with no max configured.
Everything else in the middle goes to whoever it fits (least-occupied
first). Then a bounded SWAP REPAIR (`_swap_repair()`, capped at 3 rounds
by default): for each still-unresolved unit, look for a driver who could
take it if exactly ONE of their existing single-job, ungrouped
assignments moved elsewhere, and only commit the swap if that displaced
job actually finds a legal new home -- a strict, verified improvement,
never a net-zero shuffle. Supplier fallback and the usual rearrangement
safety net run after.

**Real bug found and fixed before this reached real testing:** the
supplier-fallback branches in `allocate_by_anchor()` set
`assigned_supplier_unit` but never reset `job.unresolved` back to
`False` (a leftover from marking every pre-swap leftover unit
provisionally unresolved) -- successfully-supplied jobs were silently
still counted as unresolved. Fixed by adding the missing
`j.unresolved = False` in both the reuse and new-hire commit paths.

Real-data result: 12 unresolved (tied with baseline), but with a real,
verified improvement in driver utilization -- 9/9 active drivers had
real work (2h-12h), vs. baseline's 9/11 with 2 fully idle. Confirmed the
anchor-first design is directionally sound even though it hasn't beaten
the baseline on raw unresolved count yet.

### `_rebalance_idle_drivers()` -- "every driver has real work" as a first-class goal

Confirmed with the project owner after a direct three-way comparison
against the real `PLANNED.xlsx` (which uses all 11 drivers, none below
7.5h) showed both `allocate()` (with the Phase 14 fix) and
`allocate_by_merit()` leaving 2 drivers at a fully idle 0h -- legal under
HR-005 (a driver "not used at all" has no minimum to violate) but not
what the real planner does. 0h drivers were themselves usually a SIDE
EFFECT of `_repair_minimum_daily_hours` freeing a short day entirely to
fix someone else's minimum, with nothing afterward ever revisiting that
now-idle driver. New pass: for every 0h driver who's otherwise available,
tries to accumulate enough work to reach THEIR OWN minimum -- first from
anything still unresolved (free), then from genuine SURPLUS on other
drivers (hours above their own minimum, never dropping a donor below it).
Nothing is committed to real driver/job state until the FULL accumulated
total for one idle driver clears their minimum; if it can't be reached,
everything tentative is discarded and the driver is correctly left at a
legal 0h. Wired into the rearrangement loop of both `allocate()` and
`allocate_by_merit()`/`allocate_by_anchor()`, sharing the same
`settled_job_ids` stability guard as `_repair_minimum_daily_hours`.

**Two real bugs found and fixed while building this, via direct testing
against the existing test suite, before either reached the project
owner:**
1. **Oscillation with a genuinely unfixable short day.**
   `tests/test_daily_overtime_ceiling.py`'s single-driver 5h-day case
   (correctly released to unresolved by the repair pass, since no other
   driver exists to absorb it) started failing: the newly-idle driver
   immediately "rescued" the exact same job right back from the
   unresolved pool, undoing the repair pass's correct decision. Root
   cause: the first version of this function committed the first
   feasible job it found without checking whether the TOTAL accumulated
   hours actually reached the idle driver's minimum. Fixed by making the
   whole accumulation tentative (never touching real state) until the
   full total clears the minimum -- if it can't, the tentative set is
   simply discarded, which is free since nothing was ever committed.
2. **Donor remaining-hours miscalculation.**
   `tests/test_daily_overtime_ceiling.py`'s `jobs7` consolidation case
   (a 3h short day correctly moved onto a driver with room, making a
   legal 12h day) started failing: the idle-rescue pass then pulled the
   ORIGINAL 9h job back off the consolidated driver onto the now-idle
   one, undoing the consolidation. Root cause: the donor's "remaining
   hours after removal" calculation excluded any of the donor's jobs
   that were already marked `settled_job_ids` (correctly excluded from
   being RE-MOVED) but incorrectly also excluded from the baseline
   workload used to judge whether pulling something ELSE would leave the
   donor short -- so a donor who still had a settled 3h job looked like
   they'd drop to 0h (legal) when they'd actually be left with an
   illegal 3h day. Fixed by using two separate lists: the donor's TRUE
   current workload (all their jobs, settled or not) for the
   remaining-hours check, and a separate, narrower "pullable" list
   (excluding settled jobs) for what's actually eligible to move.

Full existing test suite re-run and passing after both fixes.

### Data-layer cleanup: embedded newlines, fleet-wide

Prompted by the project owner asking to fix the two flagged data issues
(NEW-008's newline bug, and the 10-Ton-Chiller scarcity) before further
algorithm tuning. Investigation found the newline issue was NOT isolated
to one driver as first suspected (Phase 14) -- it was present in **all
11 active drivers' `license_types`** (clearly copy-pasted from the same
wrapped Excel cell into every record), **plus one vehicle's
`vehicle_type`** (plate `Z 43915`), **plus two excluded/inactive
drivers**. A full sweep across `drivers.license_types`,
`vehicles.vehicle_type`, and `supplier_offerings.vehicle_type` found and
fixed all of it (14 records total); a second full sweep afterward
confirmed zero embedded newlines remain anywhere in the database. This
was a pure data correction, not a code change -- consistent with the
project's established rule that text-matching failures get fixed at the
data layer, never by loosening `_type_matches()`.

The 10-Ton-Chiller-Truck scarcity was investigated and found to be a
GENUINE capacity constraint, not a data bug: only one physical vehicle of
that type exists in the fleet (plate `A 67338`), and no supplier offers
that exact type either (the closest offerings are "10 Ton Dry Truck",
different, or "5 Ton Chiller Truck", different capacity). Reported to the
project owner as a real gap they may want to close (a second vehicle, or
a supplier offering) rather than something the software could
legitimately work around.

**Real-data result after the fix:** confirmed real, targeted
improvement -- 2 of the 3 previously-unresolved "Open Truck" rows now
resolve correctly in the baseline and `allocate_by_anchor` runs, exactly
as expected. But the TOTAL unresolved count did not drop, and ticked up
slightly in all three strategies (baseline 12->13, `allocate_by_anchor`
12->14, `allocate_by_merit` 16->20) -- not a new bug, but the honest
signature of a greedy heuristic: giving every strategy MORE legal options
earlier in the process reshuffled the whole downstream allocation and
created a DIFFERENT shortfall elsewhere (shifting from Manpreet to
Muhammad Atif/Imran Pasha in most runs). This was disclosed to the
project owner plainly: the data layer is now clean, and the remaining
gap to zero-unresolved is confirmed to be an algorithm-sophistication
limit, not a data problem.

### Three-way comparison, final state this session

| | `allocate` (baseline) | `allocate_by_merit` | `allocate_by_anchor` |
|---|---|---|---|
| Unresolved (post data-fix) | 13 | 20 | 14 |
| Drivers used | 9/11 | 8/11 | 9/11 |
| Hour spread | 2.0-11.0h | 2.0-12.0h | 2.0-12.0h |

`allocate_by_merit` is the clear underperformer on real data.
`allocate()` (baseline) and `allocate_by_anchor` are close; anchor gives
better driver utilization (before the data fix, 9/9 vs. baseline's 9/11
with 2 idle) at the cost of one more unresolved job. None of the three
new/modified strategies are wired into the UI yet -- `plan_day_tab.py`
still calls `allocate()` only. This is a deliberate checkpoint, not a
finished feature -- see NEXT_SESSION.md for what's still open.

Full existing test suite (`test_daily_overtime_ceiling.py`,
`test_gap_filling.py`, `test_hour_accounting.py`,
`test_license_and_hours.py`, `test_same_driver.py`,
`test_same_driver_supplier.py`, `test_same_driver_vehicle_consistency.py`,
`test_shift_period.py`, `test_specialist_reservation.py`,
`test_travel_buffer.py`) re-run and passing after every change in this
phase, including both idle-rescue bug fixes above. No new formal test
files were added yet for `allocate_by_merit`, `allocate_by_anchor`,
`_swap_repair`, or `build_planning_units` specifically -- all validation
this phase was direct real-data testing against `UNPLANNED.xlsx` +
`fleetplanner.db`, not synthetic unit tests. Flagged as a real gap in
NEXT_SESSION.md: these new code paths deserve the same synthetic test
coverage as everything else in this project before being considered
production-ready, even though real-data testing caught two genuine bugs
already.

## Phase 16 — Swap-repair widened to whole groups + multi-hop chains, shift rule corrected to first-job-only, and a new CP-SAT solver strategy reaching 0 unresolved / 0 supplier on real data (2026-08-09)

- **Starting point:** `allocate_by_anchor()` was at 14 unresolved jobs
  against the real `UNPLANNED.xlsx`, following Phase 15's experimental
  work. The project owner asked to keep pushing toward zero, offering to
  either propose a new idea or let the assistant continue technically.
- **Diagnostic first, per this project's established practice:** before
  changing anything, every unresolved job was classified as either
  genuinely vehicle-capacity-bound (no matching vehicle exists anywhere,
  in-house or supplier) or driver/timing-bound (a matching vehicle DOES
  exist, something about the current allocation is just blocking it).
  Result: only 1 of 14 (a "14 Seater Bus" job) was a genuine ceiling at
  that point; the other 13 were theoretically fixable, split roughly
  8 stuck in "Same Driver" grouped pairs `_swap_repair` couldn't move,
  and 5 genuine "no resource available at this exact moment" conflicts.

### Data correction
- The project owner spotted, via a screenshot, that vehicle `A 68982`
  was entered as `"14 Seater Van"` in the test database when the actual
  vehicle is a `"14 Seater Bus"` -- a real, exact-string-matching data
  issue of exactly the kind this project has repeatedly flagged as a
  process problem, not a code bug (see AI_CONTEXT.md Section 6,
  "Vehicle-type matching"). Corrected via `db.update_vehicle()` in the
  test database. (The project owner's own live database has the same
  issue and was left for them to fix manually via the Vehicles tab --
  not something this session touched.)

### `_swap_repair` widened: whole-group displacement, direct-fit, multi-hop chains
Three real, separable gaps were found and fixed in sequence, each
confirmed against real data before moving to the next:

1. **Whole-group (bundle) displacement.** The original `_swap_repair`
   (Phase 15) could only displace a single, ungrouped unit to make room
   for an unresolved one -- a Same-Driver group stuck in the wrong place
   had no path to being moved at all, since nothing in the function knew
   how to treat a group as one atomic thing to relocate. Fixed by adding
   `_bundle_units_for_driver()` (returns every displaceable "bundle" on a
   driver -- each ungrouped unit alone, or a WHOLE Same-Driver group's
   units together, never split, matching the same whole-group-move
   principle HR-005's repair pass already uses) and `_bundle_fits_driver()`
   (checks a whole bundle's feasibility on a candidate driver, batch-wise,
   so two individually-legal moves can't jointly bust a daily ceiling).
   `_unit_driver_feasible()` and `_find_vehicle_for_unit()` were both
   generalized to accept an optional override/tentative interval list so
   this batch-checking could reuse them instead of duplicating logic.
2. **Direct-fit check (a real, separate oversight, not a tuning issue).**
   Testing the fix above against real data surfaced a second, more basic
   gap: `_swap_repair` only ever thought in terms of *displacement* -- it
   never checked whether a driver already had genuine free capacity (e.g.
   freed up by an HR-005 release earlier in the same run) and could just
   take the unresolved unit directly, no swap needed at all. Traced with a
   step-by-step instrumented run showing a driver (IMRAN PASHA) drop to
   1.0h occupied via an HR-005 release, remain fully eligible for the
   stuck job by every hard rule, and STILL never get it, because nothing
   in `_swap_repair` ever asked "does this driver already have room?"
   before jumping straight into displacement logic. Fixed by adding a
   direct-fit check as the very first thing tried per candidate driver.
3. **Bounded multi-hop chain search.** Even with (1) and (2), several
   jobs stayed unresolved specifically because every driver was
   individually blocked -- but only because each was blocking someone
   else, in a way a single-hop swap can't see. Confirmed directly: 20
   separate jobs overlapped one 4-hour window against only 11 drivers,
   several spanning many hours each. Added `_try_place_bundle_chain()`
   (a classic bounded-depth augmenting-path search, the same idea used in
   textbook bipartite-matching algorithms: find a home for the displaced
   bundle, or -- if depth remains -- displace ONE of THAT driver's own
   bundles and recursively find a home for it too, chaining through as
   many drivers as `SWAP_REPAIR_CHAIN_DEPTH` allows, each driver visited
   at most once so it can never cycle) and `_commit_chain()` (commits an
   entire found chain atomically -- release every bundle from wherever
   it's currently sitting first, then land each on its new home).
   **A real double-release bug was found and fixed while wiring this in:**
   `_swap_repair` pre-released the outermost bundle from its origin driver
   before calling `_commit_chain`, which ALSO tries to release that same
   bundle (reading its now-already-cleared `assigned_driver_id`), raising
   a `KeyError`. Fixed by removing the redundant pre-release -- `_commit_chain`
   already handles it correctly on its own.
- Also widened `allocate_by_anchor()`'s outer loop: previously a single
  `_swap_repair` call ran once, before the gap-fill/HR-005/idle-rescue
  rearrangement loop, meaning any job released back to unresolved by a
  LATER rearrangement pass never got a second swap attempt. Restructured
  into an outer loop (rearrange up to 6x, then swap-repair, repeat up to
  10x, stopping early once nothing changes in a full round) so a release
  from a later pass can still trigger a fresh swap-repair attempt.
- **A second, independent real bug found via this same investigation:**
  `_repair_minimum_daily_hours`'s inner `_release()` helper matched a
  busy interval to remove by `(start, end, tag)` where `tag` was the RAW
  `same_driver_key` -- but the interval had originally been stored with
  the EFFECTIVE group key, which SD-004's vehicle-consistency rule can
  set to `None` even for a job that DOES have a `same_driver_key` (if its
  vehicle type didn't match what the group had already established on
  that driver). When the two didn't match, the release silently failed
  to find anything to remove, leaving a PHANTOM busy interval behind
  forever -- confirmed directly: a vehicle showed a busy interval with no
  job behind it at all, permanently blocking that time slot for anyone.
  Fixed by matching on `(start, end)` only, the same safe pattern
  `_release_unit()` already uses elsewhere in this module -- proven safe
  here too, since this function only ever touches ungrouped jobs (grouped
  days are explicitly skipped), and two different ungrouped jobs can
  never legitimately share an identical `(start, end)` on the same
  driver in the first place.
- Combined effect of all of the above, tested against real
  `UNPLANNED.xlsx`: **14 unresolved → 2 unresolved, 0 supplier, all 11
  drivers used** (up from 9/11 idle-driver waste before this phase).

### Shift rule corrected: gates only the FIRST job of the day, not every job
The project owner supplied a real human-planned file (`PLANNED_1.xlsx`,
the source `UNPLANNED.xlsx` was originally stripped from) as ground
truth and, cross-checking it against the test database, a genuine
business-rule mismatch surfaced: `DEEPAK DEWAN` is configured
`shift_period='morning'` in the database, yet the real plan gives him a
job at 16:00-19:00 -- a job our engine's window check (before this fix)
would have refused outright, since it re-checked EVERY job against the
morning/evening window, not just the first one of the day. Confirmed
directly with the project owner: "if a driver is in morning shift 07:00
that means his shift will end 16:00 if the 12hr max field is empty...
he can definitely get a job or two after 12:00." **The real rule: the
shift window only gates a driver's FIRST job of the day; once they have
any job on a given date, later jobs that day are governed purely by the
normal overlap/hour-ceiling rules.**
- Implemented via a new optional `busy_intervals` parameter on
  `_job_matches_shift_period()`: if the driver already has any interval
  starting on the same calendar date as the job being checked, the
  window check is skipped entirely (already working this day); if not,
  the normal window check applies (this IS effectively their first job).
  `busy_intervals=None` (the default) preserves the original
  always-enforce behavior, so any call site not explicitly updated fails
  safe rather than silently gaining the relaxation.
- **Every one of the 7 call sites across the whole engine was updated**
  to pass real (or tentative/hypothetical, where the call site is doing
  a batch feasibility check) interval state, not just the main
  `allocate()` loop: `_fill_gaps_with_unresolved_jobs`,
  `_repair_minimum_daily_hours` (passing the tentative-within-batch
  `combined_busy`, correctly reordered to compute before the shift check
  rather than after), `_rebalance_idle_drivers` (passing the
  within-rescue `accumulated` intervals, so a SECOND job tentatively
  added to the same idle driver in one rescue attempt also gets the
  continuation relaxation), the shared `_unit_driver_feasible()` (used by
  `allocate_by_merit`, `allocate_by_anchor`, `_swap_repair`, and the new
  chain search -- this one had the override parameter already threaded
  through for the overlap/hours checks from Phase 15, but the shift
  check itself had been left calling the old 2-argument form; fixed by
  reordering so the resolved `busy_intervals` value is computed before
  it, not after), and `_swap_repair`'s own direct main-loop check
  (passing `reduced`, the hypothetical post-displacement schedule, not
  the driver's raw current one).
- Combined with the swap-repair widening above, tested against real
  `UNPLANNED.xlsx`: **2 unresolved → 0 unresolved, 0 supplier, all 11
  drivers used** via `allocate_by_anchor()`.

### New strategy: `allocate_by_solver()` -- Google OR-Tools CP-SAT
With `allocate_by_anchor()` reaching 0/0 on the original file, the
project owner supplied the real ground-truth file directly and pointed
out a genuine, harder scheduling puzzle within it (Sheet 2's driver-hour
summary): every driver's day should land strictly within their
configured floor/ceiling, but real days ranged from 0h to 7h of idle
time with no consistent balancing logic -- e.g. moving one specific job
between two specific drivers would give both of them exactly 4h idle
instead of one having 1h and the other more. The project owner
explicitly asked what METHOD real fleet/crew-scheduling systems use for
this class of problem, having noticed the heuristic approach kept
surfacing one subtle bug at a time as new edge cases came up (three
found and fixed in roughly two hours during the work above). Answer:
constraint programming / mixed-integer programming solvers -- state the
hard rules as constraints and the actual goal as an objective, and let
the solver search the space directly and provably, rather than writing
one heuristic pass at a time. Confirmed with the project owner (OR-Tools
CP-SAT: free, open-source, solves a problem this size in well under a
second, adds one new dependency) before building.
- **New function `allocate_by_solver()`** (plus helper
  `_solver_effective_ceiling_minutes()`), added alongside the other
  three strategies, not replacing any of them (Rule 1/13). `ortools`
  added to `requirements.txt`; imported LAZILY inside the function (not
  at module level) so the rest of the app -- and the other three
  strategies -- continue to work with zero impact if `ortools` isn't
  installed.
- **Model:** one boolean `x[unit, driver]` per license-compatible
  (unit, driver) pair, `veh[unit, vehicle]` per type-compatible
  (unit, vehicle) pair, `unresolved[unit]` per unit. Hard constraints:
  exactly one of {assigned to one driver, unresolved} per unit; a
  vehicle assigned iff a driver is (and not at all for "Driver Only"
  rows); no two time-overlapping units share a driver or a vehicle;
  shift window as a first-job-only gate (encoded via `has_morning`/
  `has_evening` linking variables per driver: if a "morning" driver has
  ANY evening-window unit assigned, they must also have at least one
  morning-window unit, i.e. their real first job -- symmetric for
  "evening" drivers picking up an early-morning-window unit, e.g. an
  overnight job rolling past midnight); daily ceiling folding in the
  monthly-overtime-budget interaction (`_solver_effective_ceiling_minutes`
  replicates `_unit_driver_feasible`'s existing two-layer logic: a blank
  monthly-overtime budget collapses the effective ceiling down to the
  daily floor regardless of what the daily-ceiling field says, matching
  the project's established "blank monthly overtime = zero daily
  overtime" precedent); daily floor, WITH the same Same-Driver-group
  exemption `_repair_minimum_daily_hours` already has (see next
  subsection -- a real gap found and fixed during this same phase).
- **Same-Driver group cohesion (soft preference, not a hard rule):**
  `build_planning_units()`'s pairs-only pre-merge already handles the
  dominant real pattern (two simultaneous same-vehicle pickups) as a
  single decision, needing nothing extra. But group members that never
  got pre-merged -- a group of 3+ rows, or members that simply don't
  overlap each other in time at all -- had no incentive in the first
  version of this model to land on the same driver, even when nothing
  hard was stopping them. Confirmed as a real, not theoretical, gap:
  testing against the real ground-truth file directly showed a 4-row
  group (13:00-15:00, 17:00-19:00, and a 23:00-01:00 pair, none
  overlapping each other) kept entirely on one driver in the real plan
  purely as a preference; the solver's first version left one row of
  that exact group unresolved. **A first encoding attempt (pairwise
  `together[unit_i, unit_j, driver]` booleans, one per pair of same-group
  units per shared candidate driver) measurably slowed the solver down --
  confirmed by direct timing: wall time went from well under a second to
  over 60 seconds without even reaching a proven-optimal status, because
  pairwise terms create a lot of symmetric, equally-good alternative
  combinations for the search to sift through.** Replaced with a leaner
  `touches_group[group, driver]` encoding (minimize the number of
  DISTINCT drivers touching each group, O(members) linking constraints
  per group instead of O(members²)) -- restored fast, proven-optimal
  solves. **Lesson for future modeling work in this codebase:** when
  adding a soft preference to a CP-SAT model, prefer an encoding that
  scales linearly with group size over one that scales quadratically,
  even when both are logically equivalent -- the solver's search
  difficulty is not just about constraint count, it's about how much
  symmetry the encoding introduces.
- **Warm-start hint:** runs `allocate_by_anchor()` on independent scratch
  copies of the drivers/vehicles/jobs (via `dataclasses.replace` for
  fresh runtime state and `copy.deepcopy` for the jobs, so nothing leaks
  into the real objects this function goes on to mutate) and feeds the
  result to the solver via `model.add_hint()` before the real solve --
  purely a starting point, never constraining what the solver is free to
  find instead. **A real API bug hit while wiring this in:** OR-Tools'
  camelCase `model.AddHint(...)` is a deprecated alias whose compatibility
  shim breaks when passed list arguments in this installed version
  (`ortools==9.15`), raising a confusing `TypeError` deep inside the
  library. The real, current method is snake_case `model.add_hint(var,
  value)`, called once per (variable, value) pair, not with lists --
  confirmed by reading the actual library source directly rather than
  guessing from the error message.
- **A second real gap found and fixed during this phase, this time in
  the daily-floor constraint specifically:** the project owner clarified
  the intended semantics directly -- floor and ceiling should be
  strictly enforced for a used driver ("never less than 9, never more
  than 12"), but pointed at the real ground-truth file's own
  `VISWANADHAN` (5.0h that day, well under his 9h floor) as proof this
  isn't actually absolute in practice. Checked directly: every one of
  VISWANADHAN's real jobs that day was inside ONE Same-Driver group --
  exactly the existing, already-implemented HR-005 exemption from
  `_repair_minimum_daily_hours` (Phase 12: "if ANY of those jobs are
  grouped, the day is... recognized as unfixable-without-touching-a-
  protected-group and left alone entirely"), which the new solver simply
  hadn't replicated yet. Fixed by making the floor constraint's
  enforcement conditional on BOTH the driver being used AND having ZERO
  grouped units that day (`floor_applies = used AND NOT has_grouped_unit`,
  encoded as a small reified conjunction) -- the ceiling still applies
  unconditionally either way; only the floor gets this exemption,
  matching the other three strategies exactly.
- **Objective**, weighted so each tier is never traded away for a lower
  one: `1,000,000 × unresolved_count + 10,000 × distinct_group_driver_touches
  + unused_capacity_among_used_drivers − used_driver_count_bonus`.
- **Supplier fallback is NOT modeled inside the solver.** Dynamically
  naming/numbering hired supplier units the way the solver would need to
  is a materially different kind of combinatorial problem, and modeling
  it exactly would roughly double the size of this change for a part of
  the pipeline that's already solved well. Instead: whatever the solver
  leaves unresolved is run through the exact same reuse-before-hire
  dynamic-labeling logic `allocate()`'s supplier pass already uses,
  copied (not reimplemented) directly into this function, per Rule 1.
- **Disclosed scope boundary, not a silent gap (Rule 16):** genuine
  overlap-relaxation for Same-Driver group members beyond what
  `build_planning_units()` already pre-merges is NOT modeled -- if two
  different units share a `same_driver_key` AND genuinely overlap in
  time, they're still constrained with ordinary overlap rules here and
  can never legally land on the same driver, even though the real
  engine's SD-004-aware relaxation would allow it for a matching vehicle
  type. The dominant real-world case (two simultaneous same-vehicle
  pickups) is unaffected, since that's exactly what pre-merging already
  handles as one decision; what's excluded is specifically 3+-way
  simultaneous group overlaps, a rarer pattern not yet confirmed as a
  real problem worth the added complexity.
- **Final validated result against real `UNPLANNED.xlsx` + real
  `fleetplanner.db`, after every fix above: 44/44 jobs resolved, 0
  supplier hires, all 11 drivers used, solver status PROVEN OPTIMAL
  (not just "found a solution" -- CP-SAT's branch-and-bound genuinely
  proves no better assignment exists under the modeled constraints),
  3.78 seconds wall time.** This matches the real human-planned ground
  truth's own headline result (44/44, 0 supplier, all 11 drivers), though
  not the exact same per-driver hour distribution -- expected, since
  there is more than one valid way to reach 44/44 given the hard rules
  as configured.
- Full existing test suite re-run and passing after every fix in this
  phase (the pre-existing, unrelated stale `test_shift_start.py` also
  failed throughout this phase's work -- since confirmed genuinely dead
  and deleted, along with the equally stale `CHANGED_FILES_MANIFEST.txt`
  leftover from the 2026-08-03 session that had already flagged it for
  removal; the full suite now runs 100% clean with no skip-list needed).
  No new formal synthetic test files were added for
  `_bundle_units_for_driver`, `_try_place_bundle_chain`, or
  `allocate_by_solver()` specifically -- all validation this phase was
  direct real-data testing, same caveat carried over from Phase 15.
- Scheduling rules spec bumped to v12. `AI_CONTEXT.md`, `ARCHITECTURE.md`,
  `BUSINESS_RULES.md`, `NEXT_SESSION.md`, `AI_INDEX.json`, and this file
  updated per Rule 14/17. (`DATABASE.md` and `WORKFLOWS.md` checked and
  needed only a minor note -- no schema or workflow-level change this
  phase, purely algorithm and a new optional dependency.)

## Phase 19 — Optional Supplier Rows in Summary Table (2026-08-10)

- Updated `DriverSupplierSummaryDialog` so the detailed table is no longer
  permanently restricted to in-house drivers.
- Added an `In-house drivers only` checkbox, enabled by default. Unchecking it
  shows supplier records as well, using the same result-only data source.
- When all records are displayed, the table always orders the complete
  `IN-HOUSE DRIVERS` group first and the `SUPPLIERS` group second, with explicit
  group headers.
- Supplier rows now include first-job start, last-job end, duty span, trip count,
  and merged worked hours calculated from the current planning results.
- No database, allocation-engine, API, or surrounding Plan a Day UI changes were
  introduced.

## Phase 19b — In-house Trip Count Fix in Summary Header (2026-08-10)

*(Numbering note: this entry was originally committed as a second, duplicate
"Phase 20" prepended at the very top of this file, ahead of even the Phase 0
entry -- a real documentation-hygiene slip, not a content error. Corrected here
by moving it to its actual chronological position (after Phase 19, before
Phase 18 in this file's existing -- if unusual -- ordering for the summary-
popup cluster) and renumbering it out of collision with the real Phase 20
below, following the same fix pattern already used once before in this project
for the Phase 14/14b collision. See NEXT_SESSION.md for the standing reminder
this prompted about keeping changelog entries appended in one place.)*

- Corrected the second summary metric card so it reports **In-house trips** only.
- The value is calculated from the current in-memory result assignments where a
  driver is assigned, rather than using the total number of jobs (which includes
  supplier and unresolved jobs).
- The separate footer `Total trips` remains the overall trip/job count.
- No allocation, database, API, export, or surrounding UI behavior was changed.

## Phase 18 — Modern Summary Table Refinement (2026-08-10)

- Refined the existing `DriverSupplierSummaryDialog` without changing the
  planning engine, database, or surrounding Plan a Day UI.
- Replaced the individual driver/supplier result cards with a structured
  in-house driver table containing driver name, first-job start, last-job end,
  duty span, trips, and total worked hours.
- Retained the modern four-card header for in-house drivers, trips, suppliers,
  and supplier trips.
- Removed repeated aggregate totals from the bottom of the popup. The footer now
  shows only `Total trips` and `Unresolved trips`, followed by Close.
- Kept all calculations result-only: the popup still reads exclusively from
  `PlanDayTab.self.jobs` and performs no database or API calls.
- Updated the architecture and workflow documentation to describe the refined
  summary presentation.

## Phase 17 — Result-only Driver & Supplier Summary Popup (2026-08-10)

- Added a **Summary** button beside **Export Filled Excel** on the Plan a Day
  screen. The existing UI layout and controls were otherwise left unchanged.
- Added a read-only `DriverSupplierSummaryDialog` styled as a compact report
  popup matching the supplied visual reference.
- Summary data is calculated **only from the current `PlanDayTab.self.jobs`
  result objects**, never from SQLite/master data, so it represents exactly the
  plan currently on screen.
- The popup reports total trips, unique in-house drivers present in the results,
  unique suppliers used, supplier-assigned trips, and unresolved jobs.
- Each assigned in-house driver shows first-job/last-job duty span, trip count,
  and merged worked hours. Overlapping result intervals are counted once,
  consistent with the deterministic engine's occupied-hour accounting.
- Supplier details are derived from result labels/IDs, including `SAME ...` and
  numbered supplier-unit labels, without another database lookup.
- The feature is read-only: opening/closing the popup does not change jobs,
  assignments, history, APIs, or the uploaded workbook.
- No database schema changes, allocation-engine changes, or external API calls
  were introduced.
- `AI_INDEX.json` was reviewed and does not require a folder/module entry
  change because the feature extends the existing `plan_day_tab.py` module
  rather than adding a new module or changing the architecture boundaries.

## Phase 20 — Final Summary Popup Visual Refinement (2026-08-11)

- Removed the `In-house drivers only` checkbox from the Summary popup. The table
  now always presents both resource populations when present, with `IN-HOUSE
  DRIVERS` first and `SUPPLIERS` second.
- Reduced the popup width and increased its height/compact row sizing so up to
  approximately 15 in-house driver rows can be visible at once on a normal
  desktop display.
- Kept the existing structured columns: driver/supplier, first job start, last
  job end, duty span, trips, and total worked hours.
- Added visual icons to the four metric cards. In-house drivers and suppliers use
  a matching line-art icon family; in-house trips and supplier trips use the
  supplied trip clipart. The clipart is bundled as `app/ui/trip_clipart.png` and
  loaded relative to `plan_day_tab.py`.
- Preserved the result-only architecture: no database reads, allocation changes,
  API calls, or changes to the surrounding Plan a Day workflow.
- Updated architecture/workflow documentation to record the final popup behavior.

## Phase 21 — Duty-span correction: the daily hard rule (and monthly overtime) measure SPAN, not summed job duration (2026-08-10)

- **The project owner corrected a foundational misunderstanding, with a
  concrete example.** AALIM has `working_hours_per_day=9`,
  `max_working_hours_per_day=12` -- a hard rule that should mean: if used
  at all, his day must be >=9h and <=12h, measured strictly by **duty
  SPAN** (first job's start to last job's end). Separately, if he's given
  3 jobs summing to 8 actual worked hours (2h+3h+3h) inside that span,
  that 8h figure ("hours worked") has **no hard rule of its own at all**
  -- it's purely a fairness/balance concern, never a legality check. Every
  hard-rule check throughout `allocation_engine.py` had this backwards:
  the floor/ceiling was being checked against `_merged_hours()` (a SUM of
  job durations, deduplicated for overlaps), not the span -- meaning a
  driver needed 9+ CUMULATIVE hours of actual work to ever "activate,"
  when the real rule only requires a 9+ hour SPREAD between their first
  and last job, a much easier bar. This is, in effect, the resolution of
  the project's own long-standing open "OPT-001 duty-span question"
  (raised 2026-08-03, left undecided ever since) -- resolved directly:
  span for the hard rule, summed duration for fairness only, never the
  reverse. Confirmed as the direct cause of a real symptom the project
  owner had already noticed: `MANPREET SINGH` was getting zero jobs at
  all from `allocate_by_solver()`, because the only way to "activate" him
  under the old sum-based floor would have required 9+ hours of actual
  work with no combination available that didn't conflict elsewhere --
  under the corrected span-based floor, a much smaller, well-spread set
  of jobs is enough, and he now gets real work.
- **Fixed everywhere this check occurs:** a new `_day_span_hours()`
  helper (earliest start to latest end of a set of same-day intervals,
  deliberately distinct from `_merged_hours()`, which remains correct and
  unchanged for its existing fairness/tie-breaking role -- e.g.
  `occupied_seconds`, least-occupied-first candidate ranking, the
  solver's balance objective) plus a `_same_day_intervals()` helper for
  date-scoping. Every ceiling/floor check in the engine now uses span:
  the main `allocate()` candidate loop, `_fill_gaps_with_unresolved_jobs`,
  `_repair_minimum_daily_hours` (both its top-level floor check, now
  computing `span_hours` instead of the old `total_hours`, and its
  internal move-feasibility ceiling check), the shared
  `_unit_driver_feasible()` (used by `allocate_by_merit`,
  `allocate_by_anchor`, `_swap_repair`, and the chain search --
  propagating the fix to all of them through one shared function),
  `_swap_repair`'s own inline ceiling check, and `_rebalance_idle_drivers`
  (a more substantial rework here -- see below).
- **`_rebalance_idle_drivers()` needed a deeper rework, not just a metric
  swap.** Its whole design (accumulate candidate jobs one at a time until
  the running SUM reaches the driver's minimum) assumed sum-based floors;
  under span-based floors, reaching the minimum is about the accumulated
  jobs' SPREAD, not their total duration, so both the loop's stopping
  condition and its ceiling-feasibility check were switched to
  `_day_span_hours()`. The donor-side "would pulling this job leave the
  donor with a new illegal short day" check was similarly switched to
  compare the donor's remaining SPAN (after removing the candidate job)
  against their own floor, rather than remaining summed duration.
- **Monthly overtime budget: also span-based, confirmed directly by the
  project owner (a real, important correction to an interim guess).**
  An earlier attempt at this fix (mid-session) initially made the
  monthly-overtime-vs-budget check SUM-based rather than span-based,
  reasoning that overtime *pay* should track actual hours worked --
  flagged explicitly to the project owner as an assumption rather than
  something their original explanation covered. The project owner
  corrected this directly: `working_hours_per_day` is "the total legal
  working hours allowed / day, anything over this will be overtime" --
  a driver whose SPAN reaches 12h against a 9h baseline has 3h of
  overtime that day, full stop, deducted from
  `max_overtime_hours_per_month`, using the exact same duty-span concept
  as the daily ceiling, not a separate sum-based one. Reverted the
  interim sum-based attempt everywhere it had been applied (all the same
  call sites listed above) back to span-based overtime, matching the
  ceiling check exactly. This also simplified the solver's modeling (see
  below) back to a single combined helper instead of two separate ones.
- **`allocate_by_solver()` needed a genuinely new piece of CP-SAT
  modeling, not just a metric swap** -- span isn't expressible as a
  simple linear sum the way summed duration was; it requires MIN/MAX over
  only the units actually assigned to a driver. Solved with a channeling
  trick: for each (unit, driver) candidate pair, an "effective start" and
  "effective end" that collapse to a large neutral constant when that
  unit ISN'T assigned to that driver (so it can never influence the
  min/max), then `AddMinEquality`/`AddMaxEquality` over all of them per
  driver to get `first_start[d]`/`last_end[d]`, and `span[d] = last_end[d]
  - first_start[d]`. The daily floor (with the existing Same-Driver-group
  exemption, unchanged) and ceiling are now both checked against
  `span[d]`; the fairness/balance objective term still uses the
  unchanged, genuinely SUM-based `total_minutes[d]`. `_solver_effective_ceiling_minutes()`
  (the helper folding the flat daily ceiling and the monthly-overtime
  interaction into one number) needed no conceptual change once overtime
  was confirmed span-based -- an interim version had briefly split it
  into two separate helpers (`_solver_span_ceiling_minutes` /
  `_solver_monthly_overtime_cap_minutes`) while overtime was thought to
  be sum-based; reverted back to the single combined helper once overtime
  was confirmed span-based too, which is simpler and was in fact the
  correct design from the very first version of this function.
- **A permanent safety net was added, not just a one-time check:**
  `allocate_by_solver()` now runs an explicit post-solve validation
  after committing every unit, re-deriving each used driver's actual
  span directly from the real `Job` data (independent of the CP-SAT
  model's own internal bookkeeping) and raising a loud `AssertionError`
  if any used, non-group-exempt driver's span ever falls outside their
  floor/ceiling. Since every solver-returned solution is supposed to
  satisfy every modeled constraint by construction, this should never
  fire -- but a hard-rule violation must never ship silently regardless
  of how it could happen (Rule 2/6), so it's now checked explicitly on
  every run rather than assumed correct from the model design alone.
  Stress-tested clean across 30+ separate solver runs (including
  single-threaded runs across many different random seeds, specifically
  to rule out an intermittent issue noticed once during interim,
  incomplete edits mid-session) with zero violations caught.
- **Two existing synthetic tests needed fixture updates, not weakening.**
  `tests/test_daily_overtime_ceiling.py`'s "repair pass success" scenario
  and `tests/test_hour_accounting.py`'s duplicate-pickup scenario both
  had job times with real GAPS between them that were only "legal" under
  the old sum-based ceiling math (their true summed duration fit under
  the ceiling even though their span didn't). Adjusted the job times so
  the scenarios stay legal under the corrected span-based ceiling while
  still testing what they originally intended (a genuine repair-pass
  consolidation success; the duplicate-pickup dedup logic for fairness
  purposes) -- documented inline in each file why the times changed.
  `tests/test_gap_filling.py`'s driver fixtures also needed
  `max_overtime_hours_per_month=60.0` added (previously blank, which
  under span-based overtime correctly blocks ANY daily overtime at all,
  including the 3h the test's consolidation scenario now legitimately
  needs) -- matching how real driver profiles in the actual database are
  actually configured, not a special-case relaxation.
- **Real-data result on `UNPLANNED.xlsx` (both engines independently):**
  `allocate_by_anchor()` and `allocate_by_solver()` both still reach 0
  unresolved, 0 supplier, all 11 drivers used after this fix -- the
  headline numbers are unchanged, but the underlying per-driver hour
  distribution is now correct (`MANPREET SINGH` gets real work; every
  driver's actual duty span, not just their summed duration, is verified
  within their configured floor/ceiling).
- Full existing test suite re-run and passing after every fix in this
  phase. `tests/test_shift_start.py`, confirmed dead since the Phase 10
  shift redesign (its own imports reference functions removed back then)
  and already flagged for deletion in an old, no-longer-present
  `CHANGED_FILES_MANIFEST.txt`, was deleted this session -- the suite now
  runs 100% clean with no skip-list needed.
- **Documentation hygiene, found and fixed while syncing this session's
  work with the actual repository state:** this file had accumulated a
  genuine numbering collision -- two separate entries both titled
  "Phase 20" (one, "In-house Trip Count in Summary Header," had been
  mistakenly prepended above even the Phase 0 entry at the very top of
  the file, rather than appended in sequence). Corrected by renumbering
  the misplaced entry to "Phase 19b" and moving it to its correct
  chronological position -- the same fix pattern already used once before
  in this project for an earlier Phase 14/14b collision. **Standing
  reminder for future sessions (see NEXT_SESSION.md):** always append new
  phases at the END of this file, never prepend at the top, even for a
  small fix.
- **Deferred, explicitly, to a future session (the project owner offered
  the choice and this genuinely is separate-enough scope):** two new
  derived fields the project owner wants next -- "Balance Overtime /
  month" (`max_overtime_hours_per_month` minus overtime used so far this
  month, i.e. how much overtime budget remains) and "Balance hours /
  month" (`total_hours_per_month_target` minus total SPAN hours logged so
  far this month) -- both intended to display on the Drivers tab next to
  their respective existing fields, and both intended to be calculated
  and persisted when a day is finalized/saved (mirroring how
  `db.get_driver_month_overtime_hours` already works from
  `finalized_jobs`), not computed live on every keystroke. Not started
  this session. See `NEXT_SESSION.md` for the concrete next-step plan.

# NEXT_SESSION.md — Instructions for the next AI assistant

You are picking up an existing, working project. The person you're
talking to is **not a developer** — they're a fleet planner who has been
directing feature/bug decisions in plain language and relying on the
assistant to handle all implementation, testing, and technical tradeoffs.
Keep explanations in that same plain, concrete style; don't assume they
know programming terms.

## 1. Read these files, in this order, before touching anything

1. `AI_CONTEXT.md` — the full picture: what this is, why it's built the
   way it is, the business rules, the design decisions and their
   reasoning.
2. `DATABASE.md` — exact schema, what each column means, which tables
   are live vs. unused/reserved.
3. `ARCHITECTURE.md` — file-by-file responsibilities, data flow, the
   `allocate()` algorithm step by step.
4. `CHANGELOG_AI.md` — history, in case the person references "the
   supplier thing we fixed before" or similar — you'll need the context
   of *why* something is built the way it is, not just that it is.
5. This file, last, for how to actually work going forward.

**Do not start writing code before reading `AI_CONTEXT.md` in full.**
Several past mistakes in this project happened specifically because a
change was made without full context of an adjacent decision (see
Section 3 below).

## 2. How to understand this project quickly (if you only have 5 minutes)

- It's a **Windows desktop app** (Python + PySide6 + SQLite), used by
  **one planner**, for **one purpose**: fill in the Driver and Vehicle
  columns of a daily transport-request Excel file.
- There's a **strict three-stage pipeline**: deterministic rules engine
  (always runs first, no AI) → optional AI review (separate button,
  costs money, suggestions only) → export (writes back into the
  *original* file, untouched except two columns).
- **Hard rules live in database columns, never in free text.** If you
  see a free-text rule-line list anywhere in the UI, it is AI-context
  only — it is never enforced as a constraint. This was learned the hard
  way (twice) and is now a firm project convention.
- The single most valuable thing you can do when testing a change: get
  the person's actual data (they've previously pushed a real database +
  real Excel files to a public GitHub repo,
  `razaazad-ctrl/Feet-Planner`, specifically for this purpose) and run
  the real engine against it, rather than reasoning about it abstractly
  or only using synthetic test data. Ask if a similarly public repo/data
  dump exists for the current session if you need to validate something
  data-dependent.

## 3. Common mistakes to avoid (each of these actually happened)

- **Adding a new structured field to the database and the UI form, but
  forgetting to add it to the corresponding dataclass and actually check
  it inside `allocate()`.** This happened with `shift_start` — it was in
  the schema, the UI read/wrote it correctly, but `DriverProfile` didn't
  have the field and `build_driver_profiles` never read it, so the
  enforcement function existed but had nothing to check. **Whenever you
  add or modify a hard rule, verify all three of: (1) database column,
  (2) dataclass field + populated in `build_*_profiles`, (3) actually
  read inside the `allocate()` candidate-filtering loop.** Write a
  direct test that would fail if step 3 were missing, not just a test
  that the field round-trips through the database.
- **Assuming free-text rule-line matching (`rules_parser.py`) can be
  used for anything that needs to be reliably enforced.** It can't — see
  `AI_CONTEXT.md` Section 6. If a new hard rule is needed, it needs a new
  structured database column and form field, following the pattern of
  `working_hours_per_day`/`shift_period`/`max_working_hours_per_day`/etc.
  in `drivers_tab.py`.
- **Changing the database schema (renaming/removing/retyping a column)
  without considering that this breaks the additive migration system.**
  The migration system (`db._MIGRATIONS`) only supports *adding* new
  columns safely. Anything else requires either a fresh database (data
  loss, needs explicit warning to the user) or a real migration script
  you write yourself — don't assume `ALTER TABLE ADD COLUMN` covers it.
- **Introducing fuzzy/approximate matching for vehicle types "to fix"
  text-mismatch issues.** This was deliberately rejected — fuzzy
  matching risks silently conflating genuinely different vehicle
  configurations, which is worse than a visible zero-match. The correct
  fix for a text mismatch is telling the user to make the text
  consistent at the source (copy-paste from the Excel column), not
  loosening the matching logic. If this tradeoff is ever revisited, it
  needs an explicit conversation with the user first, not a unilateral
  code change.
- **Modifying `main_window.py`'s PIN-lock mechanism to switch-then-check
  instead of disable-then-unlock.** The switch-then-revert pattern was
  already tried and rejected because it could flash the Settings tab's
  contents before the check completed. The current pattern
  (`setTabEnabled(False)` while locked, corner "Unlock Settings" button)
  is the fix — don't regress to the earlier pattern.
- **Bundling "Run Planning" and "AI Review" into one automatic step**
  without an explicit go-ahead from the user. This was deliberately kept
  as two separate steps while the deterministic engine is still being
  hardened, specifically so bugs are traceable to one layer or the
  other. The user has agreed a combined shortcut can be added *later*,
  once they're confident in the engine — check whether that conversation
  has happened since this document was written before changing this.
- **Assuming `entity_rules_widget.py` is in active use.** It is legacy,
  superseded by `drivers_tab.py`/`suppliers_tab.py`. Check
  `main_window.py`'s actual imports before touching it or assuming
  changes to it will have any effect.
- **Assuming `off_day_log`/`comp_days` are working features because the
  tables exist.** They are reserved schema with zero CRUD or UI wired to
  them. If asked to build off-day/comp-day tracking, you're building it
  from scratch, not extending something that partially works.
- **In `allocate_by_solver()`, using a pairwise encoding for a soft
  "keep these things together" preference in the CP-SAT model.** Tried
  once (Phase 16): a `together[unit_i, unit_j, driver]` boolean per pair
  of Same-Driver-group units per shared candidate driver, correctly
  modeled logically, but measurably slowed the solver down -- wall time
  went from well under a second to over 60 seconds without even reaching
  a proven-optimal status, because pairwise terms create a lot of
  symmetric, equally-good alternative combinations for the search to
  sift through. The fix was a `touches_group[group, driver]` encoding
  (minimize the number of distinct drivers touching each group,
  O(members) instead of O(members²) per group) -- logically equivalent,
  much cheaper to search. If a future soft preference needs adding to
  this model, prefer an encoding that scales linearly with group/set
  size over one that scales quadratically, even when both are logically
  equivalent -- CP-SAT's search difficulty is about symmetry as much as
  raw constraint count. See `CHANGELOG_AI.md` Phase 16 for the full
  before/after timing.
- **Using OR-Tools' camelCase API (`model.AddHint`, `model.NewBoolVar`
  is fine, but check any camelCase method before relying on it) without
  checking whether it's a deprecated alias first.** `AddHint` specifically
  has a broken compatibility shim in the pinned `ortools` version when
  passed list arguments (`TypeError` deep inside the library, not an
  obviously-hint-related error message). The real, current method is
  snake_case `model.add_hint(var, value)`, called once per pair, not
  with lists. If an OR-Tools call throws a confusing error, check
  `inspect.getsource()` on the method actually being called before
  assuming the arguments are wrong -- the deprecated alias's own error
  can be misleading.

- **`allocation_engine.py`'s `allocate()` function.** This is the core
  of the whole product's value. It is deterministic and must stay that
  way — no AI, no network calls, fully repeatable given the same inputs.
  Any change here should be validated with a direct test (synthetic
  data first, then against real data if available) before considering
  it done, following the pattern established throughout this project
  (see `CHANGELOG_AI.md` for many examples of exactly this testing
  discipline).
- **Exact-string vehicle-type matching (`_type_matches`,
  `_driver_qualifies_for_type`).** This is intentionally strict. Any
  real-world "why isn't this working" report should be diagnosed first
  by checking for text mismatches (case, whitespace, wording variants
  like "Seated"/"Seater") before assuming there's a code bug.
- **`excel_import.py`'s header/column detection (`_HEADER_MAP`).** This
  is the single point of fragility for reading any new Excel export
  variant. An unrecognized header column silently contributes nothing
  (no error raised) — if a future file format changes column names,
  this will fail silently, not loudly. Consider whether this deserves a
  validation warning if picking this up.
- **`export.py`'s original-file preservation.** The whole point of this
  module is that it changes *nothing* except two specific cells. Any
  future change here needs to be re-verified against a real file with
  the same test discipline used originally (confirm an untouched column
  is byte-identical before/after, confirm formatting/styles survive).
- **Monthly overtime calculation (`db.get_driver_month_overtime_hours`).**
  This groups by day and sums *excess over baseline per day*, not a
  simple total — a subtle distinction that's easy to accidentally
  simplify incorrectly if refactored. Re-read the docstring and the
  `DATABASE.md` explanation before touching this.
- **Supplier hiring/naming logic in `allocate()`'s supplier pass.** The
  exact naming convention (`SUPPLIER`, `SUPPLIER 1`, `SAME <label>`) was
  explicitly confirmed by the user, not inferred — don't "improve" it
  based on what a historical reference file shows, since that file is
  known to be an inconsistent manual artifact (see `CHANGELOG_AI.md`
  Phase 6).

## 5. Current known issues (honest, as of end of last session)

- ~~`shift_start` documentation/code mismatch~~ **RESOLVED this session.**
  The project owner confirmed directly that `shift_start` was NOT being
  enforced in their live local copy either (not just this GitHub
  snapshot) — so this was a real, live bug, not a stale-snapshot
  artifact. Fixed by adding the missing `DriverProfile.shift_start`
  field, populating it in `build_driver_profiles`, and enforcing it via
  `_job_is_before_shift_start()` in the candidate loop. While fixing
  this, the project owner also asked for `license_types` and
  `working_hours_per_day` to be audited for the same failure pattern:
  `license_types` was confirmed correct (tested, no bug); a real bug WAS
  found in `working_hours_per_day` — see the item below.
- ~~`working_hours_per_day` not enforced when overtime cap is blank~~
  **RESOLVED this session.** `max_overtime_hours_per_month = None` was
  silently skipping the entire hours-check block, rather than being
  treated as "no overtime allowed" — a driver with a daily limit
  configured but no monthly overtime cap could get unlimited hours in
  one day. Confirmed with the project owner and fixed: blank overtime
  cap is now treated the same as an explicit `0`. See `AI_CONTEXT.md`
  Section 9 (bug #9) for the full writeup and the test that proved it.
- ~~No hard per-day ceiling on overtime~~ **RESOLVED this session.** The
  project owner reported the exact live symptom: a driver given jobs
  from 7 AM to 5 AM the next day (~22h). Root cause: the monthly-bucket
  overtime check had no per-day sub-limit, so a driver with unused
  monthly overtime (most real drivers have 60h/month) could spend a huge
  chunk of it in one single day. Fixed by adding
  `MAX_OVERTIME_HOURS_PER_DAY = 2.0` (confirmed with the project owner)
  as a hard daily ceiling in `allocation_engine.py`, checked before the
  monthly-bucket logic. This is currently a single global constant, not
  per-driver configurable -- if a future request needs per-driver daily
  overtime limits, that's a new column + UI field + dataclass field,
  following the same three-step pattern as every other hard rule in this
  project (see "Common mistakes to avoid" above).
- ~~No hard per-day ceiling on overtime~~ **RESOLVED in an earlier
  session, then SUPERSEDED 2026-08-03 (see next item).** The project
  owner reported the exact live symptom: a driver given jobs from 7 AM to
  5 AM the next day (~22h). Root cause: the monthly-bucket overtime check
  had no per-day sub-limit, so a driver with unused monthly overtime
  (most real drivers have 60h/month) could spend a huge chunk of it in
  one single day. Originally fixed by adding
  `MAX_OVERTIME_HOURS_PER_DAY = 2.0` as a hard daily ceiling in
  `allocation_engine.py`, checked before the monthly-bucket logic. At the
  time this was a single global constant, not per-driver configurable --
  that limitation is now resolved, see below.
- **HR-002/HR-005 rework, 2026-08-03: shift redesigned, daily ceiling made
  configurable, new hard daily minimum added.** Three related changes
  made together in one session, at the project owner's explicit request:
  1. `shift_start` (an exact clock time, fixed by the planner before
     planning) replaced with `shift_period` (`'morning'` / `'evening'` /
     `None`) -- the planner no longer commits to an exact time; the
     engine enforces a window (before/after 12:00) instead, and the
     driver's real first-job time is only known after planning. No
     automatic 15-day rotation or off-day-triggered transition logic --
     explicitly rejected by the project owner ("if the planner is
     deciding which driver to bring in morning and evening we dont have
     to track this"). See spec SS-001/SS-002, `AI_CONTEXT.md` Section 6.
  2. `MAX_OVERTIME_HOURS_PER_DAY = 2.0` (the module constant from the
     item above) removed entirely, replaced by `max_working_hours_per_day`
     -- a real per-driver field, planner-set alongside
     `working_hours_per_day` (e.g. 9/12). Blank falls back to
     `working_hours_per_day` (fail-closed, zero overtime), not
     "unlimited." See spec HR-002.
  3. NEW hard rule: a driver used at all on a day must reach at least
     `working_hours_per_day` that day (spec HR-005). Can't be a per-job
     filter (the engine doesn't know a day's total until the last job is
     considered), so it's a repair pass after normal allocation --
     `allocation_engine._repair_minimum_daily_hours()`. Moves an
     under-minimum driver's jobs to another qualifying driver with room,
     or releases the whole day to unresolved if nobody has room. This is
     a best-effort greedy heuristic (not a global optimizer) and has
     **not yet been validated against a real day's full job volume** --
     only synthetic 1-3-job scenarios so far, per the same data-scarcity
     caveat noted throughout this section. If picking this up next,
     prioritize a real-data trial run over further heuristic tuning.
  Two follow-ups deliberately NOT done this session (scope was the three
  items above): reporting each driver's actual first-job time back to
  them after planning (data already exists in a finalized day, just not
  surfaced in `export.py`/`digest_generator.py` -- spec SS-003), and
  showing month-to-date overtime-so-far on the Drivers tab (already
  computed correctly by `get_driver_month_overtime_hours`, just not
  displayed -- spec NEW-006).
- **OPT-002/003 gap-filling fix, 2026-08-03 (same day as the above, spec
  bumped to v5).** The project owner tested the HR-002/HR-005 build
  directly against a real UNPLANNED.xlsx and hit exactly the scenario
  OPT-001/002/003 had already flagged: a driver got a 13:00-15:00 job and
  a 22:00-01:00 job (5h actual work, 7h idle in the 12h span between
  them) while a 16:00-20:00 job that would have fit the gap was left
  completely unassigned. Fixed with a new post-pass,
  `allocation_engine._fill_gaps_with_unresolved_jobs()`, that runs after
  the main greedy loop and slots any still-unresolved job into a genuine
  bounded gap in an existing driver's day, if one exists and every other
  hard rule still holds. An in-loop version was tried first and reverted
  -- it's structurally impossible for it to work given jobs are processed
  in strict start-time order (the later of a gap's two bounding jobs
  isn't assigned yet when an earlier job that would sit in that gap is
  being considered). While building the test for this, also found and
  fixed a real bug in the HR-005 repair pass itself: it worked off a
  one-time snapshot of driver job lists, so a fix for one driver could
  leave a later driver's fix in the same pass reasoning about stale data
  -- jobs visibly ping-ponged between two drivers. Fixed by recomputing
  each driver's actual day fresh right before processing it, plus fixed
  a related latent bug where multiple jobs moved to the same replacement
  driver in one batch weren't checked against each other's tentative
  hours. See `CHANGELOG_AI.md` Phase 11 for full detail, and
  `tests/test_gap_filling.py`. **Still open, not decided:** whether a
  driver's duty SPAN (not just summed job duration) should count toward
  hour limits -- see priority item 1 in Section 6 below.
- **Real PLANNED.xlsx study, 2026-08-03 (same day again, spec bumped to
  v7).** The project owner supplied a real human-planned day and asked
  for a full reconstruction: strip the human's driver/vehicle
  assignments, rebuild the jobs, and see if `allocate()` can produce the
  same grouping structure from scratch. This surfaced and fixed THREE
  real issues: (1) `occupied_seconds` naively summed every row's
  duration, including simultaneous same-time duplicate rows in one
  "Same Driver" group (routine in the real file) -- one driver showed
  ~17h "occupied" vs. ~11h true, falsely tripping the daily ceiling.
  Fixed with a new `_merged_hours()` union-based calculation everywhere
  hours are read or written. (2) "Driver Only" jobs (no vehicle needed)
  always fell through to unresolved -- a real row in the file needed
  this. Fixed (NEW-004). (3) The HR-005 repair pass only summed a
  driver's UNGROUPED jobs when checking the daily minimum, so grouped-job
  hours were invisible to it -- fixed to use the true total across every
  job, and to correctly recognize when a shortfall is entirely inside a
  protected group (several real drivers legitimately end their day under
  9h for exactly this reason). After these three fixes: 42 of 44 real
  rows auto-assigned in-house, zero supplier, matching the real file's
  own type-driven driver-splitting pattern. The remaining 2 traced to a
  NEW finding, TB-001: the default 30-minute travel buffer blocked
  legitimate zero-gap back-to-back continuations between unrelated
  orders for the same driver. Confirmed with the project owner that this
  was actively wrong (planner-set times already include return-to-base
  travel time), not just strict -- `DEFAULT_TRAVEL_BUFFER_MINUTES`
  changed from 30 to 0. Re-ran the reconstruction: **all 44 rows now
  auto-assign, zero unresolved, zero supplier, zero violations.** See
  `CHANGELOG_AI.md` Phase 12 for full detail and the repeatable
  reconstruction methodology. Future work flagged, not built: once live
  Google Maps travel-time lookups are wired in, gaps between jobs at
  different locations should be checked against real drive time instead
  of a flat constant.
- **The real database's `finalized_jobs` table is empty** (no planning
  day has ever been "Finalized" yet, or the history wasn't migrated into
  this repo snapshot). This means `month_overtime_so_far` always starts
  at 0 for every driver on every run, which maximizes how much monthly
  overtime "looks available" for any given day -- not a bug by itself
  (0 is a safe/conservative starting assumption), but it does mean the
  monthly-bucket side of the overtime check is currently not doing
  anything useful without real history. The project owner has offered to
  provide the last ~25 days of PLANNED Excel files to backfill this --
  see the note at the top of this file about that in-progress request.

- **`total_hours_per_month_target` is stored and shown in the UI but
  never enforced anywhere.** It's informational only right now — no
  logic reads it. If asked to make it a real constraint, this is new
  work, not a bug fix.
- **AI suggestion Accept does not automatically update the results
  table.** Clicking Accept on an AI suggestion logs the decision
  (`decision_log`) but the planner still has to manually go find and
  edit the corresponding row if they want the table to reflect it. This
  was an explicitly acknowledged interim state, not a hidden bug — the
  natural next step (discussed but not built) is wiring Accept to
  actually mutate the relevant `Job` and re-render the table.
- **The "restrict planning to a shortlist of drivers/suppliers for
  today" feature has a supported function signature
  (`allocate(..., allowed_driver_ids=..., allowed_supplier_ids=...)`)
  but no UI control to set those parameters.** Backend-ready, no
  frontend.
- **No PDF export.** Only Excel export exists. PDF was discussed as
  likely needing Excel COM automation (`pywin32`) since the target
  machine is Windows and almost certainly has Excel installed — this
  was a plan, not an implementation.
- **Driver shift rotation (e.g. "15 days morning, then 15 days
  afternoon") was considered and explicitly rejected, 2026-08-03.** An
  earlier version of this document described this as "not implemented
  yet, derive it from history later" -- the project owner has since
  decided against building it at all: shift is now just a planner-set
  Morning/Evening label per driver (see the HR-002/HR-005 rework entry
  above), with no rotation tracking of any kind. Don't resurrect this as
  a planned feature unless the project owner explicitly asks again.
- **No packaging into a `.exe`.** The app has only ever been run via
  `python -m app.main`. PyInstaller packaging was discussed as a later
  step, deliberately deferred while the app is still under heavy
  iteration (rebuilding a `.exe` on every change is unnecessary friction
  during active development).
- **Data-quality issues found in the user's real database** (not code
  bugs, but worth knowing so you don't rediscover them from scratch):
  license types were uniformly under-specified across all drivers in
  the snapshot pushed to GitHub, at least one driver (`VENUGOPAL`) was
  missing from the roster entirely, and at least one vehicle-type wording
  mismatch existed ("23 Seater Bus" vs "23 Seated Bus"). If working
  against that same repo snapshot, expect these same issues unless the
  user has since corrected them.
- **Hour-fairness fix, 2026-08-06 (Phase 14, scheduling spec v10) --
  RESOLVED, but with an honest remaining gap.** The project owner
  reported a real day where drivers landed anywhere from 2h to 16h,
  despite SD-004 and TB-001 already being fixed. Root cause: 84% of that
  day's rows were "Same Driver" grouped, and HR-005's repair pass
  unconditionally skipped any grouped driver-day -- so the daily-minimum
  safety net almost never actually ran. Fixed with a look-ahead
  group-leader pick (SD-005) plus widening HR-005 to move whole grouped
  days (never split). Two further real bugs were caught and fixed while
  building this (a thrashing/ping-pong issue once whole groups became
  movable, fixed with a `settled_job_ids` stability guard; and a
  processing-order artifact where the repair pass's driver search picked
  the first alphabetical match instead of the most-free one). Real-data
  result: spread compressed from 2.0h-16.0h to 4.0h-11.0h against a
  5.0h-12.0h human-planned reference for the same day -- real, verified
  progress, but not yet full parity. See CHANGELOG_AI.md Phase 14 for the
  full writeup.
- **NEW-008, 2026-08-06, open, data issue not a code bug:** one driver's
  `license_types` contains a literal embedded newline before
  "(with lift)", silently defeating exact-string matching -- same failure
  class as the "Seated"/"Seater" mismatch. Accounts for a few of the real
  run's unresolved rows in Phase 14. Needs correcting at the data layer
  (Drivers tab), not a code change -- flagged for the project owner.
- **RESOLVED into real, tested alternatives (2026-08-06, same day) -- see
  CHANGELOG_AI.md Phase 15.** The open design question above (was "Same
  Driver" ruling the allocation, and should driver selection be
  merit-based) led to two full alternative strategies being built
  alongside `allocate()`, not a patch to it:
  - `allocate_by_merit()` -- shift-partitioned, event-diverse seeding,
    pairs-only Same-Driver pre-merge. Real-data result: WORSE than
    baseline (16 vs. 12 unresolved). Kept as a tested alternative, not
    recommended for use as-is.
  - `allocate_by_anchor()` -- anchors each driver's first AND last job
    intentionally (most-constrained drivers first, via
    `_driver_ordered_most_constrained_first`), then a bounded swap-repair
    search (`_swap_repair`, capped rounds). Real-data result: ties
    baseline on unresolved count, but uses all 9 active drivers instead
    of leaving 2 idle -- directionally promising.
  - `_rebalance_idle_drivers()` -- new, wired into all three strategies:
    "every driver has real work" is now a first-class goal, not just "no
    driver is illegally under-minimum." Two real bugs were found and
    fixed building this (an oscillation that undid a correct
    unresolved-release decision; a donor-workload miscalculation that
    silently undid a valid consolidation) -- both caught by the EXISTING
    test suite.
  **Neither new strategy is wired into the UI.** `plan_day_tab.py` still
  calls only `allocate()`. Whether/when to switch is an explicit decision
  still to be made with the project owner, not something to do
  unilaterally -- see the priority list in Section 6 below.
- **NEW-008's newline bug was fleet-wide, not isolated -- FIXED
  2026-08-06.** Investigating it further (per the project owner's
  request) found it in ALL 11 active drivers' `license_types`, one
  vehicle's `vehicle_type` (plate `Z 43915`), and two excluded/inactive
  drivers -- 15 records total, all cleanly fixed with a direct SQL sweep
  (not a code change). A follow-up sweep confirmed zero embedded
  newlines remain anywhere in `drivers.license_types`,
  `vehicles.vehicle_type`, or `supplier_offerings.vehicle_type`. Real
  result: 2 of 3 affected rows now resolve correctly. Total unresolved
  count did NOT drop overall (ticked up slightly in all three
  strategies) -- confirmed as the honest signature of a greedy
  heuristic (more legal options reshuffled the whole allocation and
  created different shortfalls elsewhere), not a new bug. This confirms
  the remaining gap to zero-unresolved is now an algorithm-sophistication
  limit, not a data problem.
- **10-Ton-Chiller-Truck scarcity confirmed as a genuine capacity
  constraint, not fixable in software.** Only one physical vehicle of
  that type exists (plate `A 67338`), and no supplier offers that exact
  type either (closest are "10 Ton Dry Truck", different, or "5 Ton
  Chiller Truck", different capacity). Flagged to the project owner as a
  real gap they may want to close (a second vehicle, or adding a
  matching supplier offering) -- do not attempt a code workaround for
  this if it comes up again.
- **OPEN, real gap: no dedicated synthetic tests for any of Phase 15's
  new code.** `allocate_by_merit`, `allocate_by_anchor`, `_swap_repair`,
  `build_planning_units`, and `_rebalance_idle_drivers` (the last one now
  has real-data-driven bug fixes, but no synthetic regression tests of
  its own) have only been validated via direct real-data runs against
  `UNPLANNED.xlsx` + `fleetplanner.db` so far, not the small, targeted
  synthetic tests every other piece of this engine has (see the
  `tests/` folder for the established pattern). This is a real,
  disclosed gap -- worth closing before either new strategy is
  considered for production use, following this project's own testing
  discipline (see Section 4, "Areas of the code that require extra
  care"). **Still open as of Phase 16 -- see below, the gap now also
  covers all of Phase 16's new code on top of this.**
- ~~10-Ton-Chiller-Truck scarcity confirmed as a genuine capacity
  constraint~~ **Confirmed still physically true (only one such vehicle
  exists, plate `A 67338`), but NOT actually a bottleneck once the
  algorithm correctly sequences one driver across the whole day on it --
  see Phase 16.** The scarcity note above was accurate as written; it
  just turned out one vehicle, run by one driver back-to-back
  (12:00-17:00, 17:00-22:00, 23:00-01:00 in the real ground-truth file),
  was always sufficient for the real day's actual demand. The earlier
  "genuine ceiling" conclusion was really an artifact of the algorithm
  not keeping that one driver on that one vehicle consistently across
  the day, not the vehicle count itself.
- **RESOLVED 2026-08-06/09 (Phase 15 + Phase 16 combined): zero unresolved,
  zero supplier, all 11 drivers used on the real `UNPLANNED.xlsx`, via TWO
  different validated paths.** `allocate_by_anchor()` reached this via
  three real bug fixes to `_swap_repair` (whole-group/bundle displacement,
  a direct-fit check it was missing entirely, and a bounded multi-hop
  chain search) plus a genuine business-rule correction (the Morning/
  Evening shift window only gates a driver's FIRST job of the day, not
  every job -- confirmed against a real human-planned file). The new
  `allocate_by_solver()` (Google OR-Tools CP-SAT) reaches the SAME
  headline result independently, with a mathematically PROVEN-optimal
  status (not just "a solution was found"), in under 4 seconds on the
  real file. See `CHANGELOG_AI.md` Phase 16 for the full technical
  writeup of both, including three more real bugs found and fixed while
  building the solver (a deprecated-API footgun in the `ortools` version
  pinned here, a 60x+ slowdown from a naive pairwise soft-constraint
  encoding, and a daily-floor exemption the solver hadn't replicated
  from the existing heuristic engines). **This does not mean the project
  is "done"** -- see the priority list in Section 6, now substantially
  rewritten, for what's actually next.
- **NEW, real gap opened by Phase 16: `allocate_by_solver()` has zero
  dedicated synthetic tests, same caveat as the other two experimental
  strategies above.** All validation so far is a single real-data run.
  Given how much of this strategy's correctness rests on the CP-SAT
  model itself (constraint-by-constraint) rather than step-by-step
  procedural logic, targeted synthetic tests here would likely look
  different in shape from this project's existing `tests/*.py` files --
  probably small, hand-built scenarios checking ONE constraint at a time
  (e.g. "a driver whose whole day would be one grouped job should never
  be forced to hit the floor," "an off-day driver should never get a
  variable created for them at all," "the shift window should allow a
  continuation job but refuse a lone evening job for a morning-only
  driver") rather than full `allocate()`-style end-to-end runs. Not
  built yet -- flagged as a real priority, see Section 6.
- **RESOLVED 2026-08-10 (Phase 21): the daily hard rule (floor/ceiling)
  and monthly overtime budget were both being checked against SUMMED job
  duration instead of duty SPAN (first job start to last job end) --
  a foundational correction, not a tuning tweak.** The project owner
  caught this directly with a concrete example (AALIM: 3 jobs summing to
  8h shouldn't matter for legality, only his span does) and separately
  corrected an interim guess made mid-fix (overtime is ALSO span-based,
  not sum-based, confirmed directly: "anything over [working_hours_per_day]
  will be overtime... deducted from max overtime hours/month"). Fixed
  across every strategy via a new `_day_span_hours()` helper (kept
  distinct from `_merged_hours()`, which remains correct for its
  existing fairness/tie-breaking role). `allocate_by_solver()` needed
  real new CP-SAT modeling (a MIN/MAX channeling trick over only the
  units assigned to each driver) since span isn't a simple linear sum.
  A permanent post-solve validation was added to the solver to catch any
  future violation loudly rather than silently. See `CHANGELOG_AI.md`
  Phase 21 for the full technical writeup, including which existing
  tests needed fixture updates (not weakening) to reflect the corrected
  rule.
- **NEW, real, explicitly deferred: "Balance Overtime / month" and
  "Balance hours / month" fields, requested 2026-08-10.** The project
  owner wants two new derived numbers displayed on the Drivers tab:
  - **Balance Overtime / month** = `max_overtime_hours_per_month` minus
    however much span-based overtime the driver has actually used this
    month so far (the same figure `db.get_driver_month_overtime_hours`
    already computes for the monthly-cap check -- just not currently
    displayed anywhere). Shown next to the existing "Max overtime
    hours/month" field.
  - **Balance hours / month** = `total_hours_per_month_target` minus the
    total SPAN hours logged so far this month (a NEW monthly
    accumulation this project doesn't currently track anywhere --
    `total_hours_per_month_target` is presently informational-only, see
    Section 5 above). Shown next to the existing "Total hours/month
    target" field. If `total_hours_per_month_target` is blank, both
    balance fields should read as zero, per the project owner's
    instruction.
  - **Both should be calculated when a day is Finalized/saved to
    history, not computed live** -- mirroring the existing
    `finalized_jobs`-based pattern `db.get_driver_month_overtime_hours`
    already uses, not a new on-the-fly calculation. This means the
    natural place to compute and persist "hours logged this month" is
    likely a small addition alongside (or inside) `db.save_finalized_jobs`
    or a sibling read function, following the same day-grouped,
    finalized-history-driven approach already established for overtime.
  - Explicitly NOT started this session -- the project owner offered
    the choice to do it now or defer, and given it's genuinely separate
    scope (new DB read logic + new Drivers tab UI fields, not an
    allocation-engine change), it was deferred. This is now the
    **top UI/DB priority** for the next session that picks up Drivers
    tab or monthly-tracking work -- see item 0 below, now updated to
    reflect this alongside the still-open bigger-file validation.

## 6. Recommended next improvements, roughly in priority order

Based on what's explicitly still open and what would unblock the most
value. **This list changed substantially after Phase 16, and got one
more addition after Phase 21** -- the question that used to sit at
position 0 ("should we move toward production, or keep iterating") has
effectively been answered by results, not by discussion:
`allocate_by_solver()` now reaches 0 unresolved / 0 supplier / all
drivers used, PROVEN optimal, on the real file that motivated all of
Phase 14/15/16's work, and Phase 21 corrected a foundational hard-rule
bug (span vs. summed duration) across every strategy. The open questions
now are about validating that this holds up on a bigger, more realistic
file (one that actually needs some supplier use), a genuinely new
feature the project owner asked for (the Balance fields), and closing
real, already-disclosed gaps -- not about which algorithmic direction to
pursue.

0. **Validate `allocate_by_solver()` against a bigger, real file that
   includes genuine supplier need (in progress as of the end of this
   session).** Everything validated so far is the one `UNPLANNED.xlsx`
   file the project owner deliberately built WITHOUT needing any
   supplier, specifically to prove the in-house engine could handle it
   alone first. The project owner's own words: "if this get[s] to 0
   unresolved with following all the hard rules then i will be
   confident to introduce the bigger schedule and will know that
   suppliers used were necessary." That milestone is now reached -- the
   next real test is a file where SOME supplier use is genuinely
   expected, to confirm (a) the solver still resolves everything
   in-house-first correctly, (b) supplier use only appears where truly
   necessary (matching Rule 7), and (c) solve time stays reasonable at
   a larger scale (the current file is 44 jobs / 11 drivers, solving in
   a few seconds; a bigger file's actual size and solve time are both
   unknowns until tested).
1. **Build "Balance Overtime / month" and "Balance hours / month"**
   (requested 2026-08-10, explicitly deferred to this list -- see
   Section 5's entry above for the exact field definitions and the
   project owner's stated preference for computing these at Finalize-Day
   time, not live). Concrete first steps: (a) a new `db` function
   alongside `get_driver_month_overtime_hours` that sums SPAN hours (not
   summed job duration -- consistent with Phase 21) per finalized day,
   grouped by driver and month; (b) two new read-only fields on the
   Drivers tab, positioned next to "Max overtime hours/month" and "Total
   hours/month target" respectively; (c) confirm with the project owner
   whether "hours logged this month" for the Balance-hours field should
   also be span-based (matching the overtime field's logic) before
   building it, since Phase 21 was specifically about the DAILY
   floor/ceiling and monthly overtime, and this is a related but
   distinct monthly figure that wasn't explicitly covered by that
   correction.
2. **Write dedicated synthetic tests for ALL of Phase 15/16/21's new
   code before considering any of it production-ready.** Still
   completely unaddressed: `allocate_by_merit`, `allocate_by_anchor`,
   `_swap_repair` (now significantly more complex after Phase 16's
   bundle/chain-search widening), `build_planning_units`,
   `_rebalance_idle_drivers`, `allocate_by_solver` and its CP-SAT model
   (including the new span-channeling constraints from Phase 21), and
   `_day_span_hours`/`_same_day_intervals` themselves -- all validated
   only via direct real-data runs and (for the solver specifically) a
   30+-run stress test so far, not the small, targeted synthetic tests
   every other piece of this engine has. For the solver, small
   hand-built scenarios checking one constraint at a time (see Section
   5's note on this) will likely be more useful than trying to mirror
   the existing `tests/*.py` pattern exactly.
3. **Decide whether/when to wire `allocate_by_solver()` (or
   `allocate_by_anchor`, as a fallback if `ortools` isn't available in
   some environment) into `plan_day_tab.py`'s "Run Planning" button.**
   Currently NONE of the four strategies except the original `allocate()`
   are reachable from the UI at all -- this is still true after Phase 21.
   Now that one of them has real, validated, proven-optimal results,
   this is a much more concrete decision than it was before, but it's
   still the project owner's call, not something to do unilaterally
   (Rule 1/16). Related sub-questions worth raising explicitly when this
   conversation happens: should the UI offer a choice of strategy, or
   just replace `allocate()` outright? Should `ortools` become a hard
   requirement (added to the base install) or stay optional with a
   graceful fallback to `allocate_by_anchor` if it's missing (the lazy
   import already supports this today)? What should happen in the UI if
   the solver returns `FEASIBLE` instead of `OPTIMAL` (i.e. it hit the
   time limit) -- silently accept the best-found result, or surface that
   distinction to the planner?
4. ~~Decide the duty-span question (spec OPT-001)~~ **RESOLVED 2026-08-10,
   Phase 21.** A driver's SPAN (first job to last job that day, including
   idle gaps) now counts toward daily/monthly hour limits -- confirmed
   directly by the project owner, not summed job duration as before.
   Summed duration remains relevant only for fairness/balance, never
   legality. See `CHANGELOG_AI.md` Phase 21 for the full technical
   writeup across every strategy.
5. **Confirm and, if wanted, fix NEW-007 (spec): extend gap-filling to
   cover wide-open (not just bounded) driver capacity.** Found 2026-08-03
   (Phase 13) via a real test: a driver ended the day with a single 2h
   job while other jobs sat unresolved. Possibly moot now -- Phase 16's
   multi-hop chain search and `allocate_by_solver`'s direct optimization
   both attack a broader version of this same underutilization problem
   from a different angle -- but hasn't been explicitly re-checked
   against this specific spec since those were built.
6. **Wire AI suggestion Accept to actually mutate the results table.**
   High value, well-scoped, the decision-logging plumbing already
   exists — this is "finish what's started," not new design work.
7. **Help the user fix their real data** (license types, missing
   drivers, vehicle-type text consistency, and now also the embedded-
   newline issue confirmed fleet-wide in Phase 15 -- the project owner's
   own live database was NOT touched this session, only the test
   snapshot was) using the same extract-from-PLANNED-file technique
   already built and used once in this project (see `CHANGELOG_AI.md`
   Phase 6).
8. **Build the daily driver/supplier shortlist UI**, since the backend
   already supports it — comparatively low effort for real planner
   value (handling "these specific drivers are off tomorrow" days more
   conveniently than the per-entity exclusion toggle alone).
9. **PDF export**, once the user is ready to prioritize it — needs a
   design conversation about the `pywin32`/Excel-COM approach first,
   since it's a different technical approach than everything else built
   so far (everything else is pure Python; this would shell out to a
   real Excel install).
10. **One small, well-scoped follow-up from the Phase 10 rework:**
    reporting each driver's actual first-job time back to them once a
    day is finalized (spec SS-003 -- data exists, not surfaced yet). The
    other Phase 10 follow-up listed here previously (month-to-date
    overtime-so-far on the Drivers tab, spec NEW-006) is effectively
    superseded by item 1 above ("Balance Overtime / month") -- build that
    instead of NEW-006 separately, since it's the more complete version
    of the same idea.
11. **Only after the above, and only with explicit confirmation from the
    user:** consider the combined "Plan My Day" one-click shortcut
    (merging Run Planning + AI Review) and PyInstaller packaging into a
    distributable `.exe`. If `allocate_by_solver()` does get wired into
    the UI before packaging happens, factor `ortools`'s bundle-size
    impact into that packaging conversation (see AI_CONTEXT.md Section 6
    "The solver strategy" for the tradeoff detail already discussed with
    the project owner). (Driver shift-rotation logic, previously listed
    here, was explicitly rejected 2026-08-03 -- see Section 5 -- and
    should not be resurrected as a planned item.)

Do not reorder this list based on assumptions about what seems
technically interesting — confirm priority with the user, since they've
consistently driven prioritization decisions throughout this project
rather than deferring to default technical judgment.

## 7. How to work with this specific person

- They think in **real operational scenarios**, not abstractions — when
  proposing a design, it lands better paired with a concrete example
  ("if Deepak's shift starts at 6 PM and gets offered a 10 AM job...")
  than a general description of the rule.
- They have caught **real, subtle bugs by testing directly**, not by
  reading code — they will upload real files and describe unexpected
  output rather than pointing at a line of code. Take these reports
  seriously and re-derive the root cause from the actual data rather
  than guessing; this project's bug list (`CHANGELOG_AI.md`) shows this
  approach reliably finds real issues.
- They are cost-conscious about API usage and explicitly asked for the
  system to be designed so cost doesn't grow unboundedly over time (the
  whole `decision_log`/`preference_digest` two-tier design exists
  because of this one conversation) — keep this in mind before adding
  any new AI-call pattern.
- They prefer **asking a clarifying question over guessing** when a
  requirement is ambiguous, and have been given exactly that kind of
  question multiple times in this project (e.g. the supplier-numbering
  toggle question) — this worked well and should continue as the
  default approach for genuinely ambiguous new requirements, rather than
  picking a default and hoping it's right.

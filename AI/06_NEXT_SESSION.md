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

## 4. Areas of the code that require extra care

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

## 6. Recommended next improvements, roughly in priority order

Based on what's explicitly still open and what would unblock the most
value:

1. **Decide the duty-span question (spec OPT-001), explicitly left open
   2026-08-03.** Should a driver's SPAN (first job to last job that day,
   including idle gaps) count toward daily/monthly hour limits, instead
   of (or alongside) summed job duration as today? Tradeoff already
   explained to the project owner once (span-based is more realistic
   about unavailability during a gap, but hits hour ceilings faster than
   actual hours worked and may not match driver pay) -- they wanted to
   see how much the OPT-002/003 gap-filling fix (Section 5/`CHANGELOG_AI.md`
   Phase 11) reduces the underlying scenario before deciding. Worth
   revisiting after a few real planning runs with the gap-fill fix in
   place.
2. **Confirm and, if wanted, fix NEW-007 (spec): extend gap-filling to
   cover wide-open (not just bounded) driver capacity.** Found 2026-08-03
   (Phase 13) via a real test: a driver ended the day with a single 2h
   job while other jobs sat unresolved. `_fill_gaps_with_unresolved_jobs()`
   only helps when a driver has an existing booking BOTH before AND after
   the gap -- a driver who's simply underutilized with nothing bracketing
   their light day is invisible to it. Not confirmed as a bug yet (the
   specific case checked appeared to be a genuine license-type mismatch,
   not a coverage gap) -- ask the project owner for a case where the
   license clearly matches before building this, since it's a real scope
   increase, not a quick tweak.
3. **Wire AI suggestion Accept to actually mutate the results table.**
   High value, well-scoped, the decision-logging plumbing already
   exists — this is "finish what's started," not new design work.
4. **Help the user fix their real data** (license types, missing
   drivers, vehicle-type text consistency) using the same
   extract-from-PLANNED-file technique already built and used once in
   this project (see `CHANGELOG_AI.md` Phase 6) — this will likely
   improve real-world accuracy more than further engine changes at this
   point, based on the diagnostic work already done.
5. **Build the daily driver/supplier shortlist UI**, since the backend
   already supports it — comparatively low effort for real planner
   value (handling "these specific drivers are off tomorrow" days more
   conveniently than the per-entity exclusion toggle alone).
6. **PDF export**, once the user is ready to prioritize it — needs a
   design conversation about the `pywin32`/Excel-COM approach first,
   since it's a different technical approach than everything else built
   so far (everything else is pure Python; this would shell out to a
   real Excel install).
7. **Two small, well-scoped follow-ups from the Phase 10 rework, either
   one is a good next pick:** showing month-to-date overtime-so-far on
   the Drivers tab (spec NEW-006 -- the number is already computed
   correctly, just not displayed), and reporting each driver's actual
   first-job time back to them once a day is finalized (spec SS-003 --
   same situation, data exists, not surfaced yet).
8. **Only after the above, and only with explicit confirmation from the
   user:** consider the combined "Plan My Day" one-click shortcut
   (merging Run Planning + AI Review) and PyInstaller packaging into a
   distributable `.exe`. (Driver shift-rotation logic, previously listed
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

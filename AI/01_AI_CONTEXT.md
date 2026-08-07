# AI_CONTEXT.md — Fleet Planner Project

**Read this file first, in any new session.** It orients you to the whole
project before you touch code. For structural detail see `ARCHITECTURE.md`,
for the database see `DATABASE.md`, for history see `CHANGELOG_AI.md`, and
for how to work on this project going forward see `NEXT_SESSION.md`.

## 1. What this project is

A **standalone Windows desktop application** for a catering/events company's
fleet department. One person (the planner) uses it. Every day, the planner
uploads an Excel file listing that day's transport requests (pickup times,
locations, vehicle type needed, event) with the Driver and Vehicle columns
blank. The app fills those two columns in — deciding which in-house driver
and vehicle to use, or which outside supplier to hire, for every single job
— and the planner reviews/edits before exporting a finished file to send to
the fleet supervisor, who manages the actual day separately (outside this
app, at least for now).

**This is explicitly NOT:**
- A multi-user or web-hosted system. One planner, one PC, no accounts, no
  server. This was a deliberate architecture decision (see Section 7).
- A system where AI makes final decisions. A deterministic rules engine
  produces the base plan; AI only proposes *suggestions* on top of it,
  which the planner explicitly accepts or rejects.

## 2. The real-world domain (why the rules are the way they are)

The company runs catering/event logistics (UAE-based, Dubai/Sharjah
references throughout the data). Key domain facts baked into the design:

- **Vehicles are not interchangeable.** Chiller trucks (temperature
  controlled, for food), open pickups (equipment), buses/vans (staff) are
  all physically different and a job requires a *specific* type.
- **One driver can use different vehicles across the same day.** A driver
  might take a chiller truck out in the morning, return it, and take a
  different vehicle out in the afternoon. Driver and vehicle are two
  independent schedulable resources with their own timelines, not a fixed
  pair.
- **In-house resources are tried first; outside suppliers are hired only
  as overflow**, specifically to avoid in-house drivers going into
  overtime.
- **Suppliers are hired dynamically, per day, not pre-assigned.** A
  supplier (e.g. "AL LAITH PASSENGER TRANSPORT") isn't a fixed roster of
  named trucks — the app decides at planning time how many separate units
  from that supplier are actually needed that day, and names them itself
  (see Section 6, "Supplier hiring/naming").
- **Events span multiple stages across a day** (morning equipment drop,
  midday stewarding, food dispatch, evening teardown), all sharing an
  event ID. Whether the same driver/vehicle should physically wait
  on-site between stages (saves a return trip) or come back and get
  reassigned elsewhere is a judgment call involving travel time, driver
  remaining hours, and fuel/time tradeoffs — this is explicitly the one
  piece of reasoning deferred to the AI layer, not the deterministic
  engine.
- **Fairness is measured in hours occupied, not job count.** A driver who
  waits on-site for one long event has worked comparably to a driver who
  did several short separate trips — the engine must not treat "one job"
  and "one long job" as equivalent for fairness purposes.

## 3. Architecture in one paragraph

Python desktop app, PySide6 (Qt) for the GUI, SQLite for all local
storage (one file, `fleetplanner.db`). No web server, no multi-user
sync. Three layers of decision-making, run in sequence, never merged
automatically:
1. **Deterministic rules engine** (`allocation_engine.py`) — hard
   constraints only (hours, shift windows, off-days, license/vehicle-type
   matching). No AI, no network calls, fully repeatable and testable.
2. **AI review layer** (`ai_review.py` + `maps_client.py`) — called only
   when the planner clicks "AI Review". Uses Claude (Anthropic API) plus
   Google Maps (Routes API, traffic-aware) to reason about event-chain
   stay-vs-cycle decisions and the planner's free-text day notes.
   Produces suggestions the planner accepts/rejects individually; never
   silently changes the plan.
3. **Decision-history digest** (`digest_generator.py`) — every
   accepted/rejected AI suggestion is logged forever locally
   (`decision_log` table), but only ever a small, periodically-refreshed
   summary (`preference_digest` table, capped ~400 words) is sent to
   Claude on future days. This is a deliberate cost/latency control: the
   raw log is never resent, so cost stays flat regardless of how much
   history accumulates.

## 4. Main modules and what each owns

| Module | Owns |
|---|---|
| `app/db.py` | All SQLite schema, migrations, and CRUD. Nothing else touches SQL directly. |
| `app/rules_parser.py` | Legacy free-text rule-line recognition (still used for the "AI context notes" free-text lists; NOT used for hard rules anymore — see Section 8). |
| `app/excel_import.py` | Reads the daily request Excel file into `Job` objects. Handles the real export's separate START DATE + TIME columns, event-ID extraction, footer-row filtering, text cleanup. |
| `app/allocation_engine.py` | The deterministic core. `DriverProfile`, `VehicleProfile`, `SupplierOffering`, `SupplierHire` dataclasses; `allocate()` is the main entry point. |
| `app/maps_client.py` | Wraps Google Routes API for traffic-aware travel time between two locations at a specific departure time. |
| `app/ai_review.py` | Builds the AI's context payload and calls Claude for event-chain/day-note reasoning. Returns structured suggestions. |
| `app/digest_generator.py` | Compresses `decision_log` into the small `preference_digest`, called manually by the planner (Settings tab). |
| `app/export.py` | Writes the finalized Driver/Vehicle values back into a **copy of the original uploaded workbook**, preserving all other formatting/data untouched. |
| `app/ui/*.py` | PySide6 screens — see Section 5. |
| `app/main.py` | Entry point: `db.init_db()` then launches `MainWindow`. |

## 5. UI tabs (in `MainWindow`, `app/ui/main_window.py`)

1. **Plan a Day** (`plan_day_tab.py`) — the daily workflow: upload Excel,
   day-notes free-text box, Run Planning, AI Review, Finalize Day,
   Export Filled Excel. Results table is sortable and filterable by
   driver/supplier.
2. **Drivers** (`drivers_tab.py`) — structured hard-rule fields (working
   hours/day, max working hours/day, shift (Morning/Evening), off days,
   monthly overtime cap, monthly hour
   target, license types) plus a free-text "AI context notes" list.
   Exclusion checkbox ("don't use tomorrow") with visual demotion.
3. **Suppliers** (`suppliers_tab.py`) — structured offerings (vehicle
   type + rate/hour + max available/day, repeatable per supplier) plus
   free-text AI notes. Same exclusion-toggle pattern.
4. **Vehicles** (`vehicles_tab.py`) — plate, type, notes, workshop
   toggle, and the same "don't use tomorrow" exclusion toggle.
5. **Locations** (`locations_tab.py`) — short-code → real-address
   mapping for accurate Maps lookups (e.g. "CPK" → "Central Production
   Kitchen, Al Quoz, Dubai").
6. **Settings** (`settings_tab.py`) — API key entry/testing for
   Anthropic and Google Maps, preferences-digest refresh control, and an
   optional PIN. The tab is genuinely *disabled* (not just switched away
   from) while locked — see Section 8 for why this matters.

`entity_rules_widget.py` is a **legacy shared widget**, superseded by the
dedicated `drivers_tab.py`/`suppliers_tab.py`. It is no longer wired into
`main_window.py` but was not deleted from the repo. Treat it as dead code
unless something has since re-adopted it — check `main_window.py`'s
imports to confirm current wiring before assuming otherwise.

## 6. Critical business logic to understand before changing anything

### Fairness (in-house)
`allocate()` always tries in-house drivers/vehicles first. Among
qualifying, available, non-overtime-violating drivers, it picks the one
with the **fewest occupied hours so far that run** (`min(candidates,
key=lambda d: d.occupied_seconds)`). This is *pure hours-based fairness*,
not job-count fairness.

### Supplier hiring/naming (dynamic, not pre-assigned)
Suppliers are configured with **rate/type/availability offerings only**
— never pre-named units. At planning time, for each job needing a
supplier:
1. Try to **reuse** an already-hired unit from any matching-type
   offering that's free at this time (priority: minimize headcount for
   the day, even if it means one supplier driver works more hours).
2. Only if no reuse is possible, **hire a new unit** — from the offering
   with the *lowest cumulative historical hours* (cross-day fairness
   among suppliers), respecting `max_available_per_day`.
3. Naming convention (confirmed explicitly by the user, not guessed):
   - 1st hire of the day: `SUPPLIER NAME` (no number)
   - 2nd hire: `SUPPLIER NAME 1`
   - Nth hire: `SUPPLIER NAME {N-1}`
   - Reusing an already-hired unit for a later job: `SAME <label>`
   - This numbering is **always applied by the software**, even though
     the user's own historical manually-planned reference files are
     inconsistent about it (some suppliers numbered, some not, in the
     old manual data) — the user explicitly confirmed the software
     should keep the consistent numbering logic regardless of what old
     manual files show.

### Driver hard rules — structured fields only, not free text
This is the single most important lesson learned in this project (see
Section 9, "bugs found"): **hard rules must live in structured database
columns**, not be inferred from free-typed text lines. An earlier design
tried to regex-match hard rules out of arbitrary planner-typed lines
(`rules_parser.py`); this silently failed whenever the planner's phrasing
didn't exactly match a pattern, and directly caused a real bug (a driver
scheduled well past their stated hours because the engine simply never
saw a rule it could enforce). The current model:
- `drivers.working_hours_per_day`, `drivers.max_working_hours_per_day`,
  `drivers.shift_period`, `drivers.off_days`,
  `drivers.max_overtime_hours_per_month`,
  `drivers.total_hours_per_month_target`, `drivers.license_types` are
  all dedicated columns, edited via explicit form fields with format
  hints (not detected from prose). (`drivers.shift_start` still exists in
  the schema for backward compatibility but is deprecated -- see "Shift
  enforcement" below.)
- The free-text `driver_rules` / `supplier_rules` tables (and
  `rules_parser.py`) still exist and are still shown in the UI, but are
  now explicitly scoped to **AI context only** — never enforced as hard
  constraints. This distinction is stated in the UI copy itself ("free
  text — context only, not enforced automatically").

### Overtime model (updated 2026-08-03, see CHANGELOG_AI.md Phase 10 for full history)
- `working_hours_per_day` is a driver's normal/baseline day. As of this
  session it's ALSO a hard daily **minimum**: if a driver is used at all
  on a given day, they must reach at least this many hours that day (see
  "Daily minimum hours" below) -- not just a target, an enforced rule.
- **The daily overtime ceiling is `max_working_hours_per_day`, a real
  per-driver field the planner sets alongside `working_hours_per_day`
  (e.g. 9/12), regardless of how much monthly overtime allowance the
  driver has left.** This field REPLACES a fixed module-level constant,
  `MAX_OVERTIME_HOURS_PER_DAY = 2.0`, that had no UI to change it --
  originally added after the project owner reported a driver getting
  jobs from 7 AM to 5 AM the next day (~22h) despite hard rules
  supposedly being enforced (root cause: the monthly-bucket check alone
  has no concept of "per day"). The project owner later asked for this
  to become a real configurable field rather than a fixed constant, so
  `MAX_OVERTIME_HOURS_PER_DAY` was removed entirely and
  `max_working_hours_per_day` took its place, checked in exactly the
  same position (before the monthly-bucket check, cumulative across
  however many jobs a driver picks up that day). If left blank, it
  falls back to `working_hours_per_day` (zero daily overtime, fail-closed)
  rather than reopening the old unlimited-single-day bug.
- `max_overtime_hours_per_month` is the actual monthly hard cap: `0` OR
  `None`/blank = **no overtime allowed at all** (working_hours_per_day
  becomes a strict daily ceiling); any positive number = that many hours
  of overtime allowed per month, tracked via `finalized_jobs` history
  (`db.get_driver_month_overtime_hours`, which sums *per-day excess over
  working_hours_per_day*, not raw totals) -- but capped per-day at
  `max_working_hours_per_day` regardless of how much of the monthly
  total remains, per the fix above. (Blank being treated as "no overtime"
  rather than "unlimited" was itself a bug fix, from an earlier session
  -- see CHANGELOG_AI.md Phase 8.)
- This is why `finalized_jobs` (populated by the "Finalize Day" button)
  matters for the MONTHLY side of this: without it, monthly overtime
  enforcement has no history to check against and behaves as if every
  driver starts every month at zero overtime. It does NOT affect the
  daily ceiling or the new daily minimum, which are independent of
  history.
- **Daily minimum hours (new 2026-08-03, HR-005 in the scheduling rules
  spec; WIDENED 2026-08-06, see CHANGELOG_AI.md Phase 14).** A driver used
  at all on a given day must reach at least `working_hours_per_day` that
  day. This can't be a simple per-job filter the way the daily ceiling is
  -- the engine assigns jobs one at a time in time order and doesn't know
  a driver's full-day total until the day's last job has been considered.
  It's enforced instead as a post-allocation repair pass
  (`allocation_engine._repair_minimum_daily_hours`, run in a loop up to 5
  times): for every driver left with a non-zero, under-minimum day, it
  tries to move ALL of that driver's jobs for that day to another
  qualifying driver with spare room (same license/off-day/shift/overlap/
  ceiling/monthly-cap checks as the main pass). If every job can move, the
  move is committed; if even one can't, the whole day is released to
  unresolved with an explicit note rather than silently keeping an illegal
  short day.
  **2026-08-06 update:** this pass originally skipped ANY day containing a
  "Same Driver" grouped job entirely (planner-flagged, left alone). That
  was correct in principle -- a group's own hours are sometimes genuinely
  the cause of a shortfall and shouldn't be force-split -- but on a real
  day where 84% of rows were grouped, it meant this pass almost never ran
  at all, and severe hour imbalance went completely unaddressed. Confirmed
  with the project owner and widened: a grouped day can now be moved WHOLE
  onto one other qualifying driver (never split, so the "same driver"
  instruction is still honored), with SD-004 vehicle-type consistency
  enforced on the receiving driver via a new helper,
  `_established_group_vehicle_type()`. Still doesn't attempt a supplier
  fallback (flagged for manual review instead -- that's a planner cost
  decision, not one this pass should make on its own).
  **Known limitation, disclosed to and accepted by the project owner:**
  this is a best-effort greedy heuristic, not a global optimizer. Widening
  it to move whole groups initially caused a real, observed thrashing bug
  (a short group moved onto a driver with room could make THAT driver's
  own day look under-minimum to the next pass, triggering yet another
  group to bounce onto them) -- fixed with a `settled_job_ids` stability
  guard (a job, once moved, is never moved again within the same
  `allocate()` call). A second, smaller processing-order bug (the search
  for a new driver picked the first alphabetical match rather than the
  most-free one) was also found and fixed the same session. Validated
  against the real `UNPLANNED.xlsx` + `fleetplanner.db`: driver-hour
  spread went from 2.0h-16.0h to 4.0h-11.0h on the same real day (human
  reference: 5.0h-12.0h). Not yet full parity -- a few drivers can still
  land under minimum or fully idle, as an artifact of greedy processing
  order, not a further known bug. See CHANGELOG_AI.md Phase 14 for the
  full root-cause writeup.

### Shift enforcement (redesigned 2026-08-03, see CHANGELOG_AI.md Phase 10)
**The model described here changed completely this session.** The
previous model (`shift_start`, an exact clock time set by the planner
before planning) is preserved below as history since the reasoning is
still relevant context, but is no longer how the software works --
see the new model first.

**Current model:** the planner never fixes an exact shift-start clock
time before planning. They just mark a driver `shift_period` = `"morning"`,
`"evening"`, or leave it blank (no restriction) -- a simple label, not a
computed rotation. The engine enforces this as a window (morning =
before 12:00, evening = 12:00 onward -- see `SHIFT_PERIOD_EVENING_CUTOFF_HOUR`
in `allocation_engine.py` if this cutoff ever needs to change) via
`_job_matches_shift_period()`. The driver's actual first-job time for a
given day comes out of whatever the plan happens to give them first --
it is only known, and can only be announced to the driver, after
planning, never chosen in advance. There is deliberately NO automatic
15-day-morning/15-day-evening rotation and no off-day-triggered
transition rule -- this was discussed and explicitly rejected by the
project owner once it became clear the planner would be setting
morning/evening manually anyway: "if the planner is deciding which
driver to bring in morning and evening we dont have to track this." The
planner can change a driver's label whenever they want; the software
does not compute, enforce, or remember any rotation schedule.

**Historical context (previous `shift_start` model, now deprecated):** a
job starting before a driver's configured `shift_start` (an exact clock
time, e.g. "07:00 AM") could never be assigned to that driver. This field
was stored correctly and shown in the Drivers tab UI, but was for a time
never actually wired into `DriverProfile`, `build_driver_profiles`, or
`allocate()`'s candidate-filtering loop — a real bug, confirmed directly
by the project owner, fixed in an earlier session by adding the missing
`DriverProfile.shift_start` field, populating it in `build_driver_profiles`,
and a `_job_is_before_shift_start()` / `_parse_shift_start_time()` pair
called in the candidate loop. That whole exact-time model (field, dataclass
field, parsing/enforcement functions) was then replaced entirely by
`shift_period` this session, per the "Current model" above -- the
`drivers.shift_start` database column still exists (so old data isn't
lost) but is no longer read by any code. See `CHANGELOG_AI.md` for why
this class of bug (field exists, stored correctly, UI reads/writes it,
but the *engine* never actually checks it) is worth a deliberate audit
pass whenever a new structured field is added, regardless of the shift
model change -- the underlying lesson (verify all three of: database
column, dataclass field populated in `build_*_profiles`, and actually
read inside `allocate()`'s candidate loop) still applies to every hard
rule in this project, including the new `shift_period` and
`max_working_hours_per_day` fields added this session.


### "Same Driver" column (planner-flagged, deterministic, not AI)
The daily request Excel file has a "Same Driver" column. The planner
pastes the same text (typically the Event text) onto every row they want
one driver to handle back and forth, rather than treating each row as an
independent job for fairness/overlap purposes. This is enforced in
`allocation_engine.py`, not the AI layer, because it's an explicit
planner instruction (Rule 3/9), not a judgment call. Confirmed rules
(agreed with the project owner before implementing):
- Overlapping times between two rows sharing the same flagged value are
  allowed for the same driver (or reused supplier unit) -- this is NOT a
  double-booking conflict, since the planner has already judged it's one
  person's job. Overlap against anything outside that specific group is
  still a hard conflict as normal.
- No hard-coded time-based or vehicle-type-based split. The engine
  always tries to reuse the driver(s)/supplier unit(s) already assigned
  to that group before adding a new one, mirroring the existing supplier
  reuse-before-hire pattern. It only adds an additional driver/unit when
  none of the group's current ones still qualify for the next row. This
  was validated against the real `PLANNED.xlsx`/`UNPLANNED.xlsx` files
  and a synthetic test suite (`test_same_driver.py`,
  `test_same_driver_supplier.py`) covering: same-group overlap allowed,
  single-driver-covers-everything preferred when possible, forced split
  when no driver is licensed for every vehicle type in the group, and a
  regression check confirming normal (unflagged) overlapping jobs still
  conflict as before.
- **Group-leader selection now looks ahead (added 2026-08-06, SD-005 in
  the scheduling rules spec).** A fresh group's opening row used to pick
  its driver purely by who had the fewest occupied hours AT THAT INSTANT
  -- with no idea whether the group about to land on them was a 2h errand
  or an 11h event, and no way to reconsider once picked (see SD-002/
  SD-003 below). Confirmed as a real driver of hour imbalance on a real
  day. Each group's total merged hours are now precomputed once before
  the main loop, and a fresh group's opening row picks whichever
  candidate minimizes `occupied_seconds + the group's projected total
  hours`, not just the opening row's own duration. A group's second and
  later rows are unaffected -- they still prefer the group's
  already-established driver first.
- Hour totals use the TRUE UNION of a driver's time intervals
  (`_merged_hours()`), not a naive sum -- two rows in the same group that
  overlap in time (e.g. two simultaneous pickups on one truck) count
  once, not once each. This was originally a documented simplification
  (sum, deliberately erring toward overstating hours per Rule 6) but real
  `PLANNED.xlsx` data confirmed 2026-08-03 the overstatement was large
  enough to falsely trip the daily ceiling in routine cases (one real
  driver: ~17h "occupied" vs. ~11h true) -- fixed the same day. See
  CHANGELOG_AI.md Phase 12.
- `export.py` was fixed at the same time to read a new clean
  `Job.assigned_driver_name` field instead of parsing the driver's name
  back out of `assignment_note` -- `assignment_note` is now allowed to
  contain extra human-readable context (e.g. "[Same Driver group]")
  without that leaking into the exported file's Driver cell.

### Vehicle-type matching is exact string comparison
`_type_matches()` does case-insensitive, whitespace-normalized *exact*
string comparison between a job's required type, a driver's license
types, a vehicle's type, and a supplier offering's type. **There is no
fuzzy matching.** This was a deliberate choice (fuzzy matching risks
silently conflating genuinely different vehicle configurations), but it
means real-world wording inconsistency (confirmed example: a vehicle
entered as "23 Seater Bus" vs. a job requiring "23 Seated Bus") silently
produces zero matches with no error. The documented mitigation is
process, not code: vehicle-type text must be copy-pasted exactly from
the source Excel's "VEHICLE TYPE" column into every place it's entered
(Vehicles tab, driver license types, supplier offerings) — never
retyped from memory.

### Excel import quirks handled
- The real export format has **separate `START DATE` and `TIME`**
  columns (`TIME` like `"08:00 - 15:00"`), not one combined cell as an
  earlier version assumed. Both formats are supported
  (`_combine_date_and_time` for the real format, `_parse_datetime_range`
  as a fallback for the old combined-cell format).
- Overnight jobs (end time earlier than start time, e.g. `22:00 - 04:00`)
  correctly roll the end datetime to the next calendar day.
- Footer/note rows (duty-time rosters, "vehicles in workshop" lists) are
  filtered out by requiring the SR# column to parse as a plain integer —
  **not** by checking for blank cells, since footer rows often have text
  in other columns.
- Vehicle-type and other free-text cells get whitespace/newline
  normalization (`_clean_text`) because Excel's wrapped-cell text often
  contains embedded `\n` characters that would otherwise break exact
  string matching.

## 7. Key design decisions and why (in chronological order of the
   conversation, so the reasoning is traceable)

1. **Single-user standalone desktop app, not a web app.** Originally
   discussed as a multi-user web system; the person clarified only one
   planner uses the actual software, everyone else (supervisor, drivers)
   only ever sees the *exported file*. This eliminated the need for
   accounts, hosting, or real-time sync, and simplified the whole stack
   to Python + PySide6 + SQLite.
2. **Two-stage AI, not automatic/bundled.** Run Planning (deterministic)
   and AI Review are deliberately separate button presses, not one
   combined action. Explicit reasoning: while the deterministic engine
   was still being hardened (multiple real bugs found and fixed across
   this project), keeping AI out of the loop made every bug traceable to
   either code or AI, not an ambiguous mix. A combined "Plan My Day"
   one-click shortcut was discussed as a *future* addition once the
   deterministic engine is fully proven — not built yet.
3. **AI never finalizes anything.** Every AI suggestion requires an
   explicit Accept/Reject click from the planner. This was non-negotiable
   from the first design discussion.
4. **Decision-history digest, not raw-log replay.** When asked whether
   the AI "learns over time," the honest answer is no — each API call is
   stateless. To let the *system* (not the model) reflect accumulated
   real-world planner decisions without cost/latency growing unboundedly
   over years, a two-tier history was designed: full log stored locally
   forever (never sent to the API), small fixed-size digest periodically
   refreshed and the only thing ever sent to Claude for daily reasoning.
5. **Structured hard-rule fields over free-text rule parsing.** Directly
   caused by real bugs (see Section 9). This was a mid-project pivot,
   not the original design — the first Drivers/Suppliers tabs used a
   generic free-text "rule lines" widget (`entity_rules_widget.py`) with
   regex-based recognition (`rules_parser.py`). That approach is now
   only used for AI-context notes, not hard rules.
6. **Dynamic supplier hiring/naming, not pre-named units.** The original
   supplier model required the planner to pre-name individual trucks
   ("Unit: PINK PEPPER CHILLER TRUCK #1"). This was identified as
   backwards — the numbering is something the *app* should decide based
   on the day's actual demand, not something the planner pre-declares.
   Rebuilt around simple rate/type/availability offerings.
7. **Export preserves the original file exactly.** The output must be
   the *same* Excel file the planner uploaded, byte-identical except the
   Vehicle and Driver cells — no reformatting, no regenerating from a
   template. Implemented by loading the original workbook with
   `openpyxl` (which preserves styles) and only ever writing into two
   specific columns per matched row.
8. **Settings PIN gates by disabling the tab, not by switch-then-revert.**
   An earlier implementation switched to the Settings tab and reverted if
   the PIN was wrong — which could flash tab contents briefly. Fixed by
   disabling the tab entirely (`setTabEnabled(False)`) while locked, with
   a corner "Unlock Settings" button as the only way in.
9. **Idempotent schema migrations, not "delete your database every
   time."** After several rounds of asking the user to delete their
   local database because of schema changes, a lightweight
   `_MIGRATIONS` list + `ALTER TABLE ... ADD COLUMN` (swallowing
   "duplicate column" errors) was added so small future field additions
   don't require data loss. Only genuine structural changes still need a
   fresh database.

## 8. Coding conventions used throughout

- **Every module has a substantial module-level docstring** explaining
  *why* it exists and what it deliberately does/doesn't do — not just
  what functions it has. This convention should be continued.
- **Dataclasses for runtime state** (`DriverProfile`, `VehicleProfile`,
  `SupplierOffering`, `SupplierHire`, `Job`) — these are *not* database
  rows; they're built fresh from the database at the start of each
  planning run (`build_driver_profiles`, `build_vehicle_profiles`,
  `build_supplier_offerings` in `allocation_engine.py`) and mutated
  in-memory during `allocate()`. Never assume a `DriverProfile` field is
  automatically synced with the database — it's a snapshot.
- **`db.py` functions always take `conn` as the first argument.** No
  module-level global connection. UI code holds one `conn` per
  application session (created once in `main.py`, passed down through
  every widget's constructor).
- **UI widgets receive `conn` in `__init__` and call `db.*` functions
  directly** — there is no separate service/repository layer between UI
  and `db.py`. This is intentional given the single-user, single-process
  nature of the app; do not add an abstraction layer without a clear
  reason.
- **Errors from external services (Claude, Google Maps) are wrapped in
  dedicated exception classes** (`AIReviewError`, `MapsClientError`,
  `DigestError`) and always caught at the UI layer with a
  `QMessageBox`, never allowed to crash the app or show a raw traceback
  to the planner.
- **All free-text rule-line matching is case-insensitive and
  whitespace-normalized** (see `_type_matches`, `_normalize_header`,
  `_clean_text`) — but never fuzzy/approximate. Exact-after-normalization
  only.
- **Every non-trivial function added during this project was unit-tested
  with a throwaway script before being considered done** — using
  synthetic data first, then (from the point the user started sharing
  real files) validated against the user's actual uploaded Excel files
  and real database snapshot. This is the expected standard going
  forward: don't consider a change complete until it's been run against
  something, not just read for correctness.

## 9. Real bugs found and fixed during this project (know these before
   touching related code)

1. **Date/time parsing assumed the wrong Excel format.** The real export
   has separate `START DATE` + `TIME` columns; the importer originally
   only handled a single combined cell. Fixed in `excel_import.py`
   (`_parse_date_value`, `_parse_time_range_only`,
   `_combine_date_and_time`), with the old combined-cell parser kept as
   a fallback.
2. **Footer/note rows were treated as failed job rows** instead of being
   skipped, because the skip condition checked for blank SR#/Order
   fields, but footer rows (e.g. a duty-time roster) often have text in
   the SR# column itself. Fixed by requiring SR# to parse as a plain
   integer.
3. **Vehicle plate was assigned but never displayed** in the results
   table — the engine always supported one driver using different
   vehicles across a day, but there was no "Vehicle" column shown, only
   driver name. Fixed by adding `Job.assigned_vehicle_plate` and a
   dedicated table column.
4. **Duplicate driver/supplier names were case-sensitive.** SQLite's
   default `UNIQUE` constraint is case-sensitive, so "DEEPAK DEWAN" and
   "Deepak Dewan" were treated as different people. Fixed with `COLLATE
   NOCASE` on the relevant columns.
5. **A driver's hour cap silently went unenforced** because it was typed
   as a free-text rule line in a phrasing the regex parser didn't
   recognize (e.g. "9 hours duty time" instead of the exact expected
   pattern). This is the bug that motivated the entire structured
   hard-rule-fields redesign (Section 6/7).
6. **`shift_start` was stored but never enforced.** Added as a
   structured field and read into `DriverProfile`, but the actual
   `allocate()` candidate loop never checked it — a driver whose shift
   started at 6 PM could still be assigned a 10 AM job. Root-caused by
   testing against the user's real data and finding the field was
   missing from the `DriverProfile` dataclass entirely (a copy/paste
   omission during a larger rewrite), so `build_driver_profiles` was
   silently dropping it even though the database column existed and the
   UI form read/wrote it correctly. Fixed by adding the field to the
   dataclass, reading it in `build_driver_profiles`, and calling
   `_job_is_before_shift_start()` in the candidate filter.
7. **The Settings-PIN tab could flash its contents before the PIN check
   ran**, because the original implementation switched tabs first and
   reverted after a failed check. Fixed by disabling the tab outright
   while locked.
8. **License-type and vehicle-type text mismatches** (e.g. "23 Seater
   Bus" vehicle vs. a job requiring "23 Seated Bus") cause silent
   zero-match failures because matching is exact-string, not fuzzy. Not
   a code bug per se, but a real, recurring, hard-to-spot failure mode —
   documented as a process requirement (copy-paste exact text) rather
   than "fixed," since fuzzy-matching was deliberately rejected as
   riskier than the problem it would solve.

9. **`max_overtime_hours_per_month = None` (blank) silently disabled
   `working_hours_per_day` enforcement entirely**, rather than meaning
   "no restriction beyond the daily baseline" as the field's own
   semantics implied. The whole hours-check block in `allocate()`
   required BOTH `working_hours_per_day` and `max_overtime_hours_per_month`
   to be set before checking anything — so a driver with a daily limit
   configured but no monthly overtime cap could be given unlimited hours
   in a single day. Found while auditing all three "hard rule" fields at
   the project owner's request (prompted by the `shift_start` bug above),
   proven with a test giving one driver 21.5 hours in one day with zero
   rejection. Confirmed with the project owner and fixed: a blank
   overtime cap is now treated the same as an explicit `0` (no overtime
   allowed), not as "unlimited." See the "Overtime model" note in
   Section 7 for the corrected semantics.
10. **License-type enforcement (`_driver_qualifies_for_type`,
    `_type_matches`) was audited at the same time and found to be
    correctly implemented already** — a driver not licensed for a job's
    required vehicle type, or with no license types configured at all,
    is correctly refused. No code change was needed here; documenting
    this explicitly since it was specifically asked about and checked
    end-to-end with a test, not just read for correctness. If it's still
    misbehaving in the project owner's live copy, the cause is most
    likely a text-mismatch data issue (see item 8) rather than a logic
    bug in this function.

11. **No hard ceiling on how much of a driver's monthly overtime
    allowance could be spent in a single day.** Reported directly by the
    project owner as a real, observed symptom: a driver was given jobs
    from 7 AM to 5 AM the next day (~22 hours) despite hard rules
    supposedly being enforced. Root cause: the monthly-bucket overtime
    check (`month_overtime_so_far + today's overtime <=
    max_overtime_hours_per_month`) has no per-day sub-limit — with most
    real drivers configured for 60h/month overtime and an empty
    `finalized_jobs` history (so `month_overtime_so_far` starts at 0
    every time), a single day could consume 13+ hours of that 60-hour
    budget in one sitting with nothing to stop it. Confirmed with the
    project owner (2 hours/day) and fixed by adding
    `MAX_OVERTIME_HOURS_PER_DAY = 2.0` as a hard per-day ceiling, checked
    before the monthly-bucket logic, cumulative across however many jobs
    a driver picks up in one day. Proven with a test reproducing the
    exact 7 AM–5 AM scenario and confirming it's now rejected, plus
    exact-boundary tests (11h exactly = OK, 11h01m = rejected).
    **Superseded 2026-08-03 -- see item 12 below and the "Overtime model"
    section (Section 6): `MAX_OVERTIME_HOURS_PER_DAY` was removed and
    replaced by a real per-driver `max_working_hours_per_day` field.**

12. **HR-002/HR-005 rework, 2026-08-03: shift redesigned, daily ceiling
    made a real per-driver field, new hard daily minimum added.** Not a
    bug fix -- a deliberate rework requested by the project owner. Full
    write-up in `CHANGELOG_AI.md` Phase 10 and Section 6 above
    ("Overtime model" / "Shift enforcement"). Summary: `shift_start`
    (exact time) replaced by `shift_period` (Morning/Evening label, no
    rotation); `MAX_OVERTIME_HOURS_PER_DAY` constant removed, replaced by
    `max_working_hours_per_day` (per-driver, e.g. 12 paired with
    `working_hours_per_day`=9); new hard rule that a driver used at all
    on a day must reach at least `working_hours_per_day` that day,
    enforced via a post-allocation repair pass since it can't be a
    per-job filter. This repair pass is a best-effort heuristic not yet
    validated against real full-day volume -- see Section 12 below and
    `NEXT_SESSION.md`.

13. **HR-005 essentially defeated on real data by its own "skip grouped
    days" scope, 2026-08-06.** The project owner reported the software
    still wasn't reproducing the balance in a real human-planned
    `PLANNED.xlsx` (some drivers at 12h, others at 2h). Confirmed by
    cloning the real repo and running `allocate()` against the real
    `UNPLANNED.xlsx` + `fleetplanner.db`: 84% of that day's rows carried
    a "Same Driver" tag, and HR-005's repair pass unconditionally skipped
    any driver-day containing a grouped job -- correct in principle (see
    item 12 and Phase 12), but on a day this heavily grouped it meant the
    pass almost never ran at all. Fixed with two changes approved
    together by the project owner (Rule 16 -- asked, not assumed): (a) a
    fresh "Same Driver" group's opening row now picks its driver by
    projecting the group's TOTAL merged hours, not just the opening row's
    duration (SD-005 in the scheduling rules spec); (b) HR-005's repair
    pass can now move a grouped day WHOLE onto one other qualifying
    driver (never split), with SD-004 vehicle-type consistency enforced
    on the receiving driver via `_established_group_vehicle_type()`.
    **Two further real bugs found and fixed while building this, before
    reaching the project owner:** widening the repair pass to move whole
    groups caused genuine thrashing -- a short group moved onto a driver
    with room could make that driver's own day look under-minimum to the
    very next pass, so a different group got moved onto them next, and so
    on, never settling (caught by tracing driver state pass-by-pass
    against the real file). Fixed with a `settled_job_ids` stability
    guard: once the repair pass moves a job, it's never moved again
    within the same `allocate()` call. Separately, the repair pass's
    search for a new driver picked the first alphabetically-listed
    eligible driver rather than the most-free one, which on real data
    meant a driver who simply hadn't had their own fix processed YET in
    the same pass could get skipped in favor of one freed moments earlier
    by an unrelated move -- fixed by searching least-occupied-first.
    Real-data result: driver-hour spread went from 2.0h-16.0h to
    4.0h-11.0h on the same real day (human reference: 5.0h-12.0h) -- a
    real, verified improvement, though not yet full parity; a few
    drivers can still land under minimum or fully idle as a processing-
    order artifact of the greedy heuristic, disclosed honestly rather
    than overstated. Full write-up: `CHANGELOG_AI.md` Phase 14,
    scheduling rules spec v10 (SD-005, updated HR-005/SE-003, NEW-008).
    A separate, unrelated data-quality issue was also surfaced in the
    same investigation and flagged (not fixed): one driver's
    `license_types` contains a literal embedded newline before
    "(with lift)", silently defeating exact-string matching the same way
    the documented "Seated" vs "Seater" case did -- see NEW-008 and
    Section 6/9's existing exact-matching precedent.

14. **Two new allocation strategies built and compared, idle-driver
    rescue added, and the newline data issue turned out to be
    fleet-wide, 2026-08-06 (same day as item 13).** After item 13, the
    project owner raised two structural concerns: "Same Driver" grouping
    felt like it was ruling the allocation rather than assisting it
    (though deleting it made results worse, confirming it's genuinely
    load-bearing), and driver selection was effectively alphabetical in
    places rather than merit-based. Rather than patch `allocate()`
    further, two full alternative strategies were built alongside it
    (never replacing it, per Rule 13): `allocate_by_merit()`
    (shift-partitioned, pairs-only Same-Driver pre-merge, event-diverse
    seeding with license-scarcity override) and `allocate_by_anchor()`
    (anchor each driver's first AND last job intentionally -- most-
    constrained drivers first -- then fill the middle, then a bounded
    swap-repair search). Real-data result: `allocate_by_merit`
    underperformed the baseline (16 vs. 12 unresolved); `allocate_by_anchor`
    tied on unresolved count but used all 9 active drivers instead of
    leaving 2 idle. Also added, and wired into BOTH `allocate()` and the
    new strategies: `_rebalance_idle_drivers()`, making "every driver has
    real work" a first-class goal after a direct comparison against the
    real `PLANNED.xlsx` showed the software leaving drivers at a legal
    but unrealistic 0h. Two real bugs were found and fixed while building
    this (an oscillation where a genuinely-unfixable released job got
    immediately rescued right back onto the same driver; a donor
    remaining-hours miscalculation that let a consolidation get silently
    undone) -- both caught by the EXISTING test suite, not new tests,
    underscoring the value of that suite. Separately, investigating the
    NEW-008 newline bug found it was NOT isolated to one driver as first
    thought -- it was present in all 11 active drivers' `license_types`,
    one vehicle's `vehicle_type`, and two excluded drivers (15 records,
    clearly copy-pasted from the same wrapped Excel cell). Fixed as a
    pure data correction (not a code change). Real result: 2 of 3
    affected rows now resolve correctly, but total unresolved count
    didn't drop overall -- more legal options reshuffled the whole
    downstream allocation and created different shortfalls elsewhere, the
    honest signature of a greedy heuristic rather than a new bug. The
    10-Ton-Chiller-Truck scarcity flagged alongside NEW-008 was confirmed
    as a genuine capacity constraint (one physical vehicle, no matching
    supplier offering exists) -- not a data or code issue, reported to
    the project owner as a real gap to close if they want to. Full
    write-up: `CHANGELOG_AI.md` Phase 15, scheduling rules spec v11.

## 12. Current project status (as of the end of this conversation)

**Built, tested, working:**
- Excel import (real format), event-ID grouping, deterministic
  allocation engine (hours-fairness, license-type matching, shift-period
  and off-day enforcement, daily-minimum and daily/monthly-overtime-aware
  hour capping, dynamic supplier hiring/naming with reuse priority)
- Drivers/Suppliers/Vehicles/Locations tabs with structured hard rules
  and exclusion toggles
- AI Review layer (Claude + Google Maps) with Accept/Reject suggestion
  UI and decision logging
- Preferences digest (cost-bounded cross-day AI context)
- Finalize Day (persists to `finalized_jobs`, the basis for monthly
  overtime and cross-day supplier fairness)
- Export Filled Excel (preserves original file exactly)
- Results table sorting/filtering
- Settings PIN gating
- Real-data validation performed directly against the user's actual
  `fleetplanner.db` and a real day's `UNPLANNED.xlsx`/`PLANNED.xlsx`
  pair (see `CHANGELOG_AI.md` for the specific findings from that
  session, most of which turned out to be **data configuration issues
  in the user's own database**, not code bugs — see Section 6's
  "Vehicle-type matching" and Section 9 item 8).

**Built, tested, EXPERIMENTAL -- not wired into the UI, not yet
production-ready (2026-08-06, see Section 9 item 14 and
CHANGELOG_AI.md Phase 15):**
- `allocate_by_merit()` -- shift-partitioned strategy with event-diverse
  seeding. Underperforms the baseline `allocate()` on real data (16 vs.
  12 unresolved) -- kept in the codebase as a tested alternative, not
  recommended for use as-is.
- `allocate_by_anchor()` -- anchors each driver's first and last job
  intentionally (most-constrained drivers first), then a bounded
  swap-repair search. Ties the baseline on unresolved count but uses all
  9 active drivers instead of leaving 2 idle -- directionally promising,
  not yet proven better overall.
- `_rebalance_idle_drivers()` -- makes "every driver has real work" a
  first-class goal; wired into all three strategies' rearrangement
  loops. Two real bugs were found and fixed building this (see item 14).
- **None of the above have dedicated synthetic test files yet** -- all
  validation so far is direct real-data testing against `UNPLANNED.xlsx`
  + `fleetplanner.db`. This is a real, disclosed gap, not an oversight:
  see NEXT_SESSION.md.
- `plan_day_tab.py` still calls only `allocate()` -- switching the UI to
  either new strategy has not been discussed or decided.

**Explicitly NOT yet built** (see `NEXT_SESSION.md` for prioritization):
- PDF export (Excel export is done and preserves formatting; PDF likely
  needs Excel COM automation via `pywin32` since the target machine is
  Windows and very likely has Excel installed — this was flagged but not
  implemented)
- The HR-005 daily-minimum-hours repair pass (added 2026-08-03, widened
  2026-08-06) has now been validated against a real day's full job volume
  (`UNPLANNED.xlsx` + `fleetplanner.db`, see item 13 above) -- it's no
  longer synthetic-only. Real result: driver-hour spread compressed from
  2.0h-16.0h to 4.0h-11.0h against a 5.0h-12.0h human-planned reference.
  Not yet full parity, and still a greedy heuristic (not a global
  optimizer) -- see item 13 and `NEXT_SESSION.md` for the honest
  remaining gap and what a further fix would need.
- Reporting each driver's actual first-job time back to them after
  planning (spec SS-003), and showing month-to-date overtime-so-far on
  the Drivers tab (spec NEW-006) — both flagged as small, well-scoped
  follow-ups from the 2026-08-03 rework, not built yet.
- Vehicle maintenance/inspection log — a planner-suggested feature,
  explicitly deferred until the core planning flow is solid. Schema-wise
  this is independent of everything else (a new table hanging off
  `vehicles`), so it can be added later without touching existing code.
- The "restrict today's planning to a shortlist of drivers/suppliers"
  toggle — designed and agreed (per-day, optional, defaults to
  everyone), not yet implemented in the UI (the `allocate()` function
  signature already supports `allowed_driver_ids`/`allowed_supplier_ids`
  parameters for this, unused by the UI currently)
- Combined one-click "Plan My Day" (Run Planning + AI Review merged) —
  intentionally deferred until the deterministic engine is proven
  reliable over more real-world testing
- Off-day/comp-day planner UI — the `off_day_log` and `comp_days` tables
  exist in the schema (built early, before the structured hard-rule
  redesign) but have **no CRUD functions or UI wired to them at all**.
  This is stale/orphaned schema, not a working feature — treat as
  not-yet-implemented, not as "implemented but unused."

## 11. Important assumptions baked into the current design

- The planner's PC has internet access (needed for Claude/Maps calls,
  even though the app itself is a standalone desktop install).
- The planner has (or will obtain) their own Anthropic and Google Maps
  API keys — the app does not proxy or share credentials.
- Only one calendar day is planned at a time; the whole allocation model
  (fairness, overlap checking) operates on a single `jobs` list assumed
  to represent one operational day (which may span into the early hours
  of the next calendar date for overnight jobs).
- The uploaded Excel file's header row exactly matches the expected
  column names (case/whitespace-normalized) — `excel_import.py`'s
  `_HEADER_MAP` is the single source of truth for recognized headers.
  This is a real fragility point: an unrecognized header silently means
  that column contributes nothing (not an error).
- Windows is the only target OS (explicitly confirmed by the user).

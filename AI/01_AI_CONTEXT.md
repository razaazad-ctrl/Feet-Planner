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
   hours/day, shift start, off days, monthly overtime cap, monthly hour
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
- `drivers.working_hours_per_day`, `drivers.shift_start`,
  `drivers.off_days`, `drivers.max_overtime_hours_per_month`,
  `drivers.total_hours_per_month_target`, `drivers.license_types` are
  all dedicated columns, edited via explicit form fields with format
  hints (not detected from prose).
- The free-text `driver_rules` / `supplier_rules` tables (and
  `rules_parser.py`) still exist and are still shown in the UI, but are
  now explicitly scoped to **AI context only** — never enforced as hard
  constraints. This distinction is stated in the UI copy itself ("free
  text — context only, not enforced automatically").

### Overtime model
- `working_hours_per_day` is a **baseline**, not a hard daily ceiling by
  itself.
- `max_overtime_hours_per_month` is the actual hard cap: `None`/blank =
  **unlimited overtime allowed**; `0` = **no overtime allowed at all**
  (working_hours_per_day becomes a strict daily ceiling); any positive
  number = that many hours of overtime allowed per month, tracked via
  `finalized_jobs` history (`db.get_driver_month_overtime_hours`, which
  sums *per-day excess over working_hours_per_day*, not raw totals).
- This is why `finalized_jobs` (populated by the "Finalize Day" button)
  matters: without it, monthly overtime enforcement has no history to
  check against and behaves as if every driver starts every month at
  zero overtime.

### Shift start enforcement
A job starting before a driver's configured `shift_start` must never be
assigned to that driver, regardless of how few hours they've logged.
**This was implemented as a stored field but never actually wired into
`allocate()`'s candidate-filtering loop in an earlier pass** — a real
bug, found and fixed via `_job_is_before_shift_start()`, now called in
the candidate loop. See `CHANGELOG_AI.md` and `NEXT_SESSION.md` for why
this class of bug (field exists, stored correctly, UI reads/writes it,
but the *engine* never actually checks it) is the single most important
thing to watch for when adding new structured fields.

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

## 10. Current project status (as of the end of this conversation)

**Built, tested, working:**
- Excel import (real format), event-ID grouping, deterministic
  allocation engine (hours-fairness, license-type matching, shift-start
  and off-day enforcement, monthly-overtime-aware hour capping, dynamic
  supplier hiring/naming with reuse priority)
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

**Explicitly NOT yet built** (see `NEXT_SESSION.md` for prioritization):
- PDF export (Excel export is done and preserves formatting; PDF likely
  needs Excel COM automation via `pywin32` since the target machine is
  Windows and very likely has Excel installed — this was flagged but not
  implemented)
- Driver shift rotation (e.g. "15 days morning shift, then 15 days
  afternoon") — explicitly deferred until enough real `finalized_jobs`
  history exists to derive it from, per the user's own stated preference
  (not a fixed calendar-anchored rule, but inferred from history)
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

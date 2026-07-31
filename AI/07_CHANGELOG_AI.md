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

## Documentation phase (this file and its siblings)

- At the user's request, project knowledge was extracted from this
  conversation into a permanent documentation package
  (`AI_CONTEXT.md`, `ARCHITECTURE.md`, `DATABASE.md`, this file,
  `NEXT_SESSION.md`, `AI_INDEX.json`) intended to fully replace this
  conversation as the onboarding source for any future AI assistant
  session working on this repository.

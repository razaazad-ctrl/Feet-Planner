# ARCHITECTURE.md — Fleet Planner

Structural reference. Read `AI_CONTEXT.md` first for the *why*; this file
is the *what/where*.

## 1. Folder structure

```
FleetPlanner/
├── fleetplanner.db              # SQLite database, created at runtime next
│                                 # to the app (or wherever main.py is run
│                                 # from). NOT checked into version control
│                                 # in normal use, but a snapshot has been
│                                 # committed to the linked GitHub repo
│                                 # specifically for AI testing purposes.
├── requirements.txt              # PySide6, openpyxl, anthropic, requests
├── README.md                     # Human-facing setup/run instructions
├── app/
│   ├── __init__.py                # empty, marks package
│   ├── main.py                    # Entry point: db.init_db() -> MainWindow
│   ├── db.py                      # ALL SQLite schema, migrations, CRUD
│   ├── rules_parser.py            # Free-text rule-line recognizer
│   │                                (AI-context notes only, NOT hard rules)
│   ├── excel_import.py            # Daily request-file reader -> Job objects
│   ├── allocation_engine.py       # Deterministic core: allocate()
│   ├── maps_client.py             # Google Routes API wrapper
│   ├── ai_review.py               # Claude call: event-chain/day-note review
│   ├── digest_generator.py        # Compresses decision_log -> preference_digest
│   ├── export.py                  # Writes results back into original workbook
│   └── ui/
│       ├── __init__.py
│       ├── main_window.py         # QMainWindow, tab container, PIN gating
│       ├── plan_day_tab.py        # Daily workflow screen (largest UI file)
│       ├── drivers_tab.py         # Structured driver hard rules + AI notes
│       ├── suppliers_tab.py       # Structured supplier offerings + AI notes
│       ├── vehicles_tab.py        # Vehicle roster + workshop/exclusion toggle
│       ├── locations_tab.py       # Short-code -> address mapping
│       ├── settings_tab.py        # API keys, PIN, digest refresh control
│       └── entity_rules_widget.py # LEGACY/unused generic widget (see below)
```

**`entity_rules_widget.py` status:** built early as a shared
Drivers/Suppliers widget using free-text rule lines. Superseded by the
dedicated `drivers_tab.py` and `suppliers_tab.py` once hard rules moved
to structured fields. Not imported by `main_window.py` as of the last
known state — verify current imports before assuming it's dead, since a
future session could theoretically have re-adopted it, but as documented
here it is orphaned code.

## 2. Module responsibility boundaries (who is allowed to do what)

| Layer | Responsibility | Must NOT do |
|---|---|---|
| `db.py` | Own all SQL. Every other module accesses SQLite only through `db.*` functions. | Contain business/allocation logic. |
| `excel_import.py` | Parse the uploaded file into `Job` objects. Pure data transformation. | Touch the database. Make allocation decisions. |
| `allocation_engine.py` | Pure deterministic logic. Takes in-memory profiles + jobs, returns mutated jobs. | Call the database directly for anything except via the `build_*_profiles(conn, db)` helper functions, which take `db` as an explicit module reference (dependency passed in, not imported globally) — this was a deliberate choice to keep the engine testable with mock data. Never call Claude or Google Maps. |
| `maps_client.py` | One job: HTTP call to Google Routes API, parse response, raise `MapsClientError` on failure. | Know anything about jobs, drivers, or the allocation engine. |
| `ai_review.py` | Build a JSON context payload, call Claude, parse the JSON response into suggestions. | Know how to render UI. Never mutate a `Job` directly — only returns suggestion dicts for the UI to display and the planner to act on. |
| `digest_generator.py` | Read `decision_log` since last refresh, call Claude to merge into `preference_digest`, save. | Get called automatically — this is planner-triggered only (a button in Settings), by design, to control cost. |
| `export.py` | Load the *original* uploaded workbook, write two columns, save as new file. | Regenerate formatting from scratch. Touch any other column. |
| `app/ui/*.py` | PySide6 widgets. Own all user interaction, all `QMessageBox`/dialogs, hold the single shared `conn` and pass it to `db.*` calls directly. | Contain allocation logic (that belongs in `allocation_engine.py`) or SQL (that belongs in `db.py`). |

## 3. Class relationships

### Runtime dataclasses (allocation_engine.py) — NOT ORM models
These are plain `@dataclass` objects, built fresh at the start of each
`allocate()` run from the current database state, then mutated in memory
during allocation. They are discarded after use (nothing persists them
except `Job`, which the UI holds onto for the rest of the session until
Finalize/Export).

```
DriverProfile
├── id, name                                   # from drivers table
├── working_hours_per_day, shift_start          # structured hard rules
├── license_types: list[str]
├── off_days: list[str]
├── max_overtime_hours_per_month
├── total_hours_per_month_target
├── month_overtime_so_far: float                # computed from finalized_jobs
│                                                  BEFORE allocate() runs
├── occupied_seconds: float                      # RUNTIME, mutated during allocate()
└── busy_intervals: list[(datetime, datetime)]   # RUNTIME, mutated during allocate()

VehicleProfile
├── id, plate, vehicle_type
├── in_workshop: bool           # True if EITHER db.in_workshop OR
│                                  db.excluded_from_planning is set
│                                  (merged at build_vehicle_profiles time)
└── busy_intervals: list         # RUNTIME

SupplierOffering                 # one per (supplier, vehicle_type) row in
│                                   supplier_offerings table
├── supplier_id, supplier_name, vehicle_type
├── rate_per_hour, max_available_per_day
└── cumulative_hours_history      # computed from finalized_jobs BEFORE allocate()

SupplierHire                      # RUNTIME ONLY, created dynamically during
│                                    allocate(), never stored in the database
│                                    directly (only the resulting Job.assigned_
│                                    supplier_unit label + Job.assigned_supplier_id
│                                    get persisted, via finalized_jobs, on Finalize)
├── supplier_id, supplier_name, vehicle_type
├── instance_number: int          # 1-based
├── busy_intervals: list
├── already_used: bool            # flips to True after first job -> later
│                                    reuses get the "SAME " prefix
└── label (property)              # computed: supplier_name if instance_number==1
                                     else f"{supplier_name} {instance_number-1}"
```

### Job (excel_import.py) — the central object passed through the whole pipeline
```
Job
├── (parsed from Excel) row_number, sr, order_no, date, start_dt, end_dt,
│   pickup_location, contact_person, order_location, event_text, event_id,
│   vehicle_type_required, additional_info, charge_code, same_driver_key
└── (filled in by allocation_engine.allocate(), later read by UI/export)
    assigned_driver_id, assigned_driver_name, assigned_vehicle_id,
    assigned_vehicle_plate, assigned_supplier_unit, assigned_supplier_id,
    assignment_note, unresolved
```
`same_driver_key` (added for the "Same Driver" column feature) is read
straight from the planner-pasted "Same Driver" column text -- blank for
every row unless the planner explicitly flags it. It is NOT derived from
`event_id`; a row can belong to an event chain without being flagged for
same-driver handling, and vice versa (though in practice the planner
pastes the Event text into it).

`assigned_driver_name` was added alongside `same_driver_key`. It exists
because `assignment_note` (e.g. `"In-house: NAME [Same Driver group]"`) is
a human-readable explanation string, not something to be parsed back out --
`export.py` had been fragile-parsing `assignment_note` for the driver name
before this change; now it uses `assigned_driver_name` directly. Do not
add more suffixes to `assignment_note` and expect `export.py` to strip
them out -- always write to `assigned_driver_name` for anything that must
end up unmodified in the exported file.
A single `Job` instance flows: `excel_import.load_jobs_from_excel()` →
mutated by `allocation_engine.allocate()` → read by `ai_review.py` (to
build context) and `plan_day_tab.py` (to render the results table) →
read again by `export.py` (Finalize) and `db.save_finalized_jobs()`.
**There is no separate "planned job" database table that mirrors `Job`
during a session** — `Job` objects live only in `PlanDayTab.self.jobs`
(Python memory) until the planner clicks Finalize, at which point a
flattened dict representation is written to `finalized_jobs`.

### UI class tree
```
MainWindow (QMainWindow)
├── QTabWidget (self.tabs)
│   ├── PlanDayTab
│   ├── DriversTab
│   ├── SuppliersTab
│   ├── VehiclesTab
│   │   └── EditVehicleDialog (QDialog, opened on "Edit Selected")
│   ├── LocationsTab
│   └── SettingsTab  (disabled via setTabEnabled while PIN-locked)
└── corner widget: QPushButton "🔒 Unlock Settings" (only visible when PIN set)
```
Every tab widget receives the single shared `conn` (sqlite3 Connection)
in its constructor and calls `db.*` functions directly — there is no
intermediate controller/presenter class.

## 4. Function flow — the daily workflow, end to end

```
1. User clicks "Upload Excel File..." (PlanDayTab._on_upload)
   -> excel_import.load_jobs_from_excel(path) -> list[Job]
   -> stored as self.jobs, self.uploaded_path

2. User optionally types free text into the "day notes" box
   (not processed until step 4)

3. User clicks "Run Planning" (PlanDayTab._on_run)
   -> allocation_engine.build_driver_profiles(conn, db)   [reads drivers,
      excludes any with excluded_from_planning, computes
      month_overtime_so_far via db.get_driver_month_overtime_hours]
   -> allocation_engine.build_vehicle_profiles(conn, db)  [reads vehicles,
      merges in_workshop + excluded_from_planning]
   -> allocation_engine.build_supplier_offerings(conn, db) [reads
      supplier_offerings joined with suppliers, computes
      cumulative_hours_history via db.get_supplier_cumulative_hours]
   -> allocation_engine.allocate(jobs, drivers, vehicles, offerings)
      [MUTATES jobs in place -- see Section 5 for the algorithm]
   -> PlanDayTab._render_results() populates the QTableWidget,
      populates the driver/supplier filter dropdown

4. User optionally clicks "AI Review (event chains + day notes)"
   (PlanDayTab._on_ai_review)
   -> excel_import.group_jobs_by_event(jobs) -> {event_id: [jobs]},
      filtered to only events with >=2 stages
   -> for each consecutive stage pair within a multi-stage event:
      db.resolve_location() [Locations tab lookup, exact vs approximate]
      -> maps_client.get_travel_time(maps_key, origin, destination,
         departure_dt=prev_job.end_dt)  [traffic-aware, time-of-day matters]
   -> db.get_digest(conn) -> small preferences_digest string
   -> ai_review.build_review_context(jobs, event_groups,
      driver_hours_summary, travel_lookups, day_notes, preferences_digest)
   -> ai_review.review_plan(anthropic_key, context) -> list[suggestion dict]
   -> PlanDayTab renders each suggestion as a QFrame with Accept/Reject
      buttons (PlanDayTab._add_suggestion_widget)

5. User clicks Accept or Reject on a suggestion
   -> db.log_decision(conn, plan_date, affected_jobs, suggestion_type,
      reasoning, action)   [written to decision_log immediately, forever]
   -- NOTE: as of the last known state, Accept does NOT automatically
      mutate the corresponding Job's assignment in the table. The
      planner still manually edits the table if they act on a
      suggestion. This is an intentional interim state, not a bug --
      see NEXT_SESSION.md.

6. User clicks "Finalize Day (save to history)" (PlanDayTab._on_finalize)
   -> flattens self.jobs into job_rows dicts (skips unresolved jobs)
   -> db.save_finalized_jobs(conn, plan_date, job_rows)
      [DELETEs any existing rows for that plan_date first, then inserts --
       re-finalizing a date overwrites rather than duplicates]

7. User clicks "Export Filled Excel" (PlanDayTab._on_export)
   -> export.export_filled_excel(self.uploaded_path, self.jobs, output_path)
      [loads the ORIGINAL workbook via openpyxl, finds VEHICLE/DRIVER
       columns by header text, writes only those two cells per matched
       row_number, saves as a new file -- original is never modified]
```

## 5. The `allocate()` algorithm (allocation_engine.py), step by step

```
allocate(jobs, drivers, vehicles, supplier_offerings,
         allowed_driver_ids=None, allowed_supplier_ids=None,
         allow_override_days=None, travel_buffer_minutes=30):

  sort jobs by start_dt (unparsed jobs -> flagged unresolved immediately)

  hires_by_key = {}   # (supplier_id, vehicle_type) -> list[SupplierHire],
                       # persists across the whole run = "today's hires so far"

  group_drivers = {}           # same_driver_key -> [DriverProfile,...] used so far
  group_vehicle_by_driver = {} # (same_driver_key, driver_id) -> last VehicleProfile used
  group_supplier_hires = {}    # same_driver_key -> [SupplierHire,...] used so far

  for each job in time order:
    group_key = job.same_driver_key or None   # None = normal, unflagged job

    # ---- IN-HOUSE PASS ----
    candidates = [d for d in driver_pool if:
        d qualifies for job.vehicle_type_required (exact license_types match)
        AND NOT d is off that weekday (off_days, unless overridden)
        AND NOT job starts before d.shift_start (parsed from free text like
            "07:00 AM" or "18:00"; unparseable/blank = no restriction)
        AND NOT d has a time-overlapping (with 30-min buffer) existing job
            -- UNLESS the overlapping job belongs to this SAME group_key,
               in which case overlap is allowed (see "Same Driver" note below)
        AND (if d.working_hours_per_day is set) the day's cumulative
             overtime-so-far (across every job already given to this
             driver today) plus this job's contribution does not exceed
             MAX_OVERTIME_HOURS_PER_DAY (a hard, fixed daily ceiling --
             currently 2.0h -- checked FIRST, regardless of how much
             monthly overtime budget remains)
             AND adding this job would not push d.month_overtime_so_far +
             today's projected overtime over d.max_overtime_hours_per_month
             -- OR, if no monthly overtime cap is configured at all for
             this driver, working_hours_per_day becomes a hard DAILY
             ceiling instead (0 overtime allowed) rather than silently
             going unenforced
    ]
    if candidates:
      # Fewest-drivers preference for flagged groups: prefer a driver
      # already used for this same group_key, if one of them still
      # qualifies for this row. Only fall back to the normal
      # least-occupied-hours pick across ALL candidates when there's no
      # group yet, or none of the group's current driver(s) qualify here.
      group_candidates = [d for d in candidates if group_key and d in group_drivers[group_key]]
      chosen_driver = min(group_candidates or candidates, key=occupied_seconds)

      matching_vehicles = [free (same group_key overlap exception applies),
                            type-matching, non-workshop vehicles]
      if matching_vehicles:
        # prefer the same vehicle this driver already used for this group
        chosen_vehicle = group_vehicle_by_driver.get((group_key, chosen_driver.id))
                          if that's still in matching_vehicles, else matching_vehicles[0]
        assign chosen_driver + chosen_vehicle
        update chosen_driver.occupied_seconds, both busy_intervals (tagged with group_key)
        record chosen_driver/chosen_vehicle into the group_* registries above
        continue to next job
      # else: qualified driver exists but no free in-house vehicle ->
      #        fall through to supplier pass (do NOT give up yet)

    # ---- SUPPLIER PASS ----
    matching_offerings = [offerings where vehicle_type matches]
    if no matching_offerings: mark unresolved, continue

    # Priority 1a (flagged groups only): reuse a hire already used for
    #             THIS group_key, if one is free now (same overlap
    #             exception as drivers above)
    # Priority 1b: reuse any already-hired unit (any matching supplier)
    #             that is free at this time
    if a reusable hire exists:
      assign it; label = "SAME <hire.label>" if hire.already_used
                          else hire.label
      mark hire.already_used = True
      record into group_supplier_hires[group_key] if flagged
      continue

    # Priority 2: hire a NEW unit, from the offering with the lowest
    #             cumulative_hours_history among those with remaining
    #             daily capacity (max_available_per_day)
    if no offering has remaining capacity: mark unresolved (at daily cap)
    else:
      instance_number = (count of existing hires for this supplier+type) + 1
      create new SupplierHire, register in hires_by_key (and group_supplier_hires if flagged)
      label = hire.label   # bare name if instance_number==1, else "NAME {n-1}"
      assign; continue

  return jobs  # mutated in place, also returned for convenience
```

**Key invariant:** in-house is *always* attempted before any supplier
logic runs, for every single job independently — there's no "reserve
some jobs for suppliers" pre-planning; it's a strict greedy pass in time
order.

**"Same Driver" groups (`same_driver_key`, from the planner-pasted "Same
Driver" Excel column):** confirmed behaviour, agreed with the project
owner before implementing (do not change without re-confirming):
1. Two rows sharing the same non-blank `same_driver_key`, assigned to the
   same driver (or same reused supplier unit), are allowed to have
   overlapping times without being treated as a double-booking conflict.
   This models cases like a truck parked on-site for a long window while
   the same driver also has a nested/overlapping row for the same event.
   This relaxation is scoped ONLY to pairs of rows sharing the identical
   group key — overlap against any job outside that specific group (a
   different group, or no group) is still a hard conflict as normal.
2. There is no hard-coded "split by time" or "split by vehicle type"
   rule. The engine always tries to reuse the driver(s)/supplier unit(s)
   already used for that group first (same "fewest units" idea as the
   existing supplier reuse-before-hire logic) and only introduces an
   additional driver/unit when none of the group's current one(s) still
   qualify for the next row (wrong vehicle-type license, hours
   exhausted, off day, etc). In real test data this naturally reproduces
   both a vehicle-type split and an hours-driven split, whichever the
   actual constraint is, without guessing which one applies.
3. Known, documented simplification: `occupied_seconds` and the monthly
   overtime projection still SUM every row's duration even when two rows
   in the same group overlap in time, rather than computing a true
   elapsed-time union. This can overstate a driver's real occupied hours
   for overlapping same-group rows but never understates them — an
   intentional, safety-conservative choice (Rule 6: driver hour limits
   are a hard constraint), not an oversight. Revisit only as a deliberate
   follow-up if it starts producing noticeably wrong fairness ranking.

## 6. Data flow diagram (text form)

```
[Daily request .xlsx]                [fleetplanner.db]
        |                                     |
        v                                     v
excel_import.load_jobs_from_excel   db.list_drivers / list_vehicles /
        |                            list_all_supplier_offerings
        v                                     |
   list[Job]  <----------------- allocation_engine.build_*_profiles
        |                                     |
        +---------------> allocation_engine.allocate() <---+
        |                        |                          |
        |                        v                          |
        |                 mutated list[Job]                 |
        |                        |                          |
        |          +-------------+-------------+            |
        |          v                           v            |
        |   PlanDayTab results table    (optional) AI Review |
        |                                       |             |
        |                              db.resolve_location    |
        |                              maps_client.get_travel_time
        |                              db.get_digest           |
        |                              ai_review.review_plan --+
        |                                       |
        |                              list[suggestion] -> UI cards
        |                                       |
        |                              db.log_decision (on Accept/Reject)
        v
  export.export_filled_excel  --> new .xlsx (original + filled columns)
        |
  db.save_finalized_jobs (on Finalize) --> finalized_jobs table
        |
        +--> feeds back into future runs via:
             db.get_driver_month_overtime_hours (monthly overtime cap)
             db.get_supplier_cumulative_hours (cross-day supplier fairness)
             digest_generator.refresh_digest (planner-triggered, reads
               decision_log, NOT finalized_jobs)
```

## 7. Dependency graph (imports, simplified)

```
main.py
  -> db
  -> ui.main_window

ui.main_window
  -> db
  -> ui.drivers_tab, ui.suppliers_tab, ui.vehicles_tab,
     ui.plan_day_tab, ui.settings_tab, ui.locations_tab

ui.plan_day_tab
  -> db
  -> excel_import
  -> allocation_engine
  -> maps_client
  -> ai_review
  -> export
  -> ui.settings_tab (imports ANTHROPIC_KEY_SETTING, GOOGLE_MAPS_KEY_SETTING
     constants only -- not a circular UI dependency, just shared constants)

ui.drivers_tab, ui.suppliers_tab, ui.vehicles_tab, ui.locations_tab
  -> db  (only)

ui.settings_tab
  -> db
  -> maps_client
  -> digest_generator
  -> ai_review (imports AIReviewError only, for the Test-key button)
  -> anthropic (direct SDK import, for the "Test" button's minimal call)

allocation_engine
  -> (no app-internal imports except receiving `db` as a passed-in
     parameter to build_*_profiles -- does NOT `import app.db` at module
     level, to keep it testable standalone)

excel_import
  -> openpyxl
  -> (no other app-internal imports)

ai_review
  -> anthropic
  -> json

maps_client
  -> requests

digest_generator
  -> anthropic

export
  -> openpyxl

db
  -> sqlite3, os, json, datetime
  -> rules_parser (for parse_rule_line, used only by the free-text
     AI-notes CRUD functions: add_driver_rule, add_supplier_rule, etc.)

rules_parser
  -> re, datetime  (no app-internal imports)
```

**External dependencies** (`requirements.txt`):
- `PySide6==6.11.1` — GUI framework (Qt for Python)
- `openpyxl==3.1.5` — Excel read/write, used by `excel_import.py` and
  `export.py`
- `anthropic==0.120.0` — Claude API SDK, used by `ai_review.py`,
  `digest_generator.py`, `settings_tab.py` (test-key button)
- `requests` (unpinned) — used by `maps_client.py` for direct HTTP calls
  to the Google Routes API (no Google SDK used, deliberately, to avoid
  the extra dependency weight)

No web framework, no ORM, no task queue, no external message broker —
this is intentionally a simple, single-process desktop application.

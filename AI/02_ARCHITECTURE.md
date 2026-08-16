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
│       ├── plan_day_tab.py        # Daily workflow screen, result table (all Excel columns, hide/unhide,
│       │                          #   driver/vehicle inline reassignment), driver/supplier + event filters,
│       │                          #   ReCheck clash/rule-break scan, summary popup (largest UI file)
│       ├── drivers_tab.py         # Structured driver hard rules + AI notes
│       ├── suppliers_tab.py       # Structured supplier offerings + AI notes
│       ├── vehicles_tab.py        # Vehicle roster + Active/Deactive toggle (Phase 28)
│       ├── vehicle_maintenance_dialog.py  # Vehicle Maintenance Log window (Phase 28, new)
│       ├── map_tab.py             # Locations tab: 3-panel map + travel times (Phase 32)
│       ├── locations_tab.py       # LEGACY, superseded by map_tab.py (Phase 32)
│       ├── schedules_tab.py       # View/edit finalized_jobs to match what actually happened (Phase 31)
│       ├── settings_tab.py        # API keys, PIN, digest refresh control
│       ├── entity_rules_widget.py # LEGACY/unused generic widget (see below)
│       ├── trip_clipart.png       # Summary popup trip-card icon
│       ├── vehicle_maintenance_icon.png   # Maintenance Log window title icon (Phase 28)
│       ├── maintenance_log_button.png     # Vehicles-tab per-row wrench button icon (Phase 28)
│       ├── plate_background.png           # Blank plate graphic behind the Plate field (Phase 28)
│       ├── vehicle_expiry_icon.jpg        # "Vehicle Expiry" summary-card icon (Phase 28)
│       ├── tyre_icon.png, battery_icon.png, chiller_icon.png, oilfilter_icon.png
│       │                                  # the other four summary-card icons (Phase 28)
```

All image assets are plain files sitting beside the `.py` files that use
them, loaded via `Path(__file__).with_name(...)` / `Path(__file__).parent`
-- the same pattern `trip_clipart.png` already established (see Section
3.1). Not embedded in code, not a Qt `.qrc` resource bundle -- both were
considered and rejected as unnecessary for this project's scale (see
`CHANGELOG_AI.md` Phase 28 for the reasoning). If this app is ever
packaged into a `.exe` (still deferred), the packaging step will need to
explicitly bundle these image files alongside the code.

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
├── working_hours_per_day, max_working_hours_per_day  # structured hard rules
│                                                  (normal day / hard daily ceiling --
│                                                  see HR-002 rework, 2026-08-03)
├── shift_period: str | None                    # 'morning' | 'evening' | None
│                                                  (replaces the old shift_start
│                                                  exact-time field -- see Section 6)
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

## 3.1 Result Summary Popup

`PlanDayTab` owns a read-only `DriverSupplierSummaryDialog` opened by the
`Summary` button beside `Export Filled Excel`. The dialog is deliberately a
reporting layer over `PlanDayTab.self.jobs` (plus, as of 2026-08-14 Phase
25, `PlanDayTab.self.last_drivers`) only:

- It never queries SQLite or master-data tables directly -- `DriverSupplierSummaryDialog.__init__(jobs, drivers, parent)`
  is handed `self.last_drivers` (the `DriverProfile` list already built in memory by the
  last Run Planning click), the same object `_on_ai_review` already reuses for its own
  hours summary. No new `db.*` call was added anywhere in this dialog.
- It counts total trips, unique in-house drivers present in the results, in-house-assigned
  trips, unique suppliers present in the results, supplier-assigned trips, and unresolved jobs.
- The popup uses a modern card-style header for the four primary totals; the second
  card is specifically `In-house trips`, not total trips.
- The main report is a proper table with rows for assigned in-house drivers and,
  when enabled, supplier records. Columns cover first-job start, last-job end,
  duty span, **overtime (new, Phase 25)**, trip count, and merged worked hours. This
  keeps detailed workload information aligned and easy to scan instead of rendering
  each record as a card.
- **Overtime column (new 2026-08-14, Phase 25):** `max(0, shift_span_hours -
  driver.working_hours_per_day)` -- the same span-based method
  `db.get_driver_month_overtime_hours()` uses per day (Phase 23), just for this one
  day instead of summed across a month. Shows `"--"` for a driver whose
  `working_hours_per_day` isn't known here (not present in `last_drivers`, e.g. Run
  Planning wasn't run this session) and for every supplier row -- overtime is a
  driver-specific hard-rule concept (`drivers.working_hours_per_day`), not applicable
  to suppliers. The cell's text turns red (`#c0392b`) when overtime is greater than
  zero, matching the same color the Drivers tab uses for an over-budget monthly
  balance (Phase 24).
- The table always shows both populations when they exist. There is no filter
  checkbox in the popup: explicit `IN-HOUSE DRIVERS` and `SUPPLIERS` group
  headers keep the ordering unambiguous, with in-house drivers first and suppliers
  second.
- The four metric cards use visual icons: in-house drivers and suppliers use the
  same line-art icon family as the modern popup design, while in-house trips and
  supplier trips use the supplied trip clipart bundled as `app/ui/trip_clipart.png`.
  The image is loaded relative to `plan_day_tab.py`, so the popup does not depend
  on an external path or database record.
- The footer intentionally avoids repeating the four header-card totals. It shows
  only `Total trips` and `Unresolved trips`, plus the Close action.
- For each assigned in-house driver it calculates first-job -> last-job duty span,
  trip count, and worked hours. Worked hours use merged time intervals so
  overlapping Same-Driver rows are not double-counted, matching the engine's
  occupied-hour accounting.
- Supplier detail is grouped from the assignment results themselves; numbered
  supplier hires and `SAME ...` labels are treated as the same supplier company.
  Supplier rows always appear after the in-house-driver group.
- Opening the popup does not modify jobs, assignments, the database, or the
  uploaded workbook.
- **Window chrome (changed 2026-08-14, Phase 25):** the dialog is now frameless
  (`Qt.FramelessWindowHint`) -- the native OS title bar was removed since the
  dialog already has its own in-content "×" close button, and removing it frees
  vertical space. Default/minimum size reduced from 980×900/900×820 to
  980×720/900×620, since the previous height could clip the bottom rows (including
  the Close button) under the taskbar on smaller screens; the table scrolls
  internally, so a shorter dialog only reduces how many rows are visible before
  scrolling, not what data is available. Being frameless also means the dialog can
  no longer be dragged by a title bar -- not addressed, since it wasn't requested.

This keeps the summary a true snapshot of the plan currently visible in memory,
including any future planner edits made before export/finalization.

## 3.2 Background-threaded Run Planning

New 2026-08-14 (Phase 26). Before this, `PlanDayTab._on_run` called
`allocate_by_solver()` directly on the GUI thread -- since a solve can take
up to `time_limit_seconds` (15s default), Qt's event loop couldn't paint,
animate, or respond to anything for that whole window, so the app visibly
froze (and Windows itself could flag it as "Not Responding"). This is
**the first threaded code anywhere in this app** -- a deliberate, real
architectural addition, not a cosmetic tweak, called out explicitly rather
than folded silently into a UI-polish change.

```
_SolverWorker(QObject)                      # app/ui/plan_day_tab.py
├── finished = Signal(dict)                 # solver_status on success
├── missing_dependency = Signal(str)        # ortools ImportError detail
├── failed = Signal(str)                    # any other unexpected exception
└── run()                                   # calls allocate_by_solver(),
                                               executed on the QThread, never
                                               on the GUI thread
```

`PlanDayTab._on_run`:
1. Runs the three `build_*_profiles(conn, db)` calls synchronously (cheap
   DB reads, not worth backgrounding).
2. Gives immediate feedback (within ~0ms of the click, before the thread
   even starts): disables `run_btn` and `upload_btn`, changes `run_btn`'s
   text to "Running...", shows an indeterminate `QProgressBar`
   (`setRange(0, 0)` -- a marquee/busy bar, not a fake determinate one,
   since CP-SAT doesn't expose a real linear progress percentage to
   report), and sets `summary_label` to a "Planning your day..." message.
3. Constructs a `_SolverWorker`, moves it to a new `QThread`, connects
   `thread.started -> worker.run`, connects all three of the worker's
   signals back to dedicated slots (`_on_run_finished`,
   `_on_run_missing_dependency`, `_on_run_failed`), and starts the thread.
   `self._run_thread` / `self._run_worker` are held as instance attributes
   (not locals) -- a required PySide6 pattern, since an unreferenced
   `QThread` can be garbage-collected mid-run and crash the app.
4. `self._pending_drivers` holds the `DriverProfile` list built in step 1
   until the worker reports back (needed for `self.last_drivers` once the
   run completes -- `last_drivers` also feeds the Result Summary popup's
   Overtime column, Section 3.1, and the AI Review hours summary).

On completion (`finished`/`missing_dependency`/`failed`, whichever fires),
each signal is separately connected to `thread.quit` and `worker.deleteLater`
so the thread and worker clean themselves up; `_reset_run_controls()`
(shared by all three outcomes) restores the button/progress-bar state.
`_on_run_finished` then does exactly what `_on_run` used to do
synchronously after the old direct call: render results, enable the other
buttons, show the OPTIMAL/FEASIBLE status text (Phase 22).

**`self.jobs` is mutated in place by the worker's background thread.**
This is safe here specifically because nothing on the GUI thread reads
`self.jobs` between starting the thread and receiving its completion
signal (every button that would read it -- AI Review, Finalize, Export,
Summary -- stays disabled for the whole run). Do not add any code that
reads `self.jobs` while a run is in flight without re-checking this
invariant.

**Known, disclosed limitation (Rule 16/20), not addressed this phase:**
no cancellation support, and no explicit handling if the tab/window is
closed while a run is in flight. Out of scope for what was asked (a busy
indicator so Run Planning doesn't feel frozen), not a silent gap.

## 3.2b Background-threaded AI Review + retry-on-transient-failure

New 2026-08-15 (Phase 30c), same reasoning as 3.2 above: `_on_ai_review`
used to call `ai_review.review_plan()`/`review_plan_gemini()` directly on
the GUI thread, freezing the window for the whole network round trip --
made worse once `ai_review.py` gained its own retry loop (Phase 30c, same
session), since a single overloaded/rate-limited response could now mean
up to 3 attempts, several seconds apart, all on the GUI thread.

```
_AIReviewWorker(QObject)                    # app/ui/plan_day_tab.py
├── finished = Signal(list)                 # suggestion dicts on success
├── failed = Signal(str)                    # AIReviewError or any other exception
└── run()                                   # calls review_plan() or
                                               review_plan_gemini(), executed
                                               on the QThread, never the GUI thread
```

`PlanDayTab._on_ai_review`:
1. Runs the key check, event-chain grouping, and the travel-time lookup
   loop synchronously on the GUI thread -- **deliberately not
   backgrounded**, unlike step 1 of Run Planning above: the lookup loop
   calls `db.resolve_location(self.conn, ...)`, and a `sqlite3` connection
   can only safely be used from the thread that created it. Moving this
   loop to the worker thread would risk a real cross-thread sqlite crash
   for no real benefit, since the AI API call itself (now potentially 3
   attempts) is the actual source of the reported freeze, not this loop.
2. Builds the review `context` (pure computation, no I/O).
3. Same immediate-feedback pattern as Run Planning: disables
   `ai_review_btn` (relabeled "Reviewing..."), also disables `run_btn`/
   `upload_btn` (prevents a concurrent Run Planning or new Upload from
   mutating `self.jobs` while the review reads it), shows the same
   `self.run_progress` indeterminate bar Run Planning uses (not a second
   bar -- the two operations can never overlap, since starting one
   disables the other's own trigger button).
4. Constructs an `_AIReviewWorker(use_gemini, api_key, context)`, moves it
   to a new `QThread`, connects `finished`/`failed` to
   `_on_ai_review_finished`/`_on_ai_review_failed`, starts the thread.
   `self._ai_review_thread`/`self._ai_review_worker` held as instance
   attributes for the same GC-safety reason as `_run_thread`/`_run_worker`.
5. `self._pending_ai_review_state` (a small dict: `anthropic_key`,
   `maps_warning`) holds what the finished/failed handlers need, the same
   stash-across-the-async-call pattern as `self._pending_drivers`.

On completion, `_reset_ai_review_controls()` (mirrors `_reset_run_controls()`)
restores the button/progress-bar state; `_on_ai_review_finished` then does
what `_on_ai_review` used to do synchronously after the old direct call:
set the provider badge + warning-triangle tooltip on the AI Suggestions
header, clear/rebuild the suggestion cards.

**Retry logic lives in `ai_review.py`, not the worker** --
`_call_with_retry()`/`_is_transient_error()`, shared by both
`review_plan()` and `review_plan_gemini()`, wraps just the actual API
call. Up to 3 total attempts, 3 seconds apart, only when the failure text
looks transient (`"503"`, `"overloaded"`, `"unavailable"`, `"rate
limit"`/`"429"`, `"timeout"`, `"temporarily"`, `"connection"`); a
non-transient failure (bad key, invalid request) raises immediately on
the first attempt. This keeps the retry behavior available to any caller
of `ai_review.py`, not just the threaded UI path.

## 3.2c Map / travel-time screen and its caches (Phase 32)

New 2026-08-16. The Locations tab became `map_tab.py` -- a three-panel
screen (locations editor | MapLibre/OpenFreeMap map | trips + driver-chain lists),
modeled on a voyage-planning UI the project owner supplied as reference.

**Cost is the architecture here.** Routing/geocoding APIs bill per call,
and this screen wants a travel time for every trip on an 81-trip day,
re-run whenever the planner adjusts something. Two SQLite caches make
that affordable:

```
db.geocode_cache        query_text (PK) -> lat/lon
                        The long tail of raw Excel area names
                        ("ON SITE - PALM JUMEIRAH"). Addresses don't
                        move, so these are cached permanently.

db.travel_time_cache    (origin, destination, hour_bucket) PK
                        -> duration, distance, encoded polyline
                        The hour bucket is what preserves traffic-
                        awareness (08:00 and 23:00 are genuinely
                        different durations) while still collapsing
                        every trip sharing a route in that time band
                        into ONE call.
```

Predefined location codes keep their coordinates on `locations` itself
(`latitude`/`longitude`/`geocoded_at`) rather than only in
`geocode_cache` -- deliberately, because those are planner-visible and
manually correctable, and a correction must survive a cache clear.

**Two providers, always labeled.** `maps_client.py` gained a second
backend: Google (traffic-aware, paid, preferred) and OpenRouteService
(free, no credit card, **not** traffic-aware). `MapTab._active_provider()`
picks Google when a key exists, else ORS -- the same
primary-vs-free-fallback shape as `ai_review.py`'s Anthropic/Gemini
split. Since the two produce meaningfully different numbers, the UI
always shows which one produced the current figures.

**Nothing runs automatically.** Opening the tab costs nothing; lookups
happen only on an explicit "Run Locations" click (the project owner's
requirement -- running on every tab open would be pure waste).

**Threading**: `_LookupWorker` on a `QThread`, same pattern as
`_SolverWorker`/`_AIReviewWorker`. It receives plain pre-resolved data
and returns results -- it never touches `self.conn`, because a sqlite3
connection can't be used from another thread. All cache reads happen on
the main thread before the worker starts; all cache writes happen on the
main thread in the `finished` slot.

**AI Review shares the same cache** (`plan_day_tab._on_ai_review` reads
`db.get_cached_travel_time` before fetching, and writes what it does
fetch), so the two features never pay twice for the same route.
`_build_driver_chain_gaps()` additionally feeds per-driver connection
feasibility into the AI context -- built from *already-cached* routes
only, so it adds zero API cost and simply gets richer as the map screen
fills the cache.

**Still display-only (Rule 10).** None of this touches
`allocation_engine.py`. Real drive time does NOT feed the deterministic
solver -- `DEFAULT_TRAVEL_BUFFER_MINUTES` remains `0` and the standing
code comment about wiring live travel times into gap checks remains an
open future item, explicitly deferred with the project owner.

## 3.3 Vehicle Maintenance Log

New 2026-08-14 (Phase 28, revised same day in Phase 28b after the
project owner reviewed the actual running window against their own
mockup). `VehiclesTab` (`vehicles_tab.py`) gained a wrench-icon button
per row (`COL_MAINTENANCE`) that opens `VehicleMaintenanceDialog`
(`vehicle_maintenance_dialog.py`, new file) for that specific vehicle.
Design came directly from the project owner's own reference images
(`MISC/1.png`-`4.jpg` at design time, since deleted per the project
owner's own cleanup) -- this section is the structural record of what
was built from them, including a real design correction made after
seeing the first version rendered.

**Vehicles tab changes:**
- Replaced the old separate "In Workshop" / "Don't Use Tomorrow" columns
  and their two toggle buttons with a single Active/Deactive checkbox
  column (`COL_ACTIVE`), using the exact same checkbox-in-table-item
  pattern (`Qt.ItemIsUserCheckable`, `itemChanged` signal) Drivers/
  Suppliers already use in their `QListWidget`s -- ported to
  `QTableWidget` here since Vehicles has always been table-based, not
  list+form-based, and the project owner's own mockup kept it that way.
  Checked = active/included; unchecked = excluded, row turns orange
  (`EXCLUDED_COLOR`) and sorts to the bottom, identical visual convention
  to Drivers/Suppliers.
- `db.set_vehicle_workshop_status()` was deleted outright (not just
  deprecated) -- nothing calls it anymore, matching the precedent set
  when `_parse_shift_start_time`/`_job_is_before_shift_start` were fully
  removed from `allocation_engine.py` once `shift_period` replaced
  `shift_start` (the DB *column* stays either way, since this project's
  migration system can't drop columns -- only the now-dead *code* was
  removed).
- `allocation_engine.build_vehicle_profiles()` no longer reads
  `in_workshop` -- `excluded_from_planning` alone now decides
  `VehicleProfile.in_workshop` (the dataclass field name itself was kept
  unchanged, to avoid touching every candidate-filter call site across
  `allocation_engine.py` that already reads it; only what feeds it
  changed).
- **`EditVehicleDialog` (Phase 28b) is now the ONLY place any vehicle
  field is edited.** Expanded from its original 3 fields (Plate, Type,
  Capacity/Notes) to cover every column on `vehicles`, including the
  detail fields that originally lived in `VehicleMaintenanceDialog`
  (model, year, chassis, engine, registration + expiry, tyre size,
  battery type, both certificates + their expiries, picture with a file
  picker). Two-column `QFormLayout`/`QGridLayout` combo, ~640x560.
  `basic_values()` feeds the existing `db.update_vehicle()` (unchanged
  signature), `maintenance_field_values()` feeds
  `db.update_vehicle_maintenance_fields()` -- `VehiclesTab._on_edit`
  calls both in sequence. Date fields are validated (YYYY-MM-DD or
  blank) before the dialog will accept.

**`VehicleMaintenanceDialog` (Phase 28b): a read-only report, not a
form.** The project owner's direct feedback after seeing Phase 28's
first version rendered: "the vehicle maintenance log will be just read
only fields like a report. if we want to change anything we go in
vehicle tab and click edit selected." Every vehicle-detail widget that
was previously an editable `QLineEdit` (model, year, chassis, engine,
registration, certificates, tyre size, battery type, details) is now a
plain read-only `QLabel` (`objectName="fieldValue"`), laid out in a
single flat `QGridLayout` with explicit `(row, col)` coordinates per
field pair -- **not** two independent side-by-side `QFormLayout`s, which
is what Phase 28's first version used and which visually overlapped
(each `QFormLayout` negotiates its own row heights independently, and
nesting two of them side-by-side in one grid cell doesn't force them to
agree, so the taller-content column's rows drifted out of alignment
with the shorter one and overlapped it -- root-caused directly from a
rendered screenshot, not guessed). The flat-grid approach has no such
negotiation to go wrong. Same section-level fix philosophy as the QThread
worker-reference issue (Phase 26) and the QScrollArea theming issue
(this same phase, see below): a rendered screenshot plus the concrete
before/after comparison is what confirmed each fix, not just re-reading
the code.
- Header: title icon, "VEHICLE MAINTENANCE LOG", an Active/Deactive
  checkbox -- kept here too (not moved exclusively into `EditVehicleDialog`)
  per the project owner's explicit choice ("if it stays functional in
  maint log as well then both... if keeping it makes things worse then
  remove it") -- it's an operational status toggle, not a "detail" field
  like model/chassis, and stayed functional/consistent with the Vehicles
  tab's own checkbox (same `excluded_from_planning` column either way),
  so it stayed in both places.
- Vehicle info + detail fields: all read-only now (see above).
- Five summary cards (Vehicle Expiry, Battery/Tyre/Oil/Chiller Change),
  unchanged from Phase 28 -- built by `_refresh_cards()`, see
  `DATABASE.md`'s `service_records` entry for exactly how "last service
  of this type" is derived (a single `list_service_records` call, then a
  plain Python dict keyed by `service_type`, no per-card query).
- **Service history: rebuilt as an Access-style "Continuous Forms" grid
  (Phase 28b), replacing Phase 28's separate add/edit-form-above-a-
  read-only-table design entirely.** The project owner's own term and
  reference behavior: every row in the `QTableWidget` is a live,
  directly-editable record (native cell editing for the text/number
  columns, a real `QComboBox` via `setCellWidget` for the Service Type
  column so it always shows as a dropdown, not just when a row is
  selected) -- no separate form, no per-row "Save" button. A row
  auto-saves as soon as it's edited: `itemChanged` (plain cells) or a
  combo box's `currentIndexChanged` triggers `_save_service_row(row)`,
  which INSERTs via `db.add_service_record()` if that row has no
  `service_records.id` yet (tracked in column 0's `Qt.UserRole`, set
  once the INSERT returns an id) or UPDATEs otherwise -- verified by
  test that editing an already-saved row updates in place rather than
  creating a duplicate. `"+ Add a Record"` appends one blank editable
  row at the bottom and scrolls to it; `self._suppress_save` guards
  population/insertion so rows being constructed don't fire a premature
  save. Date-format validation here is deliberately lenient (a cell with
  an unparseable date simply isn't auto-saved yet, no interrupting
  popup) -- appropriate for a live continuous grid, unlike
  `EditVehicleDialog`'s modal validate-then-accept pattern. The table
  always opens scrolled to the bottom (most recent rows) via
  `scrollToBottom()`, matching the project owner's own "always show the
  last rows that fit" note -- this reverts Phase 28's earlier
  "deliberate adaptation" away from an editable grid (that adaptation
  reasoned no editable-table-cell pattern existed elsewhere in this
  codebase; the project owner's Continuous Forms clarification
  explicitly asked for exactly that pattern here, so it's now the one
  place in this app that has it).
- **A separate, unrelated theming bug found and fixed in the same
  review pass:** Phase 28's first version wrapped the dialog's content in
  a `QScrollArea` for vertical overflow; the `QScrollArea`'s internal
  viewport is a distinct widget that does not inherit a `QDialog {
  background: ... }` stylesheet rule the way a plain child widget does,
  so the window rendered with the app's OS-default dark background (no
  app-level stylesheet exists anywhere else in this codebase) underneath
  text colors that assumed a white one -- dark-on-dark, effectively
  unreadable, confirmed from an actual rendered screenshot the project
  owner shared. Fixed by dropping the `QScrollArea` entirely (content
  sits directly on `self`, the same approach `DriverSupplierSummaryDialog`
  / Summary popup already uses successfully) and explicitly styling
  every remaining widget class used in the window (`QLineEdit` had
  previously never in fact needed a rule here since Phase 28b removed
  them from this dialog entirely, but `QComboBox`/`QPushButton`/
  `QCheckBox` all needed explicit rules they hadn't had before, since
  they'd been silently inheriting the OS theme rather than this dialog's
  intended white/card look).

- **Vehicle-info section now loaded from a `.ui` file at runtime (Phase
  28p), a deliberate one-file exception to this codebase's normal
  "hand-written Python widget construction" convention.** After several
  rounds of visual-fit changes (plate sizing/position, picture
  enlargement) that this session's headless `offscreen` Qt platform
  couldn't verify against what the project owner actually saw, they
  asked for a mouse-driven tool instead of continued code-only
  iteration. `_build_vehicle_info()` (Model/Year, Type, Chassis, Engine,
  RTA/Ad. Certificate, plate, picture) now loads
  `app/ui/vehicle_info_section.ui` via `QUiLoader` (raw XML parsed at
  every app launch, **no compile/`pyside6-uic` step, no
  convert-back-to-Python step** -- an explicit project-owner requirement)
  and pulls out its named children by `objectName` via `findChild()`
  into the same `self.<attr>` names the rest of the class already uses
  (`model_year_label`, `chassis_value`, `plate_display`,
  `picture_label`, etc.) -- `_load_vehicle()` needed no changes beyond
  one styling fix (below). The header, summary cards, and
  service-history table are deliberately **not** part of this -- they're
  populated/rebuilt dynamically at runtime
  (`_refresh_cards()`/`_reload_service_records()`), which a static `.ui`
  file isn't suited for, so they remain hand-written Python.
  `app/ui/vehicle_info_section.ui` itself was generated once via
  `QFormBuilder` (not hand-typed XML) from the exact prior Python
  widget tree, then hand-verified and fixed for three round-trip
  pitfalls a naive generate-and-load wouldn't have caught: (1)
  `QFormBuilder` serializes each widget's literal `visible` state at
  save time, so a never-`.show()`n generator script produces a file
  where everything loads hidden -- the generator now calls `.show()`
  before saving; (2) arbitrary dynamic properties (`setProperty()` with
  a custom key) are **not** serialized by `QFormBuilder`, so
  style-matching via a custom property + dialog-level attribute selector
  silently matches nothing after a reload -- field label/value colors
  and font sizes are baked directly into each widget's own `styleSheet`
  property in the generator instead; (3) `setColumnStretch()` is a
  method call, not a serialized property, so it must be (and is)
  re-applied in Python immediately after loading. The project owner can
  now open `vehicle_info_section.ui` in `designer.exe` (bundled with the
  project's installed PySide6, no separate download) and adjust sizing/
  position by mouse; saved changes take effect on the app's next launch.
  Object names in the `.ui` file must stay in sync with the
  `findChild(...)` calls in `_build_vehicle_info()` if renamed.
  **Note, disclosed rather than silently dropped:** an earlier
  Phase 28o mechanism that forced pixel-perfect horizontal centering for
  the plate (by matching the picture's grid column width to the left
  column's measured content width) was removed here -- tested against
  the project owner's actual-length sample data (real chassis/engine/
  certificate numbers, not placeholder text), the left column's natural
  width alone (870px) exceeded what a normal dialog width has room to
  mirror on both sides, so pixel-perfect centering was never reliably
  achievable for realistic data in the first place, and continuing to
  chase it in Python would work against the entire point of moving this
  to a visually-editable file. Equal grid-column stretch remains as a
  reasonable default; exact centering for their own real data is now
  the project owner's to adjust in Designer.

**PDF export (Phase 28hh).** A PDF-icon button (`pdf_icon.png`, flat
asset copied from `MISC/pdf.png`) sits at the right of the
service-history header row (the pre-existing "+ Add a Record"/"Delete
Selected Row" buttons moved left, next to the label, to make room for
it -- nothing else in the row/table/cards/vehicle-info layout changed).
It opens `_ExportPdfDialog`, a small `QDialog` in the same file: two
centered From/Till date fields (default to today, DD-MM-YYYY, same
convention as the service-table cells), and a "Create PDF" button that
prompts a standard Save-As file dialog, then calls
`_generate_maintenance_pdf()`. That function reads the vehicle +
service records straight from the database (never from
`self.service_table`, so the live on-screen table is provably
unaffected/unfiltered) and draws the report with `QPainter` directly
onto a `QPdfWriter` (A4 portrait, 150 DPI) -- `QPdfWriter` ships with
the project's existing PySide6 install, so no new dependency was added,
per the dependency-minimization rule. Drawing from data rather than
grabbing the live widgets keeps the exported page's proportions fixed
to A4 regardless of the exporting machine's screen/DPI. The service
table is filtered to records where `start_date >= From` and `end_date
<= Till` (confirmed with the project owner); the filtered table
auto-paginates (`writer.newPage()` + repeated headers) if it doesn't
fit one page. The 5 cards' data-selection logic (previously inline in
`_refresh_cards()`) was extracted into a shared module function,
`_cards_data_for(row, records)`, so the on-screen cards and the PDF's
cards can never drift apart -- both call the same function.

**What does NOT depend on any of this:** `allocation_engine.py` reads
none of the new vehicle fields or `service_records` at all -- this is
pure master-data/UI, same as every other Vehicles-tab field before it.

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
   -> (all three build_* calls above stay synchronous on the GUI thread --
      cheap DB reads, not worth backgrounding)
   -> **runs on a background QThread as of 2026-08-14, Phase 26** (see
      "Background-threaded Run Planning" below): a `_SolverWorker(QObject)`
      calls allocation_engine.allocate_by_solver(jobs, drivers, vehicles,
      offerings, solver_status_out=solver_status) on that thread
      [MUTATES jobs in place -- CP-SAT constraint solver, see Section 5c
      for the algorithm. CHANGED 2026-08-14, Phase 22 -- this call site
      used to be allocate() (Section 5's hand-written heuristic); see
      CHANGELOG_AI.md Phase 22 for why and how the switch was made.]
   -> the worker emits one of three signals back to the GUI thread:
      `finished(solver_status)` on success, `missing_dependency(detail)`
      if `ortools` isn't installed (shows the same QMessageBox as before,
      Phase 22), or `failed(detail)` for any other unexpected exception
      (never silently swallowed -- shows a QMessageBox rather than hanging
      forever in the "Running..." state)
   -> PlanDayTab._render_results() populates the QTableWidget,
      populates the driver/supplier filter dropdown, and the summary
      label shows the solver's OPTIMAL/FEASIBLE status in plain language
      alongside the job-count summary

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
         allow_override_days=None, travel_buffer_minutes=DEFAULT_TRAVEL_BUFFER_MINUTES  # 0, see below):

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
        AND job's time-of-day matches d.shift_period ('morning' = before
            12:00, 'evening' = 12:00 onward, None = no restriction -- see
            HR-002 rework, Section 6; replaces the old exact-time
            shift_start check)
        AND NOT d has a time-overlapping (with the travel buffer, 0 by
            default as of 2026-08-03 -- see TB-001) existing job
            -- UNLESS the overlapping job belongs to this SAME group_key,
               in which case overlap is allowed (see "Same Driver" note below)
        AND (if d.working_hours_per_day is set) the day's cumulative
             hours-so-far (across every job already given to this driver
             today) plus this job's contribution does not exceed
             d.max_working_hours_per_day (a hard, PER-DRIVER, planner-set
             daily ceiling -- falls back to d.working_hours_per_day, i.e.
             zero daily overtime, if left blank; see HR-002 rework,
             Section 6 -- replaces the old fixed
             MAX_OVERTIME_HOURS_PER_DAY=2.0 module constant)
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
      # qualifies for this row.
      group_candidates = [d for d in candidates if group_key and d in group_drivers[group_key]]
      if group_candidates:
        chosen_driver = min(group_candidates, key=occupied_seconds)
      elif group_key:
        # FRESH group (2026-08-06, SD-005): project each candidate's
        # occupied hours PLUS the group's TOTAL merged hours (precomputed
        # once per group before this loop), not just this opening row's
        # duration -- otherwise a driver idle for one instant could end up
        # carrying an entire large event alone, with no way to reconsider
        # once picked. See CHANGELOG_AI.md Phase 14.
        chosen_driver = min(candidates, key=lambda d: occupied_seconds(d) + group_total_hours[group_key])
      else:
        # UNGROUPED job, no existing group to defer to (2026-08-03,
        # NEW-007/specialist-reservation): prefer the more broadly-
        # licensed ("generalist") candidate over a narrowly-licensed
        # ("specialist") one, reserving specialist hours for exclusive-
        # type demand; occupied_seconds is only the tiebreak.
        chosen_driver = min(candidates, key=lambda d: (-len(d.license_types), occupied_seconds(d)))

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

  # ---- POST-PASS: gap-filling (OPT-002/003, added 2026-08-03) ----
  # Runs after every job above has been through the in-house/supplier
  # pass, and BEFORE the minimum-hours repair pass below (filling a gap
  # adds hours to that driver, which can itself resolve an under-minimum
  # day without any reassignment needed). Confirmed real-world trigger:
  # a driver ends up with jobs at 13:00-15:00 and 22:00-01:00 (a 12h span
  # for 5h of work) while a 16:00-20:00 job that fits the gap is left
  # unresolved -- can't be fixed inside the main loop above since jobs
  # are processed in strict start-time order, so the LATER of a gap's two
  # bounding jobs isn't assigned yet when an earlier job that would sit in
  # that gap is being considered. For every still-fully-unresolved job
  # (no driver AND no supplier), check every driver for a genuine bounded
  # gap (an existing job before AND after, with the travel buffer) that
  # the job fits into, respecting every other hard rule, and assign it
  # there instead of leaving it unresolved. Does NOT reclaim a job
  # already given to a supplier.
  fill_gaps_with_unresolved_jobs(jobs, driver_pool, vehicle_pool, ...)

  # ---- POST-PASS: daily-minimum-hours repair (HR-005, added 2026-08-03,
  #      WIDENED 2026-08-06 -- see CHANGELOG_AI.md Phase 14) ----
  # Runs after every job above has been through the in-house/supplier pass.
  # A driver used at all on a given day must reach at least
  # working_hours_per_day that day -- this can't be a per-job filter like
  # the daily ceiling above, since the engine doesn't know a driver's
  # full-day total until the day's last job has been considered. For every
  # (day, driver) left with a non-zero, under-minimum total: try to move
  # ALL of that driver's jobs that day to another qualifying driver with
  # spare room (same license/off-day/shift/overlap/ceiling/monthly-cap
  # rules as above, PLUS SD-004 vehicle-type consistency if the job is
  # grouped -- see _established_group_vehicle_type()). If every job can
  # move, commit the move. If even one can't, release the WHOLE day back
  # to unresolved with a clear note -- never leave a partial illegal day
  # in place. Repeats up to 5 passes (fixing one driver can free room that
  # fixes another).
  # 2026-08-06: previously this whole pass SKIPPED any day containing a
  # "Same Driver" grouped job -- correct in principle (a group's own hours
  # are sometimes genuinely the shortfall), but on a real day where 84% of
  # rows were grouped, it meant the pass almost never ran at all. Now a
  # grouped day can be moved WHOLE onto one other driver (never split).
  # Two things had to be added to make that safe: (1) a `settled_job_ids`
  # guard, shared across all 5 passes -- once a job is moved, it's never
  # moved again this run, which stops groups from thrashing/ping-ponging
  # between drivers pass after pass (a real bug caught while building
  # this); (2) the search for a new driver now checks candidates in
  # least-occupied-first order, not driver_pool's natural (alphabetical)
  # order -- otherwise a driver who hadn't had their own fix processed YET
  # in the same pass could look "busy" and get skipped in favor of one
  # freed moments earlier by an unrelated move, purely due to processing
  # order (also caught and fixed the same session).
  repeat up to 5 times: repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, ..., settled_job_ids)

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
3. `occupied_seconds` and the monthly overtime projection use the TRUE
   UNION of a driver's time intervals (`_merged_hours()`), not a naive
   sum -- two rows in the same group that overlap in time (e.g. two
   simultaneous pickups on one truck) count once, not once each. This was
   originally a documented simplification (sum, deliberately erring
   toward overstating hours per Rule 6) but real PLANNED.xlsx data
   confirmed 2026-08-03 that the overstatement was large enough to
   falsely trip the daily ceiling in routine cases (one real driver:
   ~17h "occupied" vs. ~11h true) -- fixed the same day. See
   CHANGELOG_AI.md Phase 12.

## 5b. Experimental alternative strategies (2026-08-06, allocation_engine.py)

**`allocate_by_merit()` and `allocate_by_anchor()` are NOT wired into the
UI.** (`allocate_by_solver()`, described separately in Section 5c, WAS
wired in as of 2026-08-14, Phase 22 -- see that section.) `plan_day_tab.py`
calls `allocate_by_solver()`, not `allocate()`, as of Phase 22; `allocate()`
itself remains fully intact and unused by the UI. `allocate_by_merit()`
and `allocate_by_anchor()` exist alongside both, built to be compared
against real data before any further switch (Rule 13) -- see
CHANGELOG_AI.md Phase 15 for the real-data results and AI_CONTEXT.md
Section 9 item 14 for the full story.

### `PlanningUnit` and `build_planning_units()`

Both new strategies operate on `PlanningUnit` (jobs: list of 1 or 2 Job
objects, start_dt, end_dt, vehicle_type_required, same_driver_key,
event_id) instead of raw `Job` objects. `build_planning_units(jobs)`
pre-merges Same-Driver row PAIRS (never 3+) that share a vehicle type and
start/end within 1h into one internal unit -- e.g. two rows both
06:00-10:00 on a 10-Ton truck become one unit spanning that time. This
turns the genuinely-simultaneous case into a single decision instead of
the runtime overlap-relaxation machinery `allocate()` uses
(`ignore_group_key`, SD-004 consistency checks). Every result is copied
onto all of a unit's original Job objects at commit time, so the export
always shows each original row separately, all pointing to the same
driver/vehicle.

### `allocate_by_merit(jobs, drivers, vehicles, supplier_offerings, ...)`

```
units = build_planning_units(valid_jobs)
for shift in ("morning", "evening"):
    shift_units = units where _shift_of(start) == shift
    shift_drivers = drivers where _driver_matches_shift_pool(driver, shift)
    leftover += _allocate_shift(shift_units, shift_drivers, ...)
      # within _allocate_shift, per unit in time order:
      #   1. continuity: an established group driver who still qualifies
      #      always takes it first (mirrors SD-002/SD-003)
      #   2. license scarcity ALWAYS overrides seeding -- the only
      #      qualified driver takes it immediately if free
      #   3. seeding: prefer a driver not yet seeded this shift AND an
      #      event not yet claimed this shift, over a second job from an
      #      event someone else already started
      #   4. (second loop) FILL: everything left goes to whoever it fits,
      #      least-occupied-first, specialist-reservation as tiebreak
supplier fallback for leftover units (same reuse-before-hire logic as allocate())
rearrangement loop: _fill_gaps_with_unresolved_jobs + _repair_minimum_daily_hours
                     + _rebalance_idle_drivers, looped to 6 passes, sharing
                     one settled_job_ids set
```

Real-data result: 16 unresolved vs. baseline's 12 -- the aggressive
event-diverse seeding spends driver availability faster than the
rearrangement stage recovers it. Disclosed as underperforming, not
hidden.

### `allocate_by_anchor(jobs, drivers, vehicles, supplier_offerings, ..., swap_rounds=3)`

```
units = build_planning_units(valid_jobs)
for shift in ("morning", "evening"):
    leftover += _anchor_and_fill_shift(shift_units, shift_drivers, ...)
      # per driver, MOST-CONSTRAINED (narrowest license) first:
      #   1. FIRST anchor: earliest-starting qualifying unit
      #   2. compute ceiling = _driver_ceiling(driver) -- max_working_hours_per_day
      #      if set, else working_hours_per_day itself (a fixed day, not
      #      a range, when max is blank -- confirmed against real
      #      PLANNED.xlsx: the only 9h-exact drivers are the ones with no
      #      max configured)
      #   3. target_end = first_job.end_dt + ceiling
      #   4. LAST anchor: whichever remaining qualifying unit ends
      #      closest to (but not after) target_end
      #   5. (second loop) MIDDLE FILL: everything else, least-occupied-first
mark remaining leftover units unresolved
outer loop (up to 10x): {
  rearrangement loop (up to 6x): gap-fill, HR-005 repair, idle-rescue
  _swap_repair(units, ..., max_rounds=swap_rounds)
} until neither stage changes anything in a full round
  # (2026-08-09, Phase 16) bounded local search over "bundles" -- a
  # bundle being either a single ungrouped unit, or a WHOLE Same-Driver
  # group (never split, same principle HR-005 uses). For each unresolved
  # unit U:
  #   1. direct-fit: does some qualifying driver already have genuine
  #      free room? If so, place U there directly, no displacement.
  #   2. single-hop: does displacing ONE bundle from a qualifying driver
  #      free enough room for U, AND does that bundle then find a legal
  #      home elsewhere (driver + vehicle)?
  #   3. multi-hop chain (_try_place_bundle_chain, bounded by
  #      SWAP_REPAIR_CHAIN_DEPTH): if (2) fails, can the bundle's new
  #      home be freed by displacing ONE of ITS OWN bundles too,
  #      recursively, each driver visited at most once (an augmenting-
  #      path search, the same idea used in bipartite-matching
  #      algorithms) -- catches cases where every driver is individually
  #      blocked, but only because each is blocking someone else.
  # Every committed move strictly reduces the unresolved count by one --
  # never a net-zero shuffle. Looping this against the rearrangement
  # stage (rather than running swap-repair once, as in the original
  # version of this function) matters because a LATER rearrangement pass
  # can release a job that an EARLIER swap-repair call never got to see.
supplier fallback (same logic as allocate_by_merit)
```

Real-data result (2026-08-09, after the widening above plus the shift
first-job-only correction -- see CHANGELOG_AI.md Phase 16): **0
unresolved, 0 supplier, all 11 active drivers used**, on the real
`UNPLANNED.xlsx` -- up from 14 unresolved at the start of that session.

### `_rebalance_idle_drivers(jobs, driver_pool, vehicle_pool, ..., settled_job_ids=None)`

New rearrangement-stage pass, wired into `allocate()`,
`allocate_by_merit()`, and `allocate_by_anchor()` alike. Makes "every
driver has real work" a first-class goal, not just "no driver is
illegally under-minimum" -- a driver sitting at a legal-but-unrealistic
0h (often a side effect of `_repair_minimum_daily_hours` freeing a short
day to fix someone else's minimum) is otherwise never revisited. For
each 0h driver: accumulate candidates TENTATIVELY (first from anything
still unresolved, then from genuine surplus on other drivers -- hours
above THEIR OWN minimum, never dropping a donor below it) -- nothing is
committed to real state until the full accumulated total clears the
idle driver's own minimum; if it can't be reached, everything tentative
is simply discarded (free, since nothing was ever mutated) and the
driver stays legally idle. Two real bugs were found and fixed building
this (see CHANGELOG_AI.md Phase 15): an oscillation where a genuinely-
unfixable released job got rescued right back onto the same driver
(fixed by the all-or-nothing commit rule above), and a donor
remaining-hours miscalculation that silently undid a valid consolidation
(fixed by using the donor's TRUE full workload, including already-settled
jobs, as the baseline for "would this leave them short" -- separate from
the narrower list of jobs actually eligible to be pulled).

## 5c. The solver strategy — `allocate_by_solver()` (2026-08-09, allocation_engine.py)

A fourth allocation strategy, alongside `allocate()`, `allocate_by_merit()`,
and `allocate_by_anchor()` -- not a replacement for any of them. Uses
Google OR-Tools' CP-SAT constraint solver instead of a hand-written
heuristic: every hard rule is a constraint, the real goal is a weighted
objective, and the solver searches the space directly rather than
following a sequence of heuristic passes. See `AI_CONTEXT.md`'s "The
solver strategy" subsection (Section 6) for the full design reasoning;
this section is the structural *what/where*.

```
allocate_by_solver(jobs, drivers, vehicles, supplier_offerings,
                    allowed_driver_ids=None, allowed_supplier_ids=None,
                    allow_override_days=None, travel_buffer_minutes=..., 
                    time_limit_seconds=15.0):

  from ortools.sat.python import cp_model   # LAZY import -- raises a
                                              # clear ImportError if
                                              # ortools isn't installed;
                                              # nothing else in the app
                                              # depends on it

  units = build_planning_units(valid_jobs)   # same pre-merge as merit/anchor

  # --- warm-start hint ---
  scratch_drivers/vehicles = dataclasses.replace(..., fresh runtime state)
  scratch_jobs = copy.deepcopy(valid_jobs)
  allocate_by_anchor(scratch_jobs, scratch_drivers, scratch_vehicles, ...)
    # fast heuristic run on throwaway copies, purely to seed the solver

  model = cp_model.CpModel()
  x[unit, driver.id]        = BoolVar   # per license-compatible pair only
  veh[unit, vehicle.id]     = BoolVar   # per type-compatible pair only
  unresolved[unit]          = BoolVar

  for hv, hval in <hints derived from the scratch anchor run>:
      model.add_hint(hv, hval)          # NOT model.AddHint -- see note below

  # hard constraints:
  sum(x[unit,*]) + unresolved[unit] == 1              # per unit
  sum(veh[unit,*]) == sum(x[unit,*])                  # per unit needing a vehicle
  x[i,d] + x[j,d] <= 1   for any time-overlapping (i,j) sharing driver d
  veh[i,v] + veh[j,v] <= 1  for any time-overlapping (i,j) sharing vehicle v
  has_morning[d] / has_evening[d] linking vars          # shift = first-job-only gate
  total_minutes[d] <= _solver_effective_ceiling_minutes(d)     # ceiling, always
  total_minutes[d] >= floor_minutes(d)  .OnlyEnforceIf(floor_applies[d])
    # floor_applies[d] = used[d] AND NOT has_grouped_unit[d] -- the
    # Same-Driver-group exemption HR-005 already has, replicated here

  # soft preference (objective terms, not constraints):
  touches_group[group, driver]   # minimize distinct drivers per group --
                                   # NOT pairwise together[i,j,driver] (tried
                                   # first, caused a 60x+ slowdown, see
                                   # CHANGELOG_AI.md Phase 16)
  gap[driver] = ceiling - total   # minimize unused capacity, used drivers only
  used[driver]                   # small bonus for spreading across more drivers

  model.Minimize(
      1_000_000 * sum(unresolved)
      + 10_000 * sum(touches_group terms)
      + sum(gap terms)
      - sum(used terms)
  )

  solver = cp_model.CpSolver()
  solver.parameters.max_time_in_seconds = time_limit_seconds
  status = solver.Solve(model)   # OPTIMAL = proven best; FEASIBLE = best found
                                   # so far, time limit hit; anything else =
                                   # no solution at all (rare -- "everyone
                                   # unresolved" is always feasible)

  for each unit: commit via _commit_unit() based on solver.Value(...)

  supplier fallback for units still unresolved
    # IDENTICAL reuse-before-hire logic to allocate()'s supplier pass,
    # copied not reimplemented (Rule 1) -- runs strictly AFTER the solver
    # has exhausted every in-house combination it could find, since
    # unresolved costs 1,000,000x anything else in the objective
```

**Real-data result (2026-08-09): 44/44 resolved, 0 supplier, all 11
drivers used, status `OPTIMAL`, 3.78 seconds** -- see
`CHANGELOG_AI.md` Phase 16 for the sequence of fixes that got here
(a deprecated OR-Tools API footgun, the pairwise-vs-per-group encoding
lesson, and the floor-exemption gap).

**Two disclosed scope boundaries relative to the other three strategies**
(Rule 16 -- not silent gaps): (1) genuine time-overlap relaxation for
Same-Driver group members beyond what `build_planning_units()` already
pre-merges into pairs isn't modeled -- ordinary overlap rules apply to
anything not pre-merged, even within a flagged group; (2) supplier
hiring isn't modeled inside the solver at all, handled entirely by the
reused fallback pass described above.

**Wired into `plan_day_tab.py` as of 2026-08-14 (Phase 22).** The three
open sub-questions previously listed here (strategy choice in the UI vs.
a straight replacement; whether `ortools` becomes a hard requirement; how
to surface an `OPTIMAL` vs. `FEASIBLE` result to the planner) were
resolved explicitly with the project owner: straight replacement of
`allocate()` (no strategy picker), `ortools` pinned as a hard dependency
in `requirements.txt`, and the solver status shown plainly in the UI
summary label after each Run Planning click. See `CHANGELOG_AI.md`
Phase 22 for the full implementation writeup, and `AI_CONTEXT.md`'s "The
solver strategy" subsection for the design detail.

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
     ui.plan_day_tab, ui.settings_tab, ui.map_tab, ui.schedules_tab

ui.plan_day_tab
  -> db
  -> excel_import
  -> allocation_engine
  -> maps_client
  -> ai_review
  -> export
  -> ui.settings_tab (imports ANTHROPIC_KEY_SETTING, GOOGLE_MAPS_KEY_SETTING
     constants only -- not a circular UI dependency, just shared constants)

ui.drivers_tab, ui.suppliers_tab, ui.vehicles_tab, ui.schedules_tab
  -> db  (only)

ui.map_tab                          # Phase 32
  -> db
  -> maps_client
  -> ui.settings_tab (key constants only)
  -> ui.plan_day_tab (READ-ONLY reference, passed in by main_window, to read
     the currently-loaded .jobs; never mutates it)

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
- `ortools==9.15.6755` (pinned 2026-08-14, Phase 22 — was unpinned/
  optional before this) — Google's constraint-solver toolkit, used by
  `allocate_by_solver()` in `allocation_engine.py`, which `plan_day_tab.py`'s
  Run Planning button now calls directly as the UI's default engine. Still
  imported LAZILY inside that one function, not at module level, so a
  missing install raises a clear, catchable `ImportError` (caught by
  `plan_day_tab.py`, shown as a `QMessageBox`) instead of crashing the
  whole app at startup — but it IS now a genuine runtime requirement for
  normal use of the app, not an optional extra. Worth factoring into the
  PyInstaller packaging conversation whenever that happens (`ortools`
  adds meaningfully to bundle size).

No web framework, no ORM, no task queue, no external message broker —
this is intentionally a simple, single-process desktop application.

# DATABASE.md — Fleet Planner

**Database type:** SQLite 3, single file (`fleetplanner.db`), local to the
planner's PC. No server, no network access to the database itself. All
access goes through `app/db.py` — no other module executes raw SQL.

Foreign keys are enabled per-connection (`PRAGMA foreign_keys = ON` in
`db.get_connection()`). Row access uses `sqlite3.Row` (dict-like, access
by column name), set globally on the connection.

## Schema evolution model

The schema is created via one `_SCHEMA` script (`CREATE TABLE IF NOT
EXISTS` statements) run on every `init_db()` call, **plus** an additive
`_MIGRATIONS` list of `(table, column_definition)` pairs applied via
`ALTER TABLE ... ADD COLUMN`, silently skipping "duplicate column name"
errors. This means:
- New tables: safe to add via `CREATE TABLE IF NOT EXISTS` in `_SCHEMA`
  (or a migration's `executescript`, see `supplier_offerings` and
  `finalized_jobs` which were added this way after the initial schema).
- New columns on existing tables: safe to add via `_MIGRATIONS` — will
  not destroy existing data.
- Column *type changes*, *renames*, or *removals*: **NOT supported** by
  this migration system. These would require a genuine schema rebuild
  (delete-and-recreate), which historically required telling the user to
  delete their database file. Avoid needing this if at all possible.

## Tables

### `drivers`
The core roster of in-house drivers, plus their structured hard
scheduling/qualification rules.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE COLLATE NOCASE | Case-insensitive uniqueness (fixed after a real bug where "DEEPAK DEWAN" and "Deepak Dewan" were treated as different people) |
| `active` | INTEGER (0/1) | Soft-delete flag. **Note:** `db.delete_driver()` actually does a hard `DELETE`, not a soft-delete via this flag — `active` exists but is not currently used to hide records; every driver row present is real. Don't assume `active=0` rows exist to filter. |
| `created_at`, `updated_at` | TEXT (ISO datetime) | |
| `excluded_from_planning` | INTEGER (0/1), migration | "Don't use tomorrow" toggle. Checked by `allocation_engine.build_driver_profiles` — excluded drivers are skipped entirely (not even loaded into the planning pool). |
| `exclusion_reason` | TEXT, migration | Free text, e.g. "sick leave". Currently stored but not surfaced anywhere in the UI beyond the toggle itself. |
| `working_hours_per_day` | REAL, migration | Structured hard rule. Baseline/normal daily hours -- also doubles as the hard daily MINIMUM as of the HR-002 rework (2026-08-03): a driver used at all on a day must reach at least this many hours that day, enforced via a post-pass repair step (see `allocation_engine._repair_minimum_daily_hours`, AI_CONTEXT.md Section 6 "Overtime model"). |
| `shift_start` | TEXT, migration | **DEPRECATED as of 2026-08-03, replaced by `shift_period` below.** Kept in the schema only so old data isn't lost; no longer read by `allocation_engine.py`. Do not use for new work. |
| `shift_period` | TEXT, migration | **New 2026-08-03 (HR-002 rework).** `'morning'`, `'evening'`, or `NULL` (no restriction). Replaces the old exact-clock-time `shift_start` model: the planner no longer fixes an exact start time before planning, just a Morning/Evening label; the engine enforces it as a window (morning = before 12:00, evening = 12:00 onward -- see `SHIFT_PERIOD_EVENING_CUTOFF_HOUR` in `allocation_engine.py`). No automatic rotation or transition logic -- the planner can change this label whenever they want; the software does not compute or remember a rotation schedule (explicitly decided against, see spec SS-002). |
| `off_days` | TEXT, migration | Comma-separated lowercase weekday names, e.g. `"friday"` or `"friday,saturday"`. Hard rule — driver is skipped for jobs on these weekdays unless explicitly overridden per-date (`allow_override_days` param to `allocate()`, currently only usable programmatically — no UI for it yet). |
| `max_overtime_hours_per_month` | REAL, migration | `NULL`/blank = **no overtime allowed** (working_hours_per_day becomes a strict daily ceiling) -- fixed some time ago from an earlier "unlimited" default; see AI_CONTEXT.md Section 9 item 9. Positive number = monthly overtime budget, checked against `finalized_jobs` history. |
| `max_working_hours_per_day` | REAL, migration | **New 2026-08-03 (HR-002 rework).** The hard daily ceiling (including overtime), e.g. `12` paired with `working_hours_per_day=9`. Replaces the old hardcoded `MAX_OVERTIME_HOURS_PER_DAY = 2.0` module constant in `allocation_engine.py`, which had no UI field at all. `NULL`/blank falls back to `working_hours_per_day` (zero daily overtime allowed) -- fail-closed, same precedent as a blank `max_overtime_hours_per_month`, chosen deliberately over silently reopening the old unlimited-single-day bug. |
| `total_hours_per_month_target` | REAL, migration | Informational only, mainly for temp drivers. **Not enforced as a hard rule anywhere in `allocate()`/`allocate_by_*()`** — no allocation decision reads it. As of 2026-08-14 (Phase 24) it IS read for a display-only calculation: the Drivers tab's "Balance hours / month" field (`total_hours_per_month_target` minus `db.get_driver_month_span_hours()`). Still purely informational for scheduling purposes — nothing about this changes allocation behavior. |
| `license_types` | TEXT, migration | Comma-separated vehicle-type strings, EXACT text match required against `vehicles.vehicle_type` and `Job.vehicle_type_required`. This is the single most failure-prone field in the whole system — see AI_CONTEXT.md Section 6/9 re: exact-string matching and the "Seated" vs "Seater" real-world bug. |

### `driver_rules`
Free-text lines for AI context only. **NOT used for hard-rule
enforcement** — this table predates the structured-fields redesign and
is now explicitly scoped to soft/contextual notes ("prefers not to do
late-night Sharjah runs", etc.), shown in the Drivers tab under
"Additional notes for AI."

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `driver_id` | INTEGER FK -> drivers(id) ON DELETE CASCADE | |
| `line_text` | TEXT | Exactly as typed by the planner |
| `rule_type` | TEXT | Output of `rules_parser.parse_rule_line()` — one of the recognized types (`max_hours`, `qualified_vehicle_types`, `off_day`, `leave`, etc.) or `"custom"` if unrecognized. (The old `shift_start` free-text pattern was removed 2026-08-03 -- shift is now the structured `drivers.shift_period` column, see above.) **Important: even when this recognizes a pattern like `max_hours`, it is NOT fed into the allocation engine's hard-rule logic** — that logic reads only the structured `drivers.*` columns above. This table/parser pairing is a historical artifact now serving AI-context purposes only. |
| `parsed_json` | TEXT (JSON) | Structured value from the parser, e.g. `{"hours": 8.0}` |
| `sort_order` | INTEGER | Display order |

### `suppliers`
Outside transport companies hired as overflow.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE COLLATE NOCASE | |
| `active` | INTEGER (0/1) | Same caveat as `drivers.active` — not currently used for filtering. |
| `created_at`, `updated_at` | TEXT | |
| `excluded_from_planning` | INTEGER (0/1), migration | "Don't use tomorrow" — e.g. contract expired. `allocation_engine.build_supplier_offerings` skips excluded suppliers entirely. |
| `exclusion_reason` | TEXT, migration | |

### `supplier_rules`
Free-text AI-context lines, identical purpose/caveats to `driver_rules`,
scoped to suppliers.

### `supplier_offerings` (added via migration `executescript`, not
   in the original `_SCHEMA`)
The structured hard-rule replacement for the old "pre-named unit" model.
One row per vehicle type a supplier can provide.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `supplier_id` | INTEGER FK -> suppliers(id) ON DELETE CASCADE | |
| `vehicle_type` | TEXT | Exact-match text, same rules as `drivers.license_types` |
| `rate_per_hour` | REAL | Informational/business record; not currently used in allocation *decisions* (allocation picks by cumulative historical hours for fairness, not by rate — rate is stored for the planner's own reference/costing, not read by `allocate()`'s logic) |
| `max_available_per_day` | INTEGER | Hard cap — how many separate units of this type this supplier can provide in one day. `NULL` = unlimited. |

**Important:** individual supplier *units* (e.g. "AL LAITH PASSENGER
TRANSPORT 1") are **never stored as database rows**. They are generated
dynamically in memory during `allocate()` (`SupplierHire` dataclass) each
planning run, based on how many separate hires that specific day's
demand requires. Only the resulting label text
(`Job.assigned_supplier_unit`) and the `supplier_id` get persisted, and
only if the planner clicks Finalize (into `finalized_jobs.supplier_label`
/ `finalized_jobs.supplier_id`).

### `vehicles`
In-house fleet roster.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `plate` | TEXT UNIQUE COLLATE NOCASE | |
| `vehicle_type` | TEXT NOT NULL | Exact-match text — must match `Job.vehicle_type_required` and `drivers.license_types` character-for-character (after case/whitespace normalization) |
| `capacity_notes` | TEXT | Free text. Doubles as the Vehicle Maintenance Log window's "Details" field (Phase 28) — no separate column was added for that, since the two were the same kind of free-text note. |
| `in_workshop` | INTEGER (0/1) | **DEPRECATED 2026-08-14 (Phase 28)**, same precedent as `drivers.shift_start` — the Vehicles tab's old separate "In Workshop" toggle is gone from the UI, and `allocation_engine.build_vehicle_profiles` no longer reads this column. Kept only so old data isn't lost; do not use for new work. |
| `active` | INTEGER (0/1) | Same caveat as other `active` columns |
| `created_at`, `updated_at` | TEXT | |
| `excluded_from_planning` | INTEGER (0/1), migration | **The single Active/Deactive toggle as of Phase 28** — the Vehicles tab now has one checkbox (same visual pattern as Drivers/Suppliers), driving this column alone; it used to be paired with `in_workshop` (`build_vehicle_profiles` treated either as making a vehicle unavailable), now it's the only thing that does. Unchecked = excluded from planning, row turns orange and sorts to the bottom. |
| `exclusion_reason` | TEXT, migration | |
| `vehicle_picture` | BLOB, migration | Raw image bytes (PNG/JPG), Phase 28. Loaded via `QPixmap.loadFromData()` — no file path stored, no external file dependency. |
| `vehicle_model` | TEXT, migration | e.g. "TOYOTA HIACE". Phase 28, Vehicle Maintenance Log field. |
| `vehicle_year` | INTEGER, migration | Phase 28. |
| `vehicle_chassis` | TEXT, migration | VIN/chassis number. Phase 28. |
| `vehicle_engine` | TEXT, migration | Phase 28. |
| `vehicle_registration` | TEXT, migration | Phase 28. |
| `vehicle_reg_expiry` | TEXT, migration | ISO date (`YYYY-MM-DD`). Phase 28. Drives the Maintenance Log window's "Vehicle Expiry" summary card — shown in red if past today, same red-if-expired convention as the two certificate expiry fields below. |
| `tyre_size` | TEXT, migration | Phase 28. Shown inline on the "Tyre Change" summary card. |
| `battery_type` | TEXT, migration | Phase 28. Shown inline on the "Battery Change" summary card. |
| `rta_certificate` | TEXT, migration | Phase 28. |
| `rta_certificate_expiry` | TEXT, migration | ISO date. Phase 28. Red-if-expired in the Maintenance Log window. |
| `ad_certificate` | TEXT, migration | Phase 28. |
| `ad_certificate_expiry` | TEXT, migration | ISO date. Phase 28. Red-if-expired in the Maintenance Log window. |

**Important:** none of the Phase 28 fields above are read by `allocation_engine.py` — they're pure master-data/UI, no scheduling behavior depends on them.

### `service_records`
**New 2026-08-14 (Phase 28).** One row per service/repair/inspection
event for one vehicle — the Vehicle Maintenance Log window's service
history table, opened via the Vehicles tab's wrench-icon button.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `vehicle_id` | INTEGER FK -> vehicles(id) ON DELETE CASCADE | Linked by ID, not by plate text (the project owner's original design sketch drew the relationship on `Plate`, but `vehicle_id` is the stable key every other relationship in this schema already uses, and plate text could in principle be edited). Deleting a vehicle deletes its service history — confirmed as the intended behavior, matching every other CASCADE relationship in this schema (unlike `finalized_jobs`, which is intentionally NOT cascaded, since that's cross-day historical record, not master data belonging to a live vehicle). |
| `start_date`, `end_date` | TEXT (ISO date) | |
| `service_type` | TEXT | One of a fixed set the UI presents as a combo box: `Quotation`, `Oil/Filter Change`, `Chiller Unit Service`, `Accident`, `Battery Change`, `Repair`, `Mechanical Work`, `Body Work`, `Tyre Change`. Not enforced at the database level (plain TEXT, no CHECK constraint) — the combo box is the only guardrail, matching this project's general preference for UI-level guidance over rigid DB constraints on free-entry-adjacent fields. |
| `details` | TEXT | |
| `current_reading`, `next_reading` | REAL | Odometer/meter-style readings. `next_reading` from a vehicle's most recent `"Oil/Filter Change"` or `"Chiller Unit Service"` row feeds the "Next Reading" line on those two summary cards. |
| `qty` | REAL | |
| `person`, `workshop` | TEXT | Who performed it / where. |
| `created_at`, `updated_at` | TEXT | |

**How the five summary cards are computed:** the Maintenance Log window fetches every `service_records` row for that vehicle (`db.list_service_records`, ordered oldest-first), then — entirely in Python, no extra queries — keeps the last-seen row per `service_type` while iterating (since the list is already chronological, "last seen" is "most recent"). Four cards (Battery/Tyre/Oil/Chiller Change) come from this; the fifth ("Vehicle Expiry") comes directly from `vehicles.vehicle_reg_expiry`, not from any service record.

### `off_day_log` and `comp_days`
**Schema exists, but there is no CRUD/UI wired to either table.** These
were designed early in the project (before the structured hard-rule
redesign) to support tracking scheduled-vs-actual off days and
"comp days owed" when a planner overrides a driver's off day. The design
intent (from the original conversation): off days respected by default;
if the planner overrides one, the app should log an owed comp day and
alert the planner on future planning sessions until it's manually
applied. **None of this logic has been implemented** — treat these two
tables as reserved/future schema, not working features.

```sql
CREATE TABLE off_day_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    scheduled_off INTEGER NOT NULL DEFAULT 0,
    overridden INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    UNIQUE(driver_id, date)
);

CREATE TABLE comp_days (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    earned_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'owed',  -- 'owed' | 'applied'
    applied_date TEXT,
    note TEXT
);
```

### `app_settings`
Generic key-value store. Currently used for exactly three keys:
`anthropic_api_key`, `google_maps_api_key`, `settings_pin_hash` (SHA-256
hex digest via `settings_tab.hash_pin`; empty string means "no PIN set",
checked by `settings_tab.pin_is_set`). Accessed via
`db.get_setting(conn, key, default=None)` / `db.set_setting(conn, key,
value)`.

```sql
CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

### `decision_log`
Permanent, append-only record of every AI suggestion the planner
explicitly Accepted or Rejected. **Never sent to Claude directly** — see
`preference_digest` below. This table is the reason the "does the AI
learn over time" cost/latency concern was resolved: the raw log grows
forever locally (cheap), but only a small derived summary is ever
included in an API call.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `plan_date` | TEXT | The date the suggestion applied to (not when it was logged) |
| `affected_jobs` | TEXT | Comma-separated SR numbers |
| `suggestion_type` | TEXT | e.g. `"stay_on_site"`, `"cycle_back"`, `"day_note_override"`, `"flag_conflict"` |
| `reasoning` | TEXT | The AI's stated reasoning, copied verbatim from the suggestion |
| `action` | TEXT | `'accepted'` or `'rejected'` |
| `logged_at` | TEXT | Timestamp of the click, not `plan_date` |

### `preference_digest`
Single-row table (`id` constrained to `1` via `CHECK`). Holds the
current compact summary of the planner's demonstrated real-world
patterns, capped at roughly 400 words by the digest-generation prompt
(`digest_generator.MAX_DIGEST_WORDS`). This is the *only* history ever
included in a daily `ai_review.review_plan()` call.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK, CHECK(id=1) | Enforces single-row |
| `digest_text` | TEXT | The actual summary sent to Claude on future days |
| `last_refreshed_at` | TEXT | |
| `covered_through_date` | TEXT | Watermark — `digest_generator.refresh_digest` only pulls `decision_log` rows with `plan_date > covered_through_date`, so previously-digested decisions are never resent. Refreshing with nothing new is a no-op (no API call made — verified behavior, not just intent). |

### `locations`
Short-code → real-address lookup for accurate Maps queries.

| Column | Type | Business meaning |
|---|---|---|
| `short_code` | TEXT PK COLLATE NOCASE | Exactly as it appears in the Excel file's pickup/order location columns, e.g. `"CPK"`, `"BQT STORE"` |
| `full_address` | TEXT NOT NULL | A real address Google Maps can resolve precisely |
| `created_at`, `updated_at` | TEXT | |
| `latitude`, `longitude` | REAL, nullable | **New 2026-08-16 (Phase 32).** Map coordinates, so the code can be pinned. Held here rather than only in `geocode_cache` on purpose: these are planner-visible in the Locations panel and manually correctable (a geocoder puts `"CPK"` in roughly the right industrial area; the planner knows the exact gate), and that correction must survive a cache clear. `db.set_location_coords()` writes them. |
| `geocoded_at` | TEXT, nullable | **New 2026-08-16.** When the coordinates were last set. |

`db.resolve_location(conn, raw_text)` returns `{"address": str, "exact":
bool}` — `exact=True` only if a matching `short_code` row exists;
otherwise the raw text is used as-is with `exact=False`, and the AI
review layer is instructed to be more conservative with suggestions
based on `"approximate (area-level)"` confidence data.

### `geocode_cache` and `travel_time_cache` (new 2026-08-16, Phase 32)

**Both are PURE CACHES of paid API responses — no business data.** Safe
to delete wholesale at any time; the only cost is re-fetching. They exist
for one reason: Google/OpenRouteService bill per call, and the map screen
wants a travel time for every trip on an 81-trip day, re-run whenever the
planner adjusts something. Without caching that is hundreds of paid calls
a day; with it, recurring pairs (CPK → Zabeel runs constantly, day after
day) are fetched once and reused, which realistically keeps normal use
inside the free monthly allowance.

| Table | Key | Holds |
|---|---|---|
| `geocode_cache` | `query_text` (PK, COLLATE NOCASE) | `latitude`, `longitude`, `cached_at`. The long tail of raw Excel area names (`"ON SITE - PALM JUMEIRAH"`) that aren't predefined codes. Addresses don't move → cached permanently. Case/whitespace-insensitive so the same place spelled differently isn't re-charged. |
| `travel_time_cache` | `(origin, destination, hour_bucket)` (composite PK) | `duration_minutes`, `distance_km`, `polyline`, `cached_at`. |

**Why `hour_bucket`:** it preserves traffic-awareness. The same road at
08:00 and at 23:00 is a genuinely different duration, so those are cached
separately — while every trip sharing a route *within* that time band
still collapses to a single call. Direction matters too (`B→A` is a
separate entry from `A→B`; one-way systems are real).

`polyline` stores Google's *encoded* polyline string for the actual road
path (far smaller than an expanded point list), decoded on demand by
`maps_client.decode_polyline()` — which handles OpenRouteService's
geometry too, since ORS uses the same encoding.

Helpers: `db.get_geocode`/`save_geocode`,
`db.get_cached_travel_time`/`save_travel_time`,
`db.clear_travel_time_cache()` (manual refresh escape hatch — drops
travel entries only, **geocodes survive**, since addresses don't move),
and `db.travel_cache_stats()` (surfaced in the UI so the planner can see
the cache working). Both the map screen and AI Review read/write these
same tables, so the two features never pay twice for the same route.

### `finalized_jobs`
The permanent historical record of what was actually finalized each day.
This is what monthly overtime enforcement and cross-day supplier
fairness are computed from. Written only by `db.save_finalized_jobs()`
(called from the "Finalize Day" button), which **deletes any existing
rows for that `plan_date` first** — re-finalizing a date overwrites
rather than duplicates.

| Column | Type | Business meaning |
|---|---|---|
| `id` | INTEGER PK | |
| `plan_date` | TEXT | |
| `sr` | TEXT | Original SR# from the Excel row |
| `driver_id` | INTEGER, nullable | Set only for in-house assignments |
| `vehicle_id` | INTEGER, nullable | Set only for in-house assignments |
| `supplier_id` | INTEGER, nullable | Set only for supplier assignments |
| `supplier_label` | TEXT, nullable | e.g. `"AL LAITH PASSENGER TRANSPORT 1"` — the dynamically-generated label at time of finalization, since unit numbers are never separately stored |
| `start_dt`, `end_dt` | TEXT (ISO datetime) | |
| `hours` | REAL | Precomputed job duration in hours |
| `finalized_at` | TEXT | When the Finalize button was clicked (not `plan_date`) |
| `event_text`, `pickup_location`, `vehicle_type_required`, `order_no`, `contact_person`, `order_location`, `additional_info`, `charge_code`, `same_driver_key` | TEXT, nullable | **New 2026-08-16 (Phase 31/31b, Schedules tab).** Context snapshots captured once at Finalize time, straight off the already-populated `Job` object (no extra query) — so a finalized day's record stays self-contained and readable in the Schedules tab even years later ("for complete reference of past," the project owner's own framing for why the second batch of 6 was added same-day after the first batch missed them). Blank on any row finalized before these columns existed. |
| `driver_name`, `vehicle_plate` | TEXT, nullable | **New 2026-08-16.** Readable-name snapshots alongside `driver_id`/`vehicle_id` — deliberately NOT re-derived by joining the current `drivers`/`vehicles` tables, since (like the loose ID coupling below) a driver/vehicle can be renamed or deleted later and the historical record should still show what was true at the time. |
| `cancelled` | INTEGER NOT NULL DEFAULT 0 | **New 2026-08-16.** Soft "this planned trip didn't actually happen" flag, set from the Schedules tab — deliberately NOT a row delete, matching this project's existing exclude-don't-delete convention (`drivers`/`suppliers`/`vehicles`' `excluded_from_planning`). **Excluded from every hours/overtime/fairness read** (see the four queries below, each now filters `WHERE ... AND (cancelled IS NULL OR cancelled = 0)`). |

Unresolved jobs (no assignment found) are **not** written to
`finalized_jobs` at all — `PlanDayTab._on_finalize` explicitly skips
`j.unresolved` jobs when building the rows to save.

An index, `idx_finalized_jobs_plan_date`, exists on `plan_date` (added
2026-08-16 alongside the columns above) — keeps the Schedules tab's
date-range query fast regardless of how many years of history accumulate;
negligible cost to create/maintain at this table's realistic row volume.

**The Schedules tab** (`app/ui/schedules_tab.py`, new 2026-08-16) is the
only place `finalized_jobs` rows are ever corrected after the fact —
Finalize Day only ever writes once. See `AI/06_NEXT_SESSION.md` Section
7.4 and `AI/07_CHANGELOG_AI.md` Phase 31 for the full design writeup
(why a Cancelled checkbox instead of a hard delete, why corrections
overwrite in place with no separate audit-trail columns, why every edit
to an already-saved row requires a specific "Change X for SR N from A to
B?" confirmation first). Three new functions support it:
- `db.list_finalized_jobs(conn, date_from, date_to)` — every row (any
  driver/supplier, cancelled or not) in an inclusive `plan_date` range.
- `db.insert_finalized_job(conn, plan_date, **fields)` — one new row (the
  tab's "+ Add Row", for a genuinely unplanned trip that happened anyway).
- `db.update_finalized_job(conn, row_id, **fields)` — updates only the
  explicitly-passed columns of one existing row by `id`. Never a whole-day
  rewrite like `save_finalized_jobs` (which deletes/reinserts the entire
  day) — only the fields the planner actually changed are touched.

## Relationships (foreign keys)

```
drivers (1) ----< driver_rules (many)         ON DELETE CASCADE
suppliers (1) ----< supplier_rules (many)      ON DELETE CASCADE
suppliers (1) ----< supplier_offerings (many)  ON DELETE CASCADE
drivers (1) ----< off_day_log (many)           ON DELETE CASCADE  [unused]
drivers (1) ----< comp_days (many)             ON DELETE CASCADE  [unused]
vehicles (1) ----< service_records (many)      ON DELETE CASCADE  [Phase 28]

finalized_jobs.driver_id   -- NOT a declared FK (plain INTEGER column,
finalized_jobs.vehicle_id  -- no REFERENCES clause) -- intentionally
finalized_jobs.supplier_id -- loose, so historical records survive even
                               if a driver/vehicle/supplier is later
                               deleted from the roster. Query joins must
                               handle NULL/dangling IDs gracefully.
```

Deleting a driver/supplier cascades to their rule lines and offerings
(and off_day_log/comp_days, though those are unused) but **does not**
touch `finalized_jobs` — historical records are preserved even after the
entity is deleted, by design (the loose coupling above).

## Important queries worth knowing

**Month-to-date overtime for a driver** (the actual hard-cap check basis,
consumed by every one of the four allocation strategies' hard-rule
checks — see `allocation_engine.py`'s `DriverProfile.month_overtime_so_far`):
```sql
-- db.get_driver_month_overtime_hours(conn, driver_id, year, month, working_hours_per_day)
SELECT plan_date, MIN(start_dt) AS day_start, MAX(end_dt) AS day_end FROM finalized_jobs
WHERE driver_id = ? AND plan_date LIKE ?   -- e.g. '2026-03%'
  AND (cancelled IS NULL OR cancelled = 0)  -- added 2026-08-16, Phase 31
GROUP BY plan_date
-- then in Python: sum(max(0, span_hours - working_hours_per_day) for each day,
-- where span_hours = (day_end - day_start) in hours)
```
This groups by day *first*, then sums only the excess **duty SPAN** (not
summed job duration) over baseline per day — a driver who works exactly
their normal hours every day shows zero overtime even with a large
month-to-date total.

**CORRECTED 2026-08-14 (Phase 23).** This function originally summed each
day's `hours` column (`SUM(hours) AS day_total`, i.e. summed job
duration) instead of computing that day's true span. Phase 21
(2026-08-10) established that every daily/monthly overtime check in this
engine must use duty SPAN (earliest job start to latest job end that
day), not summed duration — but that fix only touched the live,
in-memory checks in `allocation_engine.py`; this function, which supplies
the *historical* `month_overtime_so_far` baseline those same checks read,
was missed. Since span ≥ summed duration always (equal only when a
driver's jobs that day are perfectly back-to-back with no gap), the old
version could only ever under-count historical overtime — found and
fixed while scoping the "Balance Overtime / month" Drivers-tab field,
which reuses this function directly.

**Shared helper (Phase 23):** both this function and
`get_driver_month_span_hours` below now go through a private
`db._driver_month_daily_span_hours(conn, driver_id, year, month)` helper
that returns a plain list of per-day span hours (the `SELECT ...
GROUP BY plan_date` query above, parsed into hours) — added so the
day-grouped span calculation itself only has to be written, and fixed,
once.

**Month-to-date total SPAN hours for a driver, new 2026-08-14 (Phase 24)**
— the basis for the Drivers tab's "Balance hours / month" field:
```sql
-- db.get_driver_month_span_hours(conn, driver_id, year, month)
-- same underlying query as get_driver_month_overtime_hours above, via
-- _driver_month_daily_span_hours, but summed directly with no baseline
-- subtracted -- a plain running total, not an excess-over-baseline figure.
```
Unlike `get_driver_month_to_date_hours` (which sums the `hours` column
directly, i.e. summed job duration across the month, ungrouped by day),
this sums each day's true SPAN first, matching the Phase 21 principle.
These two "total hours this month" figures can legitimately disagree on a
month with any gap days — this is expected, not a bug in either one; they
answer different questions (raw summed job duration vs. duty-span
total).

**Cross-day supplier fairness tiebreak:**
```sql
-- db.get_supplier_cumulative_hours(conn, supplier_id, since_date=None)
SELECT COALESCE(SUM(hours), 0) FROM finalized_jobs
WHERE supplier_id = ? AND (cancelled IS NULL OR cancelled = 0)  -- filter added 2026-08-16, Phase 31
[AND plan_date >= ?]
```
Used to prefer hiring from whichever supplier has received the least
cumulative business historically, among suppliers who could equally take
a new hire.

**Decisions not yet folded into the digest:**
```sql
-- db.get_decisions_since(conn, since_date_iso)
SELECT * FROM decision_log WHERE plan_date > ? ORDER BY plan_date
-- (or all rows, ordered, if since_date_iso is None)
```

## Constraints and assumptions worth flagging

- All name/plate/short_code uniqueness is `COLLATE NOCASE` — enforced at
  the SQLite level, not just in application code, after a real bug where
  application-level checking alone missed a case-variant duplicate.
- `plan_date` and other date columns are stored as **plain ISO text**
  (`YYYY-MM-DD`), not SQLite's `DATE` type (SQLite doesn't have a real
  date type) — all date arithmetic/comparison in queries relies on
  lexicographic string comparison, which only works correctly because
  ISO format sorts correctly as text. Do not introduce a different date
  format anywhere in this schema.
- There is no explicit `schema_version` table — migrations are tracked
  implicitly by attempting each `ALTER TABLE` and catching "duplicate
  column" errors. Two sessions running migrations concurrently against
  the same file is not a scenario this app needs to handle (single-user,
  single-process by design).
- A committed snapshot of a real `fleetplanner.db` exists in the linked
  GitHub repository (`razaazad-ctrl/Feet-Planner`), pushed by the user
  specifically to let an AI assistant test against real data. That
  snapshot revealed real data-quality issues (e.g., every driver had
  identical `license_types`, a missing driver `VENUGOPAL`, a
  vehicle-type text mismatch "23 Seater Bus" vs "23 Seated Bus") — these
  are data problems in that specific snapshot, not schema defects. See
  `CHANGELOG_AI.md` for the full findings.
- **FIXED 2026-08-06 (Phase 15):** an embedded newline character (from a
  wrapped Excel cell, copy-pasted into the database directly rather than
  through `excel_import._clean_text()`'s normalization) was found inside
  `"4.2 Ton Double cabin Open Truck\n(with lift)"` — not just in one
  driver's `license_types` as first suspected, but in **all 11 active
  drivers' `license_types`, one vehicle's `vehicle_type` (plate
  `Z 43915`), and two excluded/inactive drivers** — 15 records total,
  clearly propagated from the same source cell into every record. Fixed
  with a direct SQL sweep (`.replace('\n', ' ')` on each affected value);
  a follow-up scan across `drivers.license_types`,
  `vehicles.vehicle_type`, and `supplier_offerings.vehicle_type`
  confirmed zero embedded newlines remain anywhere in the database. This
  is the same failure class as the "Seated"/"Seater" mismatch above —
  invisible in most UI text fields, but a real character difference that
  silently defeats `_type_matches()`'s exact comparison. If a future
  session sees an unexplained zero-match on text that looks identical in
  the UI, check for a literal `\n` (or other whitespace variant) before
  assuming it's a code bug.

- **Another instance of the same exact-string-matching failure class,
  found and fixed 2026-08-09:** vehicle plate `A 68982` was entered as
  `vehicle_type = "14 Seater Van"` in the test database when the actual
  vehicle is a `"14 Seater Bus"` -- spotted by the project owner via a
  direct screenshot of the real source data, not discovered through the
  software. Fixed via `db.update_vehicle()` in the test database only;
  the project owner's own live database has the same issue and needs the
  same manual correction via the Vehicles tab, not something this
  session touched. No schema or code change -- purely a data-entry
  correction, same category as the "Seated"/"Seater" and embedded-
  newline issues above. See `CHANGELOG_AI.md` Phase 16.

- **The embedded-newline fix (above) recurred 2026-08-10** -- the cleaned
  database snapshot from Phase 15 (2026-08-06) was not the one that ended
  up committed to the repository; vehicle `Z 43915`'s `vehicle_type` had
  the embedded newline back in it, causing 3 real jobs to go unresolved
  (`_type_matches()` failing on the invisible-in-the-UI character
  difference, same failure mode as before). Re-applied the same SQL
  sweep fix directly to the repo's actual `fleetplanner.db`. If this
  recurs again, it's worth checking whether the database gets
  regenerated/re-exported from a source that still has the original
  wrapped-cell newline, rather than re-fixing it ad hoc each time.

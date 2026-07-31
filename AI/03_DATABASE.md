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
| `working_hours_per_day` | REAL, migration | Structured hard rule. Baseline daily hours — NOT a hard ceiling by itself (see `max_overtime_hours_per_month`). |
| `shift_start` | TEXT, migration | Structured hard rule, free text like `"07:00 AM"` or `"18:00"`, parsed at allocation time by `allocation_engine._parse_shift_start_time`. A job starting before this time can never be assigned to this driver. **This field existed in the schema and UI before it was actually enforced in the allocation engine — a real bug, since fixed. See AI_CONTEXT.md Section 9.** |
| `off_days` | TEXT, migration | Comma-separated lowercase weekday names, e.g. `"friday"` or `"friday,saturday"`. Hard rule — driver is skipped for jobs on these weekdays unless explicitly overridden per-date (`allow_override_days` param to `allocate()`, currently only usable programmatically — no UI for it yet). |
| `max_overtime_hours_per_month` | REAL, migration | `NULL`/blank = **unlimited overtime**. `0` = **no overtime allowed** (working_hours_per_day becomes a strict daily ceiling). Positive number = monthly overtime budget, checked against `finalized_jobs` history. |
| `total_hours_per_month_target` | REAL, migration | Informational only, mainly for temp drivers. **Not currently enforced anywhere in `allocate()`** — stored and displayed, no logic reads it yet. |
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
| `rule_type` | TEXT | Output of `rules_parser.parse_rule_line()` — one of the recognized types (`shift_start`, `max_hours`, `qualified_vehicle_types`, `off_day`, `leave`, etc.) or `"custom"` if unrecognized. **Important: even when this recognizes a pattern like `max_hours`, it is NOT fed into the allocation engine's hard-rule logic** — that logic reads only the structured `drivers.*` columns above. This table/parser pairing is a historical artifact now serving AI-context purposes only. |
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
| `capacity_notes` | TEXT | Free text, informational only |
| `in_workshop` | INTEGER (0/1) | Specific "under maintenance" status, separate from the general exclusion toggle below |
| `active` | INTEGER (0/1) | Same caveat as other `active` columns |
| `created_at`, `updated_at` | TEXT | |
| `excluded_from_planning` | INTEGER (0/1), migration | General "don't use tomorrow" (e.g. parked at an event site serving as temporary storage — a real scenario the user described). `allocation_engine.build_vehicle_profiles` treats `in_workshop` and `excluded_from_planning` as equivalent — a vehicle is unavailable if *either* is set. |
| `exclusion_reason` | TEXT, migration | |

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

`db.resolve_location(conn, raw_text)` returns `{"address": str, "exact":
bool}` — `exact=True` only if a matching `short_code` row exists;
otherwise the raw text is used as-is with `exact=False`, and the AI
review layer is instructed to be more conservative with suggestions
based on `"approximate (area-level)"` confidence data.

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

Unresolved jobs (no assignment found) are **not** written to
`finalized_jobs` at all — `PlanDayTab._on_finalize` explicitly skips
`j.unresolved` jobs when building the rows to save.

## Relationships (foreign keys)

```
drivers (1) ----< driver_rules (many)         ON DELETE CASCADE
suppliers (1) ----< supplier_rules (many)      ON DELETE CASCADE
suppliers (1) ----< supplier_offerings (many)  ON DELETE CASCADE
drivers (1) ----< off_day_log (many)           ON DELETE CASCADE  [unused]
drivers (1) ----< comp_days (many)             ON DELETE CASCADE  [unused]

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

**Month-to-date overtime for a driver** (the actual hard-cap check basis):
```sql
-- db.get_driver_month_overtime_hours(conn, driver_id, year, month, working_hours_per_day)
SELECT plan_date, SUM(hours) AS day_total FROM finalized_jobs
WHERE driver_id = ? AND plan_date LIKE ?   -- e.g. '2026-03%'
GROUP BY plan_date
-- then in Python: sum(max(0, day_total - working_hours_per_day) for each day)
```
This groups by day *first*, then sums only the excess over baseline per
day — a driver who works exactly their normal hours every day shows zero
overtime even with a large month-to-date total. This is a deliberate
design distinct from a naive "total hours" sum.

**Cross-day supplier fairness tiebreak:**
```sql
-- db.get_supplier_cumulative_hours(conn, supplier_id, since_date=None)
SELECT COALESCE(SUM(hours), 0) FROM finalized_jobs WHERE supplier_id = ?
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

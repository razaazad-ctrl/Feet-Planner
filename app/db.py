"""
db.py

Local persistent storage for the Fleet Planner app, backed by SQLite.
One file, lives on the planner's PC (default: next to the app, or in
%APPDATA%\\FleetPlanner on Windows once packaged).

Everything here is master data that's set up once and edited only when
something actually changes (a new driver joins, a supplier's rate changes,
a vehicle goes to the workshop, etc.) -- never re-entered per planning day.
"""
import sqlite3
import os
import json
from datetime import datetime

from app.rules_parser import parse_rule_line

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fleetplanner.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS drivers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS driver_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id   INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    line_text   TEXT NOT NULL,
    rule_type   TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    sort_order  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    line_text   TEXT NOT NULL,
    rule_type   TEXT NOT NULL,
    parsed_json TEXT NOT NULL,
    sort_order  INTEGER NOT NULL
);

-- Vehicles (in-house fleet)
CREATE TABLE IF NOT EXISTS vehicles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plate       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    vehicle_type TEXT NOT NULL,
    capacity_notes TEXT,
    in_workshop INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Off-day schedule tracking, one row per driver per planned day
CREATE TABLE IF NOT EXISTS off_day_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,
    scheduled_off   INTEGER NOT NULL DEFAULT 0,
    overridden      INTEGER NOT NULL DEFAULT 0,
    note            TEXT,
    UNIQUE(driver_id, date)
);

-- Comp days owed when a scheduled off day is overridden by the planner
CREATE TABLE IF NOT EXISTS comp_days (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id       INTEGER NOT NULL REFERENCES drivers(id) ON DELETE CASCADE,
    earned_date     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'owed',  -- 'owed' | 'applied'
    applied_date    TEXT,
    note            TEXT
);

-- Simple local key/value store, used for the two API keys. Stored only
-- on this PC, never sent anywhere except directly to Anthropic/Google
-- when the app itself makes a request.
CREATE TABLE IF NOT EXISTS app_settings (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- Every AI suggestion the planner acts on gets logged here, in full,
-- forever. This table is NEVER sent to Claude directly -- it just grows
-- as a local record. Only the small digest below is ever included in a
-- daily AI Review call, which is what keeps cost and speed flat over time.
CREATE TABLE IF NOT EXISTS decision_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date       TEXT NOT NULL,
    affected_jobs   TEXT,          -- comma-separated SR numbers
    suggestion_type TEXT,
    reasoning       TEXT,
    action          TEXT NOT NULL, -- 'accepted' | 'rejected'
    logged_at       TEXT NOT NULL
);

-- A single-row table holding the current compact "preferences digest" --
-- a short, fixed-size summary of the planner's demonstrated real-world
-- choices, periodically refreshed from decision_log. This is the ONLY
-- thing from the planner's history that ever gets sent to Claude, which
-- is what keeps daily token cost constant no matter how many years of
-- decisions have accumulated in decision_log.
CREATE TABLE IF NOT EXISTS preference_digest (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    digest_text         TEXT NOT NULL DEFAULT '',
    last_refreshed_at   TEXT,
    covered_through_date TEXT
);

-- Short-code location lookup: maps a code as it actually appears in the
-- daily Excel file (e.g. "CPK", "BQT STORE", "DICC") to a real address
-- Google Maps can resolve precisely. Locations NOT in this table (e.g.
-- a bare area name like "Dubai - AL MIZHAR", or a one-off customer
-- address that came in late) still get looked up by their raw text --
-- they just get flagged as an approximate/area-level estimate rather
-- than an exact one, so the planner and the AI both know which travel
-- times to trust more.
CREATE TABLE IF NOT EXISTS locations (
    short_code      TEXT PRIMARY KEY COLLATE NOCASE,
    full_address    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""


def get_connection(db_path=None):
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA)
    _run_migrations(conn)
    conn.commit()
    conn.close()


# Columns added after the initial release of each table. Each entry is
# (table, column_definition_sql). Adding a new column here is safe to run
# against an existing database -- it's skipped automatically if already
# present, so people don't have to delete their database file every time
# a small field gets added (only real structural changes still need that).
_MIGRATIONS = [
    ("drivers", "excluded_from_planning INTEGER NOT NULL DEFAULT 0"),
    ("drivers", "exclusion_reason TEXT"),
    ("suppliers", "excluded_from_planning INTEGER NOT NULL DEFAULT 0"),
    ("suppliers", "exclusion_reason TEXT"),
    ("vehicles", "excluded_from_planning INTEGER NOT NULL DEFAULT 0"),
    ("vehicles", "exclusion_reason TEXT"),
    # Structured hard-rule fields for drivers -- exact format, reliably
    # enforced, instead of free-text lines that can silently fail to match.
    ("drivers", "working_hours_per_day REAL"),
    ("drivers", "shift_start TEXT"),                  # DEPRECATED (see HR-002 rework below) -- kept so
                                                       # old data isn't lost; no longer read by the engine.
    ("drivers", "off_days TEXT"),                    # comma-separated lowercase weekday names
    ("drivers", "max_overtime_hours_per_month REAL"), # NULL = unlimited overtime
    ("drivers", "total_hours_per_month_target REAL"), # mainly informational, for temp drivers
    ("drivers", "license_types TEXT"),                # comma-separated vehicle types
    # --- HR-002 rework -------------------------------------------------
    # shift_period replaces shift_start: the planner no longer commits to
    # an exact clock time before planning. They just mark this driver as
    # "morning" or "evening" (or leave blank = no restriction); the exact
    # first-job time that day falls out of the plan itself and is
    # reported back to the driver afterward, not chosen in advance.
    ("drivers", "shift_period TEXT"),                 # 'morning' | 'evening' | NULL
    # Replaces the old hardcoded MAX_OVERTIME_HOURS_PER_DAY=2.0 constant
    # in allocation_engine.py. NULL = no daily overtime allowed beyond
    # working_hours_per_day (fail-closed, matching the same precedent as
    # max_overtime_hours_per_month=None elsewhere in this file).
    ("drivers", "max_working_hours_per_day REAL"),
    # --- Vehicle Maintenance Log (2026-08-14) ---------------------------
    # `in_workshop` (above) is now DEPRECATED, same precedent as
    # `drivers.shift_start`: the Vehicles tab's new single Active/Deactive
    # checkbox reuses the existing `excluded_from_planning` column for
    # both "in workshop" and "don't use tomorrow" -- both toggles are
    # gone from the UI, and allocation_engine.build_vehicle_profiles no
    # longer reads in_workshop. Column kept so old data isn't lost.
    ("vehicles", "vehicle_picture BLOB"),
    ("vehicles", "vehicle_model TEXT"),
    ("vehicles", "vehicle_year INTEGER"),
    ("vehicles", "vehicle_chassis TEXT"),
    ("vehicles", "vehicle_engine TEXT"),
    ("vehicles", "vehicle_registration TEXT"),
    ("vehicles", "vehicle_reg_expiry TEXT"),          # ISO date
    ("vehicles", "tyre_size TEXT"),
    ("vehicles", "battery_type TEXT"),
    ("vehicles", "rta_certificate TEXT"),
    ("vehicles", "rta_certificate_expiry TEXT"),      # ISO date
    ("vehicles", "ad_certificate TEXT"),
    ("vehicles", "ad_certificate_expiry TEXT"),       # ISO date
]


def _run_migrations(conn):
    for table, column_def in _MIGRATIONS:
        column_name = column_def.split()[0]
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    conn.executescript("""
    -- Structured rate/availability offering, one row per vehicle type a
    -- supplier provides. Replaces the old "pre-named unit" model -- the
    -- app now generates unit numbering/naming dynamically at planning
    -- time based on how many separate hires a day actually needs.
    CREATE TABLE IF NOT EXISTS supplier_offerings (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id             INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
        vehicle_type            TEXT NOT NULL,
        rate_per_hour           REAL,
        max_available_per_day   INTEGER
    );

    -- Every job's FINAL assignment, saved once the planner finalizes a
    -- day. This is the historical record everything cross-day depends on:
    -- monthly overtime caps, monthly hour targets, and giving suppliers
    -- equal business opportunity over time.
    CREATE TABLE IF NOT EXISTS finalized_jobs (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_date           TEXT NOT NULL,
        sr                  TEXT,
        driver_id           INTEGER,
        vehicle_id          INTEGER,
        supplier_id         INTEGER,
        supplier_label      TEXT,   -- e.g. "AL LAITH PASSENGER TRANSPORT 1"
        start_dt            TEXT,
        end_dt              TEXT,
        hours               REAL,
        finalized_at        TEXT NOT NULL
    );

    -- Vehicle Maintenance Log (2026-08-14): one row per service/repair/
    -- inspection event for one vehicle. Linked by vehicle_id (not plate
    -- text, even though the original design sketch drew the relationship
    -- on Plate) -- vehicle_id is the stable key every other relationship
    -- in this schema already uses. The five summary cards on the
    -- Maintenance Log window (Vehicle Expiry, Battery/Tyre/Oil/Chiller)
    -- are derived by finding each vehicle's most recent row per
    -- service_type -- nothing about "last service date" is stored
    -- redundantly here.
    CREATE TABLE IF NOT EXISTS service_records (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
        start_date      TEXT,
        end_date        TEXT,
        service_type    TEXT,   -- e.g. "Oil/Filter Change", "Chiller Unit Service", "Tyre Change"
        details         TEXT,
        current_reading REAL,
        next_reading    REAL,
        qty             REAL,
        person          TEXT,
        workshop        TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    """)


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- drivers

def add_driver(conn, name):
    now = _now()
    cur = conn.execute(
        "INSERT INTO drivers (name, active, created_at, updated_at) VALUES (?, 1, ?, ?)",
        (name.strip(), now, now),
    )
    conn.commit()
    return cur.lastrowid


def delete_driver(conn, driver_id):
    conn.execute("DELETE FROM drivers WHERE id = ?", (driver_id,))
    conn.commit()


def list_drivers(conn, active_only=True):
    q = "SELECT * FROM drivers"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY excluded_from_planning, name COLLATE NOCASE"
    return conn.execute(q).fetchall()


def set_driver_excluded(conn, driver_id, excluded, reason=""):
    conn.execute(
        "UPDATE drivers SET excluded_from_planning = ?, exclusion_reason = ?, updated_at = ? WHERE id = ?",
        (1 if excluded else 0, reason, _now(), driver_id),
    )
    conn.commit()


def set_driver_hard_rules(conn, driver_id, working_hours_per_day=None, shift_period=None,
                           off_days=None, max_overtime_hours_per_month=None,
                           total_hours_per_month_target=None, license_types=None,
                           max_working_hours_per_day=None):
    """
    off_days: list of lowercase weekday strings, or None
    license_types: list of vehicle-type strings, or None
    shift_period: 'morning', 'evening', or None (no restriction) -- see HR-002 rework.
    All numeric args: None means "not set" (no cap / not applicable).
    """
    if shift_period not in (None, "morning", "evening"):
        raise ValueError(f"shift_period must be 'morning', 'evening', or None, got {shift_period!r}")
    conn.execute(
        "UPDATE drivers SET working_hours_per_day = ?, shift_period = ?, off_days = ?, "
        "max_overtime_hours_per_month = ?, total_hours_per_month_target = ?, license_types = ?, "
        "max_working_hours_per_day = ?, updated_at = ? WHERE id = ?",
        (
            working_hours_per_day, shift_period,
            ",".join(off_days) if off_days else None,
            max_overtime_hours_per_month, total_hours_per_month_target,
            ",".join(license_types) if license_types else None,
            max_working_hours_per_day,
            _now(), driver_id,
        ),
    )
    conn.commit()


def get_driver_month_to_date_hours(conn, driver_id, year, month):
    """Sums this driver's finalized hours for the given calendar month --
    used to enforce the monthly overtime cap. Returns 0.0 if no history yet."""
    prefix = f"{year:04d}-{month:02d}"
    row = conn.execute(
        "SELECT COALESCE(SUM(hours), 0) AS total FROM finalized_jobs "
        "WHERE driver_id = ? AND plan_date LIKE ?",
        (driver_id, f"{prefix}%"),
    ).fetchone()
    return row["total"]


def _driver_month_daily_span_hours(conn, driver_id, year, month):
    """Returns a list of per-day duty-SPAN hours (earliest finalized job
    start to latest finalized job end, per calendar day) for this driver in
    the given month. Shared by get_driver_month_overtime_hours (which
    subtracts a per-day baseline from each) and get_driver_month_span_hours
    (which sums these directly) -- centralizes the day-grouped span
    calculation in one place so a future correction to it (see Phase 23,
    2026-08-14) only has to happen once."""
    prefix = f"{year:04d}-{month:02d}"
    rows = conn.execute(
        "SELECT plan_date, MIN(start_dt) AS day_start, MAX(end_dt) AS day_end "
        "FROM finalized_jobs WHERE driver_id = ? AND plan_date LIKE ? "
        "GROUP BY plan_date",
        (driver_id, f"{prefix}%"),
    ).fetchall()
    spans = []
    for r in rows:
        if not r["day_start"] or not r["day_end"]:
            continue
        spans.append((datetime.fromisoformat(r["day_end"]) - datetime.fromisoformat(r["day_start"])).total_seconds() / 3600.0)
    return spans


def get_driver_month_overtime_hours(conn, driver_id, year, month, working_hours_per_day):
    """
    Sums OVERTIME specifically (SPAN beyond working_hours_per_day on each
    individual finalized day -- earliest job start to latest job end that
    day -- not summed job duration), not just total hours -- a driver
    working exactly their normal hours every day should show zero overtime
    even with a high month-to-date total. Groups finalized_jobs by day
    first (via _driver_month_daily_span_hours).

    CORRECTED 2026-08-14 (Phase 23) to match the Phase 21 (2026-08-10)
    principle that every daily/monthly overtime check in this project uses
    duty SPAN, not summed job duration: this function previously summed
    each day's `hours` column (per-job durations), which under-counts
    whenever a driver had a genuine idle gap between two jobs on the same
    day (summed duration < span any time jobs aren't perfectly
    back-to-back) -- meaning the historical month_overtime_so_far figure
    every allocate_by_*() strategy's hard-rule checks depend on was
    silently more permissive than the corrected model intends. Found while
    building the "Balance Overtime / month" Drivers-tab field, which
    reuses this function directly.
    """
    return sum(
        max(0.0, span_hours - working_hours_per_day)
        for span_hours in _driver_month_daily_span_hours(conn, driver_id, year, month)
    )


def get_driver_month_span_hours(conn, driver_id, year, month):
    """
    Sums this driver's total duty SPAN across every finalized day in the
    given month -- a plain running total (NOT an excess-over-baseline
    figure like get_driver_month_overtime_hours). Basis for the Drivers
    tab's "Balance hours / month" field: total_hours_per_month_target
    minus this value. New 2026-08-14 (Phase 23) -- nothing tracked this
    monthly span accumulation before.
    """
    return sum(_driver_month_daily_span_hours(conn, driver_id, year, month))


# ------------------------------------------------------------ driver rules

def get_driver_rules(conn, driver_id):
    return conn.execute(
        "SELECT * FROM driver_rules WHERE driver_id = ? ORDER BY sort_order",
        (driver_id,),
    ).fetchall()


def add_driver_rule(conn, driver_id, line_text):
    rule_type, parsed_value = parse_rule_line(line_text)
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM driver_rules WHERE driver_id = ?",
        (driver_id,),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO driver_rules (driver_id, line_text, rule_type, parsed_json, sort_order) "
        "VALUES (?, ?, ?, ?, ?)",
        (driver_id, line_text.strip(), rule_type, json.dumps(parsed_value), max_order + 1),
    )
    _touch_driver(conn, driver_id)
    conn.commit()
    return cur.lastrowid, rule_type, parsed_value


def update_driver_rule(conn, rule_id, new_text):
    rule_type, parsed_value = parse_rule_line(new_text)
    conn.execute(
        "UPDATE driver_rules SET line_text = ?, rule_type = ?, parsed_json = ? WHERE id = ?",
        (new_text.strip(), rule_type, json.dumps(parsed_value), rule_id),
    )
    conn.commit()
    return rule_type, parsed_value


def delete_driver_rule(conn, rule_id):
    conn.execute("DELETE FROM driver_rules WHERE id = ?", (rule_id,))
    conn.commit()


def _touch_driver(conn, driver_id):
    conn.execute("UPDATE drivers SET updated_at = ? WHERE id = ?", (_now(), driver_id))


# --------------------------------------------------------------- suppliers

def add_supplier(conn, name):
    now = _now()
    cur = conn.execute(
        "INSERT INTO suppliers (name, active, created_at, updated_at) VALUES (?, 1, ?, ?)",
        (name.strip(), now, now),
    )
    conn.commit()
    return cur.lastrowid


def delete_supplier(conn, supplier_id):
    conn.execute("DELETE FROM suppliers WHERE id = ?", (supplier_id,))
    conn.commit()


def list_suppliers(conn, active_only=True):
    q = "SELECT * FROM suppliers"
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY excluded_from_planning, name COLLATE NOCASE"
    return conn.execute(q).fetchall()


def set_supplier_excluded(conn, supplier_id, excluded, reason=""):
    conn.execute(
        "UPDATE suppliers SET excluded_from_planning = ?, exclusion_reason = ?, updated_at = ? WHERE id = ?",
        (1 if excluded else 0, reason, _now(), supplier_id),
    )
    conn.commit()


# --------------------------------------------------------- supplier offerings

def add_supplier_offering(conn, supplier_id, vehicle_type, rate_per_hour, max_available_per_day):
    cur = conn.execute(
        "INSERT INTO supplier_offerings (supplier_id, vehicle_type, rate_per_hour, max_available_per_day) "
        "VALUES (?, ?, ?, ?)",
        (supplier_id, vehicle_type.strip(), rate_per_hour, max_available_per_day),
    )
    conn.commit()
    return cur.lastrowid


def delete_supplier_offering(conn, offering_id):
    conn.execute("DELETE FROM supplier_offerings WHERE id = ?", (offering_id,))
    conn.commit()


def get_supplier_offerings(conn, supplier_id):
    return conn.execute(
        "SELECT * FROM supplier_offerings WHERE supplier_id = ? ORDER BY vehicle_type", (supplier_id,)
    ).fetchall()


def list_all_supplier_offerings(conn):
    """Every offering across every non-excluded supplier, joined with the
    supplier name -- what the allocation engine actually needs."""
    return conn.execute(
        "SELECT so.*, s.name AS supplier_name FROM supplier_offerings so "
        "JOIN suppliers s ON s.id = so.supplier_id "
        "WHERE s.excluded_from_planning = 0 "
        "ORDER BY s.name, so.vehicle_type"
    ).fetchall()


# ----------------------------------------------------------- finalized jobs

def save_finalized_jobs(conn, plan_date, job_rows):
    """
    job_rows: list of dicts with keys sr, driver_id, vehicle_id, supplier_id,
    supplier_label, start_dt (iso str), end_dt (iso str), hours.
    Replaces any previously finalized rows for this plan_date (re-finalizing
    a day overwrites, rather than duplicating).
    """
    conn.execute("DELETE FROM finalized_jobs WHERE plan_date = ?", (plan_date,))
    now = _now()
    for r in job_rows:
        conn.execute(
            "INSERT INTO finalized_jobs (plan_date, sr, driver_id, vehicle_id, supplier_id, "
            "supplier_label, start_dt, end_dt, hours, finalized_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (plan_date, r.get("sr"), r.get("driver_id"), r.get("vehicle_id"), r.get("supplier_id"),
             r.get("supplier_label"), r.get("start_dt"), r.get("end_dt"), r.get("hours"), now),
        )
    conn.commit()


def get_supplier_cumulative_hours(conn, supplier_id, since_date=None):
    """Total historical hours given to this supplier -- used to prefer
    suppliers with less cumulative business when multiple offer the same
    type/rate, giving everyone a fair long-run opportunity."""
    q = "SELECT COALESCE(SUM(hours), 0) AS total FROM finalized_jobs WHERE supplier_id = ?"
    params = [supplier_id]
    if since_date:
        q += " AND plan_date >= ?"
        params.append(since_date)
    return conn.execute(q, params).fetchone()["total"]


# ---------------------------------------------------------- supplier rules

def get_supplier_rules(conn, supplier_id):
    return conn.execute(
        "SELECT * FROM supplier_rules WHERE supplier_id = ? ORDER BY sort_order",
        (supplier_id,),
    ).fetchall()


def add_supplier_rule(conn, supplier_id, line_text):
    rule_type, parsed_value = parse_rule_line(line_text)
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM supplier_rules WHERE supplier_id = ?",
        (supplier_id,),
    ).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO supplier_rules (supplier_id, line_text, rule_type, parsed_json, sort_order) "
        "VALUES (?, ?, ?, ?, ?)",
        (supplier_id, line_text.strip(), rule_type, json.dumps(parsed_value), max_order + 1),
    )
    conn.execute("UPDATE suppliers SET updated_at = ? WHERE id = ?", (_now(), supplier_id))
    conn.commit()
    return cur.lastrowid, rule_type, parsed_value


def update_supplier_rule(conn, rule_id, new_text):
    rule_type, parsed_value = parse_rule_line(new_text)
    conn.execute(
        "UPDATE supplier_rules SET line_text = ?, rule_type = ?, parsed_json = ? WHERE id = ?",
        (new_text.strip(), rule_type, json.dumps(parsed_value), rule_id),
    )
    conn.commit()
    return rule_type, parsed_value


def delete_supplier_rule(conn, rule_id):
    conn.execute("DELETE FROM supplier_rules WHERE id = ?", (rule_id,))
    conn.commit()


# --------------------------------------------------------------- vehicles

def add_vehicle(conn, plate, vehicle_type, capacity_notes=""):
    now = _now()
    cur = conn.execute(
        "INSERT INTO vehicles (plate, vehicle_type, capacity_notes, in_workshop, active, "
        "created_at, updated_at) VALUES (?, ?, ?, 0, 1, ?, ?)",
        (plate.strip(), vehicle_type.strip(), capacity_notes.strip(), now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_vehicle(conn, vehicle_id, plate, vehicle_type, capacity_notes=""):
    conn.execute(
        "UPDATE vehicles SET plate = ?, vehicle_type = ?, capacity_notes = ?, updated_at = ? "
        "WHERE id = ?",
        (plate.strip(), vehicle_type.strip(), capacity_notes.strip(), _now(), vehicle_id),
    )
    conn.commit()


def delete_vehicle(conn, vehicle_id):
    conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    conn.commit()


def get_vehicle(conn, vehicle_id):
    return conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()


def list_vehicles(conn, active_only=True):
    q = "SELECT * FROM vehicles"
    if active_only:
        q += " WHERE active = 1"
    # in_workshop dropped from the sort (2026-08-14) -- deprecated, no
    # longer surfaced in the UI; excluded_from_planning (driven by the
    # Vehicles tab's single Active/Deactive checkbox) is now the only
    # thing that demotes a vehicle to the bottom of the list.
    q += " ORDER BY excluded_from_planning, plate COLLATE NOCASE"
    return conn.execute(q).fetchall()


def set_vehicle_excluded(conn, vehicle_id, excluded, reason=""):
    conn.execute(
        "UPDATE vehicles SET excluded_from_planning = ?, exclusion_reason = ?, updated_at = ? WHERE id = ?",
        (1 if excluded else 0, reason, _now(), vehicle_id),
    )
    conn.commit()


def update_vehicle_maintenance_fields(conn, vehicle_id, vehicle_picture=None, vehicle_model="",
                                       vehicle_year=None, vehicle_chassis="", vehicle_engine="",
                                       vehicle_registration="", vehicle_reg_expiry=None,
                                       tyre_size="", battery_type="", capacity_notes="",
                                       rta_certificate="", rta_certificate_expiry=None,
                                       ad_certificate="", ad_certificate_expiry=None):
    """
    Saves the Vehicle Maintenance Log window's detail fields -- separate
    from update_vehicle() (which the basic Vehicles-tab "Edit Selected"
    dialog uses for just Plate/Type/Notes), so that existing dialog and
    its caller are untouched by this addition.

    vehicle_picture: raw image bytes (BLOB) or None to leave the current
    picture unchanged -- pass b"" explicitly to clear it.
    Date fields (vehicle_reg_expiry, rta_certificate_expiry,
    ad_certificate_expiry): ISO 'YYYY-MM-DD' strings or None.
    "Details" (per the original field list) reuses the vehicles table's
    existing capacity_notes column rather than adding a duplicate
    free-text field -- there was no meaningful difference between the two.
    """
    params = [
        vehicle_model.strip(), vehicle_year, vehicle_chassis.strip(), vehicle_engine.strip(),
        vehicle_registration.strip(), vehicle_reg_expiry, tyre_size.strip(), battery_type.strip(),
        capacity_notes.strip(), rta_certificate.strip(), rta_certificate_expiry,
        ad_certificate.strip(), ad_certificate_expiry, _now(),
    ]
    set_clause = (
        "vehicle_model = ?, vehicle_year = ?, vehicle_chassis = ?, vehicle_engine = ?, "
        "vehicle_registration = ?, vehicle_reg_expiry = ?, tyre_size = ?, battery_type = ?, "
        "capacity_notes = ?, rta_certificate = ?, rta_certificate_expiry = ?, "
        "ad_certificate = ?, ad_certificate_expiry = ?, updated_at = ?"
    )
    if vehicle_picture is not None:
        set_clause += ", vehicle_picture = ?"
        params.append(vehicle_picture)
    params.append(vehicle_id)
    conn.execute(f"UPDATE vehicles SET {set_clause} WHERE id = ?", params)
    conn.commit()


# ------------------------------------------------------ vehicle service log

def add_service_record(conn, vehicle_id, start_date=None, end_date=None, service_type="",
                        details="", current_reading=None, next_reading=None, qty=None,
                        person="", workshop=""):
    now = _now()
    cur = conn.execute(
        "INSERT INTO service_records (vehicle_id, start_date, end_date, service_type, details, "
        "current_reading, next_reading, qty, person, workshop, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (vehicle_id, start_date, end_date, service_type, details.strip(), current_reading,
         next_reading, qty, person.strip(), workshop.strip(), now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_service_record(conn, record_id, start_date=None, end_date=None, service_type="",
                           details="", current_reading=None, next_reading=None, qty=None,
                           person="", workshop=""):
    conn.execute(
        "UPDATE service_records SET start_date = ?, end_date = ?, service_type = ?, details = ?, "
        "current_reading = ?, next_reading = ?, qty = ?, person = ?, workshop = ?, updated_at = ? "
        "WHERE id = ?",
        (start_date, end_date, service_type, details.strip(), current_reading, next_reading,
         qty, person.strip(), workshop.strip(), _now(), record_id),
    )
    conn.commit()


def delete_service_record(conn, record_id):
    conn.execute("DELETE FROM service_records WHERE id = ?", (record_id,))
    conn.commit()


def list_service_records(conn, vehicle_id):
    """Every service record for one vehicle, oldest first -- the
    Maintenance Log window's own table scrolls to the bottom (most
    recent) row after populating, rather than this function guessing how
    many rows fit the visible window."""
    return conn.execute(
        "SELECT * FROM service_records WHERE vehicle_id = ? ORDER BY "
        "COALESCE(start_date, '') ASC, id ASC",
        (vehicle_id,),
    ).fetchall()


# ------------------------------------------------------------- off-days

def set_off_day_status(conn, driver_id, date_str, scheduled_off, overridden=False, note=""):
    conn.execute(
        "INSERT INTO off_day_log (driver_id, date, scheduled_off, overridden, note) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(driver_id, date) DO UPDATE SET "
        "scheduled_off=excluded.scheduled_off, overridden=excluded.overridden, note=excluded.note",
        (driver_id, date_str, int(scheduled_off), int(overridden), note),
    )
    conn.commit()


def add_comp_day(conn, driver_id, earned_date, note=""):
    cur = conn.execute(
        "INSERT INTO comp_days (driver_id, earned_date, status, note) VALUES (?, ?, 'owed', ?)",
        (driver_id, earned_date, note),
    )
    conn.commit()
    return cur.lastrowid


def list_owed_comp_days(conn, driver_id=None):
    q = "SELECT c.*, d.name AS driver_name FROM comp_days c JOIN drivers d ON d.id = c.driver_id WHERE c.status = 'owed'"
    params = ()
    if driver_id is not None:
        q += " AND c.driver_id = ?"
        params = (driver_id,)
    q += " ORDER BY c.earned_date"
    return conn.execute(q, params).fetchall()


def apply_comp_day(conn, comp_day_id, applied_date):
    conn.execute(
        "UPDATE comp_days SET status = 'applied', applied_date = ? WHERE id = ?",
        (applied_date, comp_day_id),
    )
    conn.commit()


# --------------------------------------------------------------- settings

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# ---------------------------------------------------------- decision log

def log_decision(conn, plan_date, affected_jobs, suggestion_type, reasoning, action):
    """action is 'accepted' or 'rejected'. Logged forever, full detail --
    this table is never sent to Claude directly; see preference_digest."""
    conn.execute(
        "INSERT INTO decision_log (plan_date, affected_jobs, suggestion_type, reasoning, action, logged_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (plan_date, ",".join(affected_jobs), suggestion_type, reasoning, action, _now()),
    )
    conn.commit()


def get_decisions_since(conn, since_date_iso):
    """since_date_iso: an ISO date string, or None for all decisions ever."""
    if since_date_iso:
        return conn.execute(
            "SELECT * FROM decision_log WHERE plan_date > ? ORDER BY plan_date", (since_date_iso,)
        ).fetchall()
    return conn.execute("SELECT * FROM decision_log ORDER BY plan_date").fetchall()


def count_undigested_decisions(conn):
    digest = get_digest(conn)
    since = digest["covered_through_date"] if digest else None
    return len(get_decisions_since(conn, since))


# ------------------------------------------------------- preference digest

def get_digest(conn):
    row = conn.execute("SELECT * FROM preference_digest WHERE id = 1").fetchone()
    return dict(row) if row else None


def save_digest(conn, digest_text, covered_through_date):
    conn.execute(
        "INSERT INTO preference_digest (id, digest_text, last_refreshed_at, covered_through_date) "
        "VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET digest_text = excluded.digest_text, "
        "last_refreshed_at = excluded.last_refreshed_at, covered_through_date = excluded.covered_through_date",
        (digest_text, _now(), covered_through_date),
    )
    conn.commit()


# --------------------------------------------------------------- locations

def add_location(conn, short_code, full_address):
    now = _now()
    conn.execute(
        "INSERT INTO locations (short_code, full_address, created_at, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(short_code) DO UPDATE SET full_address = excluded.full_address, updated_at = excluded.updated_at",
        (short_code.strip(), full_address.strip(), now, now),
    )
    conn.commit()


def delete_location(conn, short_code):
    conn.execute("DELETE FROM locations WHERE short_code = ?", (short_code,))
    conn.commit()


def list_locations(conn):
    return conn.execute("SELECT * FROM locations ORDER BY short_code COLLATE NOCASE").fetchall()


def resolve_location(conn, raw_text):
    """
    Looks up raw_text (as it appears in the daily Excel file, e.g. "CPK"
    or "BQT STORE") against the predefined locations table.

    Returns {"address": str, "exact": bool}. If a predefined short code
    matches, the real address is used and exact=True (Maps gets a precise
    point, so its travel-time estimate can be trusted fully). Otherwise
    the raw text itself is used as a best-effort address and exact=False
    -- Maps will still return a plausible average for that area, but the
    planner/AI should treat it as a rougher estimate, not a precise one.
    """
    if not raw_text:
        return {"address": "", "exact": False}
    row = conn.execute(
        "SELECT full_address FROM locations WHERE short_code = ?", (raw_text.strip(),)
    ).fetchone()
    if row:
        return {"address": row["full_address"], "exact": True}
    return {"address": raw_text.strip(), "exact": False}

import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from app import db

# Phase 31 (Schedules tab, 2026-08-16): pure db.py tests for the new
# finalized_jobs read/write functions and the cancelled-exclusion filter
# added to every existing hours/overtime/fairness reader. In-memory sqlite,
# no Qt -- same spirit as the rest of this project's tests/*.py, adapted
# for db.py since that's where the genuinely new logic in this feature
# lives (schedules_tab.py itself is a thin UI layer over these).

conn = db.get_connection(":memory:")
conn.executescript(db._SCHEMA)
db._run_migrations(conn)
conn.commit()

# --- insert/update/list round-trip -----------------------------------
new_id = db.insert_finalized_job(
    conn, "2026-08-01", sr="1", driver_id=5, driver_name="AHMED",
    start_dt="2026-08-01T07:00:00", end_dt="2026-08-01T15:00:00", hours=8.0,
    event_text="Wedding", pickup_location="CPK", vehicle_type_required="Bus",
)
assert isinstance(new_id, int)
rows = db.list_finalized_jobs(conn, "2026-08-01", "2026-08-01")
assert len(rows) == 1 and rows[0]["id"] == new_id and rows[0]["event_text"] == "Wedding"
print("PASS: insert_finalized_job + list_finalized_jobs round-trip")

db.update_finalized_job(conn, new_id, driver_id=9, driver_name="RAZA", hours=9.5)
row = conn.execute("SELECT * FROM finalized_jobs WHERE id=?", (new_id,)).fetchone()
assert row["driver_id"] == 9 and row["driver_name"] == "RAZA" and row["hours"] == 9.5
assert row["sr"] == "1" and row["event_text"] == "Wedding", "unrelated fields must be untouched"
print("PASS: update_finalized_job only touches the fields passed in")

db.update_finalized_job(conn, new_id, driver_id=None)
row = conn.execute("SELECT driver_id FROM finalized_jobs WHERE id=?", (new_id,)).fetchone()
assert row["driver_id"] is None
print("PASS: update_finalized_job can explicitly clear a field to NULL")

db.update_finalized_job(conn, new_id)  # no fields at all -- must be a safe no-op
print("PASS: update_finalized_job with no fields is a safe no-op")

# --- list_finalized_jobs date-range filtering --------------------------
db.insert_finalized_job(conn, "2026-08-15", sr="2", hours=1.0)
in_range = db.list_finalized_jobs(conn, "2026-08-01", "2026-08-10")
assert {r["sr"] for r in in_range} == {"1"}, "the 08-15 row must be outside this range"
print("PASS: list_finalized_jobs respects the date range (inclusive)")

# --- cancelled exclusion, the one behavior change to existing logic ----
driver_id = 42
db.insert_finalized_job(conn, "2026-08-01", driver_id=driver_id, hours=5.0,
                         start_dt="2026-08-01T08:00:00", end_dt="2026-08-01T13:00:00")
cancelled_id = db.insert_finalized_job(conn, "2026-08-01", driver_id=driver_id, hours=100.0,
                                        start_dt="2026-08-01T08:00:00", end_dt="2026-08-01T20:00:00")
db.update_finalized_job(conn, cancelled_id, cancelled=1)

month_hours = db.get_driver_month_to_date_hours(conn, driver_id, 2026, 8)
assert month_hours == 5.0, f"expected 5.0 (cancelled row excluded), got {month_hours}"
print("PASS: get_driver_month_to_date_hours excludes cancelled rows")

overtime = db.get_driver_month_overtime_hours(conn, driver_id, 2026, 8, working_hours_per_day=9.0)
assert overtime == 0.0, f"expected 0.0 (only the 5h non-cancelled row counts, under the 9h baseline), got {overtime}"
print("PASS: get_driver_month_overtime_hours excludes cancelled rows")

span_hours = db.get_driver_month_span_hours(conn, driver_id, 2026, 8)
assert span_hours == 5.0, f"expected 5.0, got {span_hours}"
print("PASS: get_driver_month_span_hours excludes cancelled rows")

supplier_id = 7
db.insert_finalized_job(conn, "2026-08-01", supplier_id=supplier_id, hours=3.0)
cancelled_supplier_id = db.insert_finalized_job(conn, "2026-08-01", supplier_id=supplier_id, hours=50.0)
db.update_finalized_job(conn, cancelled_supplier_id, cancelled=1)
supplier_total = db.get_supplier_cumulative_hours(conn, supplier_id)
assert supplier_total == 3.0, f"expected 3.0, got {supplier_total}"
print("PASS: get_supplier_cumulative_hours excludes cancelled rows")

# --- a non-cancelled row with the exact same driver/day is unaffected --
other_driver = 43
db.insert_finalized_job(conn, "2026-08-02", driver_id=other_driver, hours=6.0,
                         start_dt="2026-08-02T08:00:00", end_dt="2026-08-02T14:00:00")
total = db.get_driver_month_to_date_hours(conn, other_driver, 2026, 8)
assert total == 6.0, f"expected 6.0 (nothing cancelled for this driver), got {total}"
print("PASS: an otherwise-identical non-cancelled row is still counted normally")

print()
print("ALL FINALIZED_JOBS DB TESTS PASSED")

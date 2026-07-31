import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile, _parse_shift_start_time, _job_is_before_shift_start

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# --- Unit tests for the parsing helper itself ---
assert _parse_shift_start_time("07:00 AM") == dt(7).time()
assert _parse_shift_start_time("11:00 PM") == dt(23).time()
assert _parse_shift_start_time("18:00") == dt(18).time()
assert _parse_shift_start_time(None) is None
assert _parse_shift_start_time("") is None
assert _parse_shift_start_time("garbage") is None  # fails open, not silently blocking
print("PASS: _parse_shift_start_time handles AM/PM, 24h, blank, and garbage")

assert _job_is_before_shift_start(dt(6, 30), dt(7).time()) is True
assert _job_is_before_shift_start(dt(7, 0), dt(7).time()) is False
assert _job_is_before_shift_start(dt(23, 0), None) is False
print("PASS: _job_is_before_shift_start compares time-of-day correctly")

# --- Full allocate() test: a driver whose shift starts at 11 PM must NEVER
#     get a 10 AM job, even if they'd otherwise be the fairest/only choice ---
jobs_module = __import__("app.excel_import", fromlist=["Job"])
Job = jobs_module.Job

jobs = [
    Job(row_number=1, sr="1", start_dt=dt(10), end_dt=dt(12), vehicle_type_required="Bus"),
]
drivers = [
    DriverProfile(id=1, name="LATE_SHIFT_DRIVER", license_types=["Bus"], shift_start="11:00 PM"),
]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
assert jobs[0].assigned_driver_id is None, "A driver whose shift starts at 11 PM must not get a 10 AM job!"
assert jobs[0].unresolved
print("PASS: allocate() correctly refuses to assign a 10 AM job to an 11 PM-shift driver")

# --- Same scenario but a job AFTER shift start should succeed ---
jobs2 = [
    Job(row_number=1, sr="1", start_dt=dt(23, 30), end_dt=dt(23, 59), vehicle_type_required="Bus"),
]
drivers2 = [
    DriverProfile(id=1, name="LATE_SHIFT_DRIVER", license_types=["Bus"], shift_start="11:00 PM"),
]
allocate(jobs2, drivers2, vehicles, [])
assert jobs2[0].assigned_driver_id == 1, "A job starting after shift_start should be assignable"
print("PASS: a job starting after shift_start is correctly assignable")

# --- No shift_start configured at all -> no restriction (fail-open, matches
#     every other optional hard-rule field in this engine) ---
jobs3 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(8), vehicle_type_required="Bus")]
drivers3 = [DriverProfile(id=1, name="NO_SHIFT_CONFIGURED", license_types=["Bus"], shift_start=None)]
allocate(jobs3, drivers3, vehicles, [])
assert jobs3[0].assigned_driver_id == 1
print("PASS: no shift_start configured means no restriction")

print()
print("ALL SHIFT_START TESTS PASSED")

import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile, _job_matches_shift_period
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# HR-002 rework: the planner no longer picks an exact shift-start clock
# time in advance. They just mark a driver "morning" or "evening" (evening
# = anything from noon onward); the actual first-job time each day is
# whatever the plan produces, and is reported back afterward, not fixed
# beforehand.

# --- Unit tests for the window-check helper itself ---
assert _job_matches_shift_period(dt(6), "morning") is True
assert _job_matches_shift_period(dt(11, 59), "morning") is True
assert _job_matches_shift_period(dt(12, 0), "morning") is False   # noon is the cutoff -> evening
assert _job_matches_shift_period(dt(18), "morning") is False
assert _job_matches_shift_period(dt(12, 0), "evening") is True
assert _job_matches_shift_period(dt(23, 30), "evening") is True
assert _job_matches_shift_period(dt(6), "evening") is False
assert _job_matches_shift_period(dt(6), None) is True             # no restriction (fail-open)
assert _job_matches_shift_period(dt(18), None) is True
print("PASS: _job_matches_shift_period enforces the morning/evening noon split correctly")

# --- Full allocate() test: an evening-marked driver must NEVER get a
#     morning job, even if they'd otherwise be the fairest/only choice ---
jobs = [Job(row_number=1, sr="1", start_dt=dt(10), end_dt=dt(12), vehicle_type_required="Bus")]
drivers = [DriverProfile(id=1, name="EVENING_DRIVER", license_types=["Bus"], shift_period="evening")]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
assert jobs[0].assigned_driver_id is None, "An evening driver must not get a 10 AM job!"
assert jobs[0].unresolved
print("PASS: allocate() correctly refuses to assign a morning job to an evening-marked driver")

# --- Same scenario but an afternoon/evening job should succeed ---
jobs2 = [Job(row_number=1, sr="1", start_dt=dt(14), end_dt=dt(16), vehicle_type_required="Bus")]
drivers2 = [DriverProfile(id=1, name="EVENING_DRIVER", license_types=["Bus"], shift_period="evening")]
allocate(jobs2, drivers2, vehicles, [])
assert jobs2[0].assigned_driver_id == 1, "An afternoon job should be assignable to an evening-marked driver"
print("PASS: an afternoon job is correctly assignable to an evening-marked driver")

# --- Morning-marked driver: mirror check ---
jobs3 = [Job(row_number=1, sr="1", start_dt=dt(8), end_dt=dt(10), vehicle_type_required="Bus")]
drivers3 = [DriverProfile(id=1, name="MORNING_DRIVER", license_types=["Bus"], shift_period="morning")]
allocate(jobs3, drivers3, vehicles, [])
assert jobs3[0].assigned_driver_id == 1, "A morning job should be assignable to a morning-marked driver"
print("PASS: a morning job is correctly assignable to a morning-marked driver")

jobs4 = [Job(row_number=1, sr="1", start_dt=dt(15), end_dt=dt(17), vehicle_type_required="Bus")]
drivers4 = [DriverProfile(id=1, name="MORNING_DRIVER", license_types=["Bus"], shift_period="morning")]
allocate(jobs4, drivers4, vehicles, [])
assert jobs4[0].assigned_driver_id is None, "A morning-marked driver must not get an afternoon job"
print("PASS: an afternoon job is correctly refused for a morning-marked driver")

# --- No shift_period configured at all -> no restriction (fail-open, matches
#     every other optional hard-rule field in this engine) ---
jobs5 = [Job(row_number=1, sr="1", start_dt=dt(18), end_dt=dt(20), vehicle_type_required="Bus")]
drivers5 = [DriverProfile(id=1, name="NO_PERIOD_CONFIGURED", license_types=["Bus"], shift_period=None)]
allocate(jobs5, drivers5, vehicles, [])
assert jobs5[0].assigned_driver_id == 1
print("PASS: no shift_period configured means no restriction")

print()
print("ALL SHIFT_PERIOD TESTS PASSED")

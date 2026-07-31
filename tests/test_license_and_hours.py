import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# --- License types: a driver not licensed for the required type must never get the job ---
jobs = [Job(row_number=1, sr="1", start_dt=dt(9), end_dt=dt(11), vehicle_type_required="Chiller Truck")]
drivers = [DriverProfile(id=1, name="BUS_ONLY_DRIVER", license_types=["Bus"])]
vehicles = [VehicleProfile(id=1, plate="CHILLER-1", vehicle_type="Chiller Truck")]
allocate(jobs, drivers, vehicles, [])
assert jobs[0].assigned_driver_id is None, "Bus-only driver must NEVER be assigned a Chiller Truck job!"
print("PASS: license-type mismatch correctly blocks assignment")

# --- License types: a driver with NO license types configured at all must
#     never get any job (fail closed, not fail open) ---
jobs = [Job(row_number=1, sr="1", start_dt=dt(9), end_dt=dt(11), vehicle_type_required="Bus")]
drivers = [DriverProfile(id=1, name="UNCONFIGURED_DRIVER", license_types=[])]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
assert jobs[0].assigned_driver_id is None, "A driver with no license_types configured must not be assignable to anything!"
print("PASS: unconfigured license_types correctly blocks assignment (fails closed)")

# --- Working hours: driver with working_hours_per_day=9 and max_overtime=0
#     (KARIM KHAN's real config) must be refused a job that would push them past 9h today ---
jobs = [
    Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(15), vehicle_type_required="Bus"),   # 9h -> exactly at cap, OK
    Job(row_number=2, sr="2", start_dt=dt(16), end_dt=dt(17), vehicle_type_required="Bus"),  # +1h -> would exceed cap
]
drivers = [DriverProfile(id=1, name="ZERO_OVERTIME_DRIVER", license_types=["Bus"],
                          working_hours_per_day=9.0, max_overtime_hours_per_month=0.0)]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
assert jobs[0].assigned_driver_id == 1, "First 9h job should be assignable (exactly at cap)"
assert jobs[1].assigned_driver_id is None, "A driver at their daily cap with 0 monthly overtime allowance must not get a 10th hour!"
print("PASS: working_hours_per_day + max_overtime_hours_per_month=0 correctly blocks the extra hour")

# --- BUG CHECK: what happens if working_hours_per_day is set but
#     max_overtime_hours_per_month is left blank (None)? Currently the
#     entire hours-check block is skipped in that case -- meaning a driver
#     with a configured daily limit but no configured overtime cap can be
#     given UNLIMITED hours in one day, no check at all. ---
jobs = [
    Job(row_number=1, sr="1", start_dt=dt(0), end_dt=dt(9), vehicle_type_required="Bus"),
    Job(row_number=2, sr="2", start_dt=dt(9), end_dt=dt(18), vehicle_type_required="Bus"),
    Job(row_number=3, sr="3", start_dt=dt(18), end_dt=dt(23, 59), vehicle_type_required="Bus"),
]
drivers = [DriverProfile(id=1, name="NO_OVERTIME_CAP_DRIVER", license_types=["Bus"],
                          working_hours_per_day=9.0, max_overtime_hours_per_month=None)]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
all_assigned = all(j.assigned_driver_id == 1 for j in jobs)
print(f"Driver worked ~24h in one day, all 3 back-to-back jobs assigned: {all_assigned}")
if all_assigned:
    print("CONFIRMED BUG: working_hours_per_day is NOT enforced at all when max_overtime_hours_per_month is blank.")

print()
print("--- isolating the hours-cap loophole from the travel-buffer overlap check ---")
# Same idea, but jobs are spaced far enough apart that the 30-min travel
# buffer never rejects them -- isolates the hours-cap question specifically.
jobs2 = [
    Job(row_number=1, sr="1", start_dt=dt(0), end_dt=dt(8), vehicle_type_required="Bus"),
    Job(row_number=2, sr="2", start_dt=dt(9), end_dt=dt(17), vehicle_type_required="Bus"),
    Job(row_number=3, sr="3", start_dt=dt(18), end_dt=dt(23, 30), vehicle_type_required="Bus"),
]
drivers2 = [DriverProfile(id=1, name="NO_OVERTIME_CAP_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_overtime_hours_per_month=None)]
vehicles2 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs2, drivers2, vehicles2, [])
for j in jobs2:
    print(f"SR{j.sr} {j.start_dt.strftime('%H:%M')}-{j.end_dt.strftime('%H:%M')} -> assigned_driver_id={j.assigned_driver_id}")
total_hours = sum((j.end_dt - j.start_dt).total_seconds()/3600 for j in jobs2 if j.assigned_driver_id == 1)
print(f"Total hours given to this single driver in one day: {total_hours}")
assert jobs2[0].assigned_driver_id == 1, "First 8h job should be assignable (within the 9h daily cap)"
assert jobs2[1].assigned_driver_id is None, "Second job would push driver to 16h -- must be refused now that blank overtime = hard daily cap"
assert jobs2[2].assigned_driver_id is None, "Third job would also exceed the daily cap -- must be refused"
print("PASS: working_hours_per_day is now a hard daily cap when max_overtime_hours_per_month is blank (fixed)")

print()
print("--- confirm a driver still gets a full day up to their exact cap, just not beyond it ---")
jobs3 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(15), vehicle_type_required="Bus")]  # exactly 9h
drivers3 = [DriverProfile(id=1, name="EXACT_CAP_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_overtime_hours_per_month=None)]
vehicles3 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]  # fresh vehicle, not reused from jobs2
allocate(jobs3, drivers3, vehicles3, [])
assert jobs3[0].assigned_driver_id == 1, "A job landing exactly at the daily cap should still be assignable"
print("PASS: exactly-at-cap job still assignable (no off-by-one over-restriction)")

print()
print("ALL LICENSE/HOURS TESTS PASSED")

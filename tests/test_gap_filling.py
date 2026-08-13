import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# OPT-001/002/003 fix (2026-08-03): reproduces the exact real-world
# symptom reported by the project owner testing against UNPLANNED.xlsx --
# a driver ends up with two jobs 12h apart (13:00-15:00, 22:00-01:00,
# only 5h actual work, 7h idle in the middle) while a job that fits
# neatly in that gap (16:00-20:00) is left completely unassigned.
early = Job(row_number=1, sr="1", start_dt=dt(13), end_dt=dt(15), vehicle_type_required="Bus")
middle = Job(row_number=2, sr="2", start_dt=dt(16), end_dt=dt(20), vehicle_type_required="Bus")
late = Job(row_number=3, sr="3", start_dt=dt(22, d=15), end_dt=dt(1, d=16), vehicle_type_required="Bus")
jobs = [early, middle, late]

# Two qualifying drivers -- one will naturally pick up the early+late jobs
# via ordinary least-occupied-first fairness (leaving a gap), the other
# stays fully idle. Before the fix, the middle job either goes to the
# idle driver (wasteful -- two drivers active for what one could cover)
# or, in the reported real case, fails to find any driver at all and is
# left unresolved.
drivers = [
    DriverProfile(id=1, name="D1", license_types=["Bus"], working_hours_per_day=9.0, max_working_hours_per_day=12.0, max_overtime_hours_per_month=60.0),
    DriverProfile(id=2, name="D2", license_types=["Bus"], working_hours_per_day=9.0, max_working_hours_per_day=12.0, max_overtime_hours_per_month=60.0),
]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus"),
            VehicleProfile(id=2, plate="BUS-2", vehicle_type="Bus")]

allocate(jobs, drivers, vehicles, [])

for j in jobs:
    print(f"SR{j.sr} {j.start_dt.strftime('%H:%M')}-{j.end_dt.strftime('%H:%M')} -> "
          f"driver_id={j.assigned_driver_id} unresolved={j.unresolved} note={j.assignment_note}")

assert not middle.unresolved and middle.assigned_driver_id is not None, \
    "The 16:00-20:00 job should no longer be left unassigned -- it fits cleanly in the gap"
assert middle.assigned_driver_id == early.assigned_driver_id == late.assigned_driver_id, \
    "The gap-fill job should land on the SAME driver who already has the bounding early/late jobs, not a fresh idle driver"
print("PASS: the gap job is filled onto the driver whose existing schedule it fits into")

total_drivers_used = len({j.assigned_driver_id for j in jobs if j.assigned_driver_id is not None})
assert total_drivers_used == 1, f"Expected only 1 driver used (gap filled), got {total_drivers_used}"
print("PASS: only one driver needed for all three jobs, instead of leaving a 7h idle hole + an idle second driver")

# --- Sanity check: gap-filling must NOT override a hard rule. If the
# only driver with a bounded gap can't legally take the job (e.g. it
# would push them over their daily ceiling), it must stay unresolved.
early2 = Job(row_number=1, sr="1", start_dt=dt(0), end_dt=dt(4), vehicle_type_required="Bus")     # 4h
middle2 = Job(row_number=2, sr="2", start_dt=dt(6), end_dt=dt(16), vehicle_type_required="Bus")   # 10h -- would push driver to 18h total
late2 = Job(row_number=3, sr="3", start_dt=dt(20), end_dt=dt(23), vehicle_type_required="Bus")    # 3h
jobs2 = [early2, middle2, late2]
drivers2 = [DriverProfile(id=1, name="D1", license_types=["Bus"], working_hours_per_day=9.0, max_working_hours_per_day=12.0)]
vehicles2 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs2, drivers2, vehicles2, [])
assert middle2.unresolved, "A gap-fill that would break the daily ceiling must stay unresolved, not be forced in"
print("PASS: gap-filling still respects the daily hour ceiling -- doesn't force an illegal assignment")

print()
print("ALL GAP-FILLING TESTS PASSED")

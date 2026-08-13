import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile, _merged_hours
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# Hour-accounting fix (2026-08-03): confirmed against a real PLANNED.xlsx
# that occupied_seconds naively summing every row's duration -- even when
# two rows in the same "Same Driver" group are the exact same time slot
# (e.g. two simultaneous pickups on one truck) -- overstates a driver's
# true hours enough to falsely trip the daily ceiling. Real example found:
# a driver with ~11h of ACTUAL work was showing ~17h of "occupied" time.

# --- Direct unit tests for _merged_hours ---
assert _merged_hours([]) == 0.0
assert _merged_hours([(dt(9), dt(12))]) == 3.0
assert _merged_hours([(dt(9), dt(12)), (dt(9), dt(12))]) == 3.0, "identical overlapping intervals must count once"
assert _merged_hours([(dt(9), dt(12)), (dt(10), dt(14))]) == 5.0, "partial overlap: union is 9-14 = 5h"
assert _merged_hours([(dt(9), dt(12)), (dt(12), dt(15))]) == 6.0, "touching intervals: 9-15 = 6h"
assert _merged_hours([(dt(9), dt(10)), (dt(14), dt(16))]) == 3.0, "disjoint: 1h + 2h = 3h, gap doesn't count"
print("PASS: _merged_hours computes true union hours correctly")

# --- Reproduces the exact real-world pattern found in PLANNED.xlsx
# (ARAVIND BALAKRISHNAN): a same-time duplicate-pickup pair (3h, counts
# once for fairness/occupied_seconds purposes) plus a separate contiguous
# block. Job times chosen (2026-08-10 duty-span correction) so the whole
# day's SPAN, not just the summed/deduplicated duration, fits exactly at
# a 9h ceiling -- 04:00 to 13:00, back-to-back with no gaps. Must fit
# cleanly under a 9/9 hour driver with zero overtime allowed. ---
jobs = [
    Job(row_number=1, sr="1", start_dt=dt(4), end_dt=dt(7), vehicle_type_required="Bus", same_driver_key="EVENT-A"),
    Job(row_number=2, sr="2", start_dt=dt(4), end_dt=dt(7), vehicle_type_required="Bus", same_driver_key="EVENT-A"),  # simultaneous duplicate pickup
    Job(row_number=3, sr="3", start_dt=dt(7), end_dt=dt(9), vehicle_type_required="Bus", same_driver_key="EVENT-B"),
    Job(row_number=4, sr="4", start_dt=dt(9), end_dt=dt(10), vehicle_type_required="Bus", same_driver_key="EVENT-B"),
    Job(row_number=5, sr="5", start_dt=dt(10), end_dt=dt(13), vehicle_type_required="Bus", same_driver_key="EVENT-B"),
]
drivers = [DriverProfile(id=1, name="D1", license_types=["Bus"], working_hours_per_day=9.0,
                          max_working_hours_per_day=9.0, max_overtime_hours_per_month=None)]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus"),
            VehicleProfile(id=2, plate="BUS-2", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
for j in jobs:
    print(f"SR{j.sr} {j.start_dt.strftime('%H:%M')}-{j.end_dt.strftime('%H:%M')} -> driver_id={j.assigned_driver_id} unresolved={j.unresolved}")
assert all(j.assigned_driver_id == 1 for j in jobs), "All 5 rows should fit on one 9h-max driver -- true SPAN is exactly 9h (04:00-13:00), back to back"
assert not any(j.unresolved for j in jobs)
print("PASS: simultaneous duplicate-pickup pair correctly counted once, whole day fits under a 9h zero-overtime driver")

# --- Driver Only jobs (NEW-004 fix) need no vehicle at all ---
jobs2 = [Job(row_number=1, sr="1", start_dt=dt(15), end_dt=dt(17), vehicle_type_required="Driver Only")]
drivers2 = [DriverProfile(id=1, name="D1", license_types=["Driver Only"], working_hours_per_day=None)]  # hours rule irrelevant to this test
allocate(jobs2, drivers2, [], [])  # note: empty vehicle pool
assert jobs2[0].assigned_driver_id == 1 and jobs2[0].assigned_vehicle_id is None, \
    "A 'Driver Only' job should be assignable even with zero vehicles available"
print("PASS: 'Driver Only' jobs no longer require a matching vehicle")

print()
print("ALL HOUR-ACCOUNTING TESTS PASSED")

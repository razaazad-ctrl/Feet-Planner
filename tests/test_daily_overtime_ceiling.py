import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# Reproduce the exact reported symptom: a driver with plenty of unused
# monthly overtime should NOT be able to get a ~22h single day (7 AM to
# 5 AM next day), because that's 13h of overtime in one day, way past
# any sane daily ceiling, even though 13h is well under a 60h/month cap.
jobs = [
    Job(row_number=1, sr="1", start_dt=dt(7, d=15), end_dt=dt(23, 59, d=15), vehicle_type_required="Bus"),
    Job(row_number=2, sr="2", start_dt=dt(0, 5, d=16), end_dt=dt(5, 0, d=16), vehicle_type_required="Bus"),
]
drivers = [DriverProfile(id=1, name="AMPLE_MONTHLY_BUDGET_DRIVER", license_types=["Bus"],
                          working_hours_per_day=9.0, max_overtime_hours_per_month=60.0,
                          month_overtime_so_far=0.0)]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])

total_hours_today = sum((j.end_dt - j.start_dt).total_seconds()/3600 for j in jobs if j.assigned_driver_id == 1)
print(f"Hours actually given to this driver on day 1: {total_hours_today}")
assert total_hours_today <= 11.0 + 1e-6, \
    f"Driver was given {total_hours_today}h in one day -- daily ceiling (9h + 2h overtime = 11h) not enforced!"
print("PASS: driver capped at 11h/day (9h + 2h overtime ceiling) even with 60h/month unused")

# Confirm a driver CAN still use overtime up to the daily ceiling, just not beyond
jobs2 = [Job(row_number=1, sr="1", start_dt=dt(7), end_dt=dt(18), vehicle_type_required="Bus")]  # 11h exactly
drivers2 = [DriverProfile(id=1, name="EXACT_DAILY_CEILING_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_overtime_hours_per_month=60.0)]
vehicles2 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs2, drivers2, vehicles2, [])
assert jobs2[0].assigned_driver_id == 1, "An 11h day (exactly at the 9+2 ceiling) should still be assignable"
print("PASS: exactly-11h day still assignable (no off-by-one over-restriction)")

# Confirm going 1 minute past the ceiling is rejected
jobs3 = [Job(row_number=1, sr="1", start_dt=dt(7), end_dt=dt(18, 1), vehicle_type_required="Bus")]  # 11h01m
drivers3 = [DriverProfile(id=1, name="OVER_DAILY_CEILING_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_overtime_hours_per_month=60.0)]
vehicles3 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs3, drivers3, vehicles3, [])
assert jobs3[0].assigned_driver_id is None, "11h01m should be rejected -- 1 minute past the daily ceiling"
print("PASS: 1 minute past the daily ceiling is correctly rejected")

print()
print("ALL DAILY OVERTIME CEILING TESTS PASSED")

import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# NEW-007 fix (2026-08-03): the project owner's exact real scenario. One
# driver is licensed ONLY for "10 Ton Chiller Truck" -- that day's full
# set of Chiller Truck requests (12:00-17:00 x2 simultaneous, 17:00-22:00,
# 23:00-00:00 x2 simultaneous -- merges to 11h true work) fits cleanly
# inside their 9/12 hour rule with room to spare. But a generalist driver
# (also licensed for Chiller, plus other types) is ALSO in the pool, and
# an earlier, non-exclusive Chiller job could have gone to either of them.
# If that early shared job wrongly lands on the specialist first, it can
# burn just enough of their capacity that one of the LATER exclusive
# requests no longer fits under their ceiling -- even though a driver who
# could ONLY do Chiller work was otherwise sitting idle enough to take it.
SPECIALIST = DriverProfile(id=1, name="SPECIALIST", license_types=["Chiller Truck"],
                            working_hours_per_day=9.0, max_working_hours_per_day=12.0,
                            max_overtime_hours_per_month=60.0)
GENERALIST = DriverProfile(id=2, name="GENERALIST", license_types=["Chiller Truck", "Bus"],
                            working_hours_per_day=9.0, max_working_hours_per_day=12.0,
                            max_overtime_hours_per_month=60.0)
drivers = [SPECIALIST, GENERALIST]
vehicles = [VehicleProfile(id=1, plate="CHILLER-1", vehicle_type="Chiller Truck"),
            VehicleProfile(id=2, plate="CHILLER-2", vehicle_type="Chiller Truck"),
            VehicleProfile(id=3, plate="BUS-1", vehicle_type="Bus")]

# An early shared Chiller job BOTH drivers could do, processed first
# (earliest start time) -- this is the one that should go to the
# generalist, not the specialist. Paired with a same-day Bus job for the
# generalist so their OWN day clears the 9h minimum independently -- kept
# separate from HR-005 (daily minimum) interactions on purpose; this test
# is about NEW-007 (specialist reservation) specifically.
shared_early_job = Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(8), vehicle_type_required="Chiller Truck")
generalist_bus_job = Job(row_number=7, sr="7", start_dt=dt(9), end_dt=dt(16), vehicle_type_required="Bus")

# The specialist's exclusive full-day demand -- exactly the project
# owner's real example (times/duplicates included). Flagged as one "Same
# Driver" group since the duplicate pairs represent one truck serving two
# simultaneous orders (same as the real-world pattern found earlier in
# PLANNED.xlsx) -- without this tag the duplicate pairs would (correctly)
# conflict with each other as a genuine double-booking.
exclusive_jobs = [
    Job(row_number=2, sr="2", start_dt=dt(12), end_dt=dt(17), vehicle_type_required="Chiller Truck", same_driver_key="G1"),
    Job(row_number=3, sr="3", start_dt=dt(12), end_dt=dt(17), vehicle_type_required="Chiller Truck", same_driver_key="G1"),  # simultaneous duplicate
    Job(row_number=4, sr="4", start_dt=dt(17), end_dt=dt(22), vehicle_type_required="Chiller Truck", same_driver_key="G1"),
    Job(row_number=5, sr="5", start_dt=dt(23), end_dt=dt(0, d=16), vehicle_type_required="Chiller Truck", same_driver_key="G1"),
    Job(row_number=6, sr="6", start_dt=dt(23), end_dt=dt(0, d=16), vehicle_type_required="Chiller Truck", same_driver_key="G1"),  # simultaneous duplicate
]
jobs = [shared_early_job, generalist_bus_job] + exclusive_jobs

allocate(jobs, drivers, vehicles, [])

for j in jobs:
    print(f"SR{j.sr} {j.start_dt.strftime('%H:%M')}-{j.end_dt.strftime('%H:%M')} -> "
          f"driver_id={j.assigned_driver_id} unresolved={j.unresolved}")

assert shared_early_job.assigned_driver_id == GENERALIST.id, \
    "The early shared job should go to the generalist, reserving the specialist's hours for exclusive work"
assert generalist_bus_job.assigned_driver_id == GENERALIST.id
assert all(j.assigned_driver_id == SPECIALIST.id for j in exclusive_jobs), \
    "ALL of the specialist's exclusive-type demand should land on them -- none should be pushed to supplier/unresolved"
assert not any(j.unresolved for j in jobs)
print("PASS: shared work goes to the generalist, specialist's hours are correctly reserved for exclusive-type demand, zero unresolved")

print()
print("ALL SPECIALIST-RESERVATION TESTS PASSED")

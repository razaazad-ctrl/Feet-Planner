import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# Confirmed with the project owner 2026-08-03 (TB-001 in the scheduling
# rules spec): a planner-set end time already accounts for travel back to
# base -- 05:00-08:00 followed by 08:00-11:00 for the same driver is
# intentional, not a shorthand that still needs a buffer added on top.
# DEFAULT_TRAVEL_BUFFER_MINUTES is now 0: zero-gap back-to-back jobs for
# the same driver/vehicle are allowed, even across two completely
# UNRELATED orders with no "Same Driver" flag connecting them. Genuine
# time overlap is still a hard conflict.

# --- Two unrelated orders, zero gap, no group flag -- must both assign
# to the same driver, exactly like the real-world case found in a real
# PLANNED.xlsx (SR47/SR60 pattern). ---
job_a = Job(row_number=1, sr="1", start_dt=dt(5), end_dt=dt(8), vehicle_type_required="Bus")   # order A
job_b = Job(row_number=2, sr="2", start_dt=dt(8), end_dt=dt(11), vehicle_type_required="Bus")  # unrelated order B, zero gap
jobs = [job_a, job_b]
drivers = [DriverProfile(id=1, name="D1", license_types=["Bus"], working_hours_per_day=None)]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
assert job_a.assigned_driver_id == 1 and job_b.assigned_driver_id == 1, \
    "Zero-gap back-to-back jobs for the same driver must both be assignable, even without a Same Driver flag"
print("PASS: zero-gap back-to-back unrelated orders both assign to the same driver")

# --- Genuine overlap (not just adjacency) must still be a hard conflict ---
job_c = Job(row_number=1, sr="1", start_dt=dt(5), end_dt=dt(8), vehicle_type_required="Bus")
job_d = Job(row_number=2, sr="2", start_dt=dt(7, 59), end_dt=dt(11), vehicle_type_required="Bus")  # starts 1 min before job_c ends
jobs2 = [job_c, job_d]
drivers2 = [DriverProfile(id=1, name="D1", license_types=["Bus"], working_hours_per_day=None)]
allocate(jobs2, drivers2, vehicles, [])
assert job_c.assigned_driver_id != job_d.assigned_driver_id or job_d.unresolved, \
    "A genuine 1-minute overlap must still be treated as a real conflict, not silently allowed"
print("PASS: genuine time overlap (not just adjacency) is still correctly rejected")

print()
print("ALL TRAVEL BUFFER TESTS PASSED")

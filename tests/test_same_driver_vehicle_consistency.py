import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# Real bug found and fixed 2026-08-03: a real test run put one driver
# (AALIM HASSAN, real name) on a 10 Ton Chiller Truck 23:00-00:00 AND a
# 20 Seater Bus 23:00-01:00 SIMULTANEOUSLY, both rows sharing the same
# "Same Driver" flagged group. The group-overlap relaxation (meant only
# for genuine same-vehicle simultaneous orders, e.g. one truck doing two
# pickups at once) was firing for ANY two rows sharing a group tag,
# regardless of vehicle type -- physically impossible, one person can't
# drive two vehicles at once.

# --- Reproduce the exact bug shape: one group, two vehicle types, two
# rows at the exact same time. The two vehicle types must go to two
# DIFFERENT drivers, never the same one. ---
chiller_job = Job(row_number=1, sr="1", start_dt=dt(23), end_dt=dt(0, d=16),
                   vehicle_type_required="Chiller Truck", same_driver_key="EVENT-X")
bus_job = Job(row_number=2, sr="2", start_dt=dt(23), end_dt=dt(1, d=16),
              vehicle_type_required="Bus", same_driver_key="EVENT-X")
jobs = [chiller_job, bus_job]
drivers = [
    DriverProfile(id=1, name="D1", license_types=["Chiller Truck", "Bus"], working_hours_per_day=None),
    DriverProfile(id=2, name="D2", license_types=["Chiller Truck", "Bus"], working_hours_per_day=None),
]
vehicles = [VehicleProfile(id=1, plate="CHILLER-1", vehicle_type="Chiller Truck"),
            VehicleProfile(id=2, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])
print(f"Chiller job -> driver {chiller_job.assigned_driver_id}, Bus job -> driver {bus_job.assigned_driver_id}")
assert chiller_job.assigned_driver_id is not None and bus_job.assigned_driver_id is not None, \
    "Both rows should still be assignable (just not to the same driver)"
assert chiller_job.assigned_driver_id != bus_job.assigned_driver_id, \
    "A driver cannot be on a Chiller Truck and a Bus at the exact same time, even inside a 'Same Driver' flagged group"
print("PASS: simultaneous different-vehicle rows in one group correctly go to two different drivers")

# --- Confirm the legitimate case still works: genuine SAME vehicle type
# serving two simultaneous orders in one group must still be allowed on
# ONE driver (this is the whole point of the "Same Driver" feature). ---
job_a = Job(row_number=1, sr="1", start_dt=dt(4), end_dt=dt(7), vehicle_type_required="Bus", same_driver_key="EVENT-Y")
job_b = Job(row_number=2, sr="2", start_dt=dt(4), end_dt=dt(7), vehicle_type_required="Bus", same_driver_key="EVENT-Y")
jobs2 = [job_a, job_b]
drivers2 = [DriverProfile(id=1, name="D1", license_types=["Bus"], working_hours_per_day=None)]
vehicles2 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs2, drivers2, vehicles2, [])
assert job_a.assigned_driver_id == 1 and job_b.assigned_driver_id == 1, \
    "Two simultaneous SAME-vehicle-type orders in one group should still both go to the one driver"
print("PASS: genuine simultaneous same-vehicle-type orders still correctly assign to one driver")

# --- Confirm a driver CAN still pick up a different vehicle type within
# the same group at a NON-overlapping time (matches real ground-truth
# behavior -- e.g. a driver doing Bus jobs all day plus one later Chiller
# job for the same event, no time conflict). ---
job_c = Job(row_number=1, sr="1", start_dt=dt(13), end_dt=dt(15), vehicle_type_required="Bus", same_driver_key="EVENT-Z")
job_d = Job(row_number=2, sr="2", start_dt=dt(22), end_dt=dt(1, d=16), vehicle_type_required="Chiller Truck", same_driver_key="EVENT-Z")
jobs3 = [job_c, job_d]
drivers3 = [DriverProfile(id=1, name="D1", license_types=["Bus", "Chiller Truck"], working_hours_per_day=None)]
vehicles3 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus"), VehicleProfile(id=2, plate="CHILLER-1", vehicle_type="Chiller Truck")]
allocate(jobs3, drivers3, vehicles3, [])
assert job_c.assigned_driver_id == 1 and job_d.assigned_driver_id == 1, \
    "A driver switching vehicle type within a group at a non-overlapping time should still be allowed"
print("PASS: non-overlapping vehicle-type switch within a group is still allowed for the same driver")

print()
print("ALL SAME-DRIVER VEHICLE-CONSISTENCY TESTS PASSED")

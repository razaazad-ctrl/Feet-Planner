import sys
sys.path.insert(0, "/home/claude/Feet-Planner")

from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile, SupplierOffering
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

def fresh_jobs():
    return [
        # Group "G1": two IDENTICAL overlapping windows, same vehicle type.
        # Expectation: same driver, no conflict (overlap allowed within group).
        Job(row_number=1, sr="1", start_dt=dt(12), end_dt=dt(17),
            vehicle_type_required="Chiller Truck", same_driver_key="G1"),
        Job(row_number=2, sr="2", start_dt=dt(12), end_dt=dt(17),
            vehicle_type_required="Chiller Truck", same_driver_key="G1"),

        # Group "G2": one Chiller row, one Bus row, non-overlapping.
        # Only one driver (D1) is licensed for both; D2 only Chiller, D3 only Bus.
        # Expectation: since D1 qualifies for everything, D1 does the whole group
        # (fewest drivers), NOT split, even though a split would also be possible.
        Job(row_number=3, sr="3", start_dt=dt(8), end_dt=dt(10),
            vehicle_type_required="Chiller Truck", same_driver_key="G2"),
        Job(row_number=4, sr="4", start_dt=dt(11), end_dt=dt(12),
            vehicle_type_required="Bus", same_driver_key="G2"),

        # Group "G3": Chiller row then Bus row, but NO single driver is licensed
        # for both types this time (D4 Chiller-only, D5 Bus-only).
        # Expectation: forced split -- D4 does the Chiller row, D5 the Bus row.
        Job(row_number=5, sr="5", start_dt=dt(8), end_dt=dt(10),
            vehicle_type_required="Chiller Truck", same_driver_key="G3"),
        Job(row_number=6, sr="6", start_dt=dt(11), end_dt=dt(12),
            vehicle_type_required="Bus", same_driver_key="G3"),

        # Regression check: NOT flagged (no same_driver_key). Two overlapping
        # Chiller jobs. Expectation: normal conflict handling -- can't be the
        # same driver, since no group relaxation applies. Only D1/D4 qualify
        # for Chiller in this test, so this must split across two drivers or
        # go unresolved/supplier -- must NOT silently allow one driver to
        # double-book like group jobs do.
        Job(row_number=7, sr="7", start_dt=dt(9), end_dt=dt(14),
            vehicle_type_required="Chiller Truck", same_driver_key=""),
        Job(row_number=8, sr="8", start_dt=dt(9), end_dt=dt(14),
            vehicle_type_required="Chiller Truck", same_driver_key=""),
    ]

drivers = [
    DriverProfile(id=1, name="D1_BOTH", license_types=["Chiller Truck", "Bus"]),
    DriverProfile(id=2, name="D2_CHILLER_ONLY_A", license_types=["Chiller Truck"]),
    DriverProfile(id=3, name="D3_BUS_ONLY", license_types=["Bus"]),
    DriverProfile(id=4, name="D4_CHILLER_ONLY_B", license_types=["Chiller Truck"]),
    DriverProfile(id=5, name="D5_BUS_ONLY_B", license_types=["Bus"]),
    DriverProfile(id=6, name="D6_CHILLER_FREE", license_types=["Chiller Truck"]),
    DriverProfile(id=7, name="D7_CHILLER_FREE", license_types=["Chiller Truck"]),
]
vehicles = [
    VehicleProfile(id=1, plate="CHILLER-1", vehicle_type="Chiller Truck"),
    VehicleProfile(id=2, plate="CHILLER-2", vehicle_type="Chiller Truck"),
    VehicleProfile(id=3, plate="BUS-1", vehicle_type="Bus"),
    VehicleProfile(id=4, plate="BUS-2", vehicle_type="Bus"),
    VehicleProfile(id=5, plate="CHILLER-3", vehicle_type="Chiller Truck"),
    VehicleProfile(id=6, plate="CHILLER-4", vehicle_type="Chiller Truck"),
]
offerings = []  # no suppliers needed for this test

jobs = fresh_jobs()
allocate(jobs, drivers, vehicles, offerings)

by_sr = {j.sr: j for j in jobs}

print("=== Results ===")
for j in jobs:
    print(f"SR{j.sr} group={j.same_driver_key or '-':4s} {j.start_dt.strftime('%H:%M')}-{j.end_dt.strftime('%H:%M')} "
          f"-> {j.assignment_note}  veh={j.assigned_vehicle_plate}  unresolved={j.unresolved}")

print()
print("=== Assertions ===")

# G1: identical overlapping windows -> same driver, no conflict
assert by_sr["1"].assigned_driver_id == by_sr["2"].assigned_driver_id, "G1 should get the same driver despite overlap"
assert not by_sr["1"].unresolved and not by_sr["2"].unresolved
print("PASS: G1 overlap relaxation within flagged group works")

# G2: one driver can do both -> should NOT split, D1 should get both
assert by_sr["3"].assigned_driver_id == 1, f"expected D1 on SR3, got {by_sr['3'].assigned_driver_id}"
assert by_sr["4"].assigned_driver_id == 1, f"expected D1 on SR4, got {by_sr['4'].assigned_driver_id}"
print("PASS: G2 stays on a single qualified driver (fewest drivers)")

# G3: no single driver licensed for both -> forced split across 2 drivers
assert by_sr["5"].assigned_driver_id != by_sr["6"].assigned_driver_id, "G3 must split since no driver covers both types"
assert by_sr["5"].assigned_driver_id in (2, 4)
assert by_sr["6"].assigned_driver_id in (3, 5)
print(f"PASS: G3 forced split by vehicle-type licensing (SR5->driver {by_sr['5'].assigned_driver_id}, SR6->driver {by_sr['6'].assigned_driver_id})")

# Regression: unflagged overlapping jobs must be split across two DIFFERENT
# drivers (D6/D7 are free and available specifically to prove this is a real
# split, not just "both unresolved").
assert by_sr["7"].assigned_driver_id is not None and by_sr["8"].assigned_driver_id is not None, \
    "Regression rows should have been assignable (free drivers available)"
assert by_sr["7"].assigned_driver_id != by_sr["8"].assigned_driver_id, \
    "Unflagged overlapping jobs must never be double-booked onto the same driver (regression check failed!)"
print("PASS: regression check -- unflagged overlaps correctly split across two different drivers")

print()
print("ALL TESTS PASSED")

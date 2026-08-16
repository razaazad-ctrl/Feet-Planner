import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, allocate_by_solver, SupplierOffering
from app.excel_import import Job


def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)


# Phase 29d regression: hiring the SAME supplier for TWO DIFFERENT vehicle
# types on the same day used to give both hires the identical, unnumbered
# label -- instance_number was computed from a (supplier_id, vehicle_type)
# -scoped registry instead of a supplier-wide one, so each hire looked like
# the "1st" unit within its own type bucket. Two genuinely different
# physical units (different vehicle_type, not reusable for each other's
# jobs) displayed as if they were the same one -- caught in real use as a
# ReCheck "vehicle clash" false alarm that turned out to be a real engine
# label collision. No in-house drivers/vehicles at all, forcing both jobs
# to the supplier pass.
def make_jobs():
    return [
        Job(row_number=1, sr="19", start_dt=dt(9), end_dt=dt(12), vehicle_type_required="10 Ton Dry Truck"),
        Job(row_number=2, sr="15", start_dt=dt(9), end_dt=dt(18), vehicle_type_required="5 Ton Open Truck"),
    ]


offerings = [
    SupplierOffering(supplier_id=1, supplier_name="NEW HEIGHTS HEAVY TRUCK", vehicle_type="10 Ton Dry Truck",
                      rate_per_hour=100, max_available_per_day=5),
    SupplierOffering(supplier_id=1, supplier_name="NEW HEIGHTS HEAVY TRUCK", vehicle_type="5 Ton Open Truck",
                      rate_per_hour=80, max_available_per_day=5),
]

jobs = make_jobs()
allocate(jobs, [], [], offerings)
for j in jobs:
    print(f"SR{j.sr} ({j.vehicle_type_required}) -> unit={j.assigned_supplier_unit}")
labels = [j.assigned_supplier_unit for j in jobs]
assert all(labels), "both rows should have gotten a supplier unit"
assert labels[0] != labels[1], (
    f"two different physical hires (different vehicle types) must get DISTINCT labels, got {labels}"
)
print("PASS (allocate): same supplier, two different vehicle types -> distinct unit labels")

jobs2 = make_jobs()
allocate_by_solver(jobs2, [], [], offerings)
for j in jobs2:
    print(f"SR{j.sr} ({j.vehicle_type_required}) -> unit={j.assigned_supplier_unit}")
labels2 = [j.assigned_supplier_unit for j in jobs2]
assert all(labels2), "both rows should have gotten a supplier unit"
assert labels2[0] != labels2[1], (
    f"two different physical hires (different vehicle types) must get DISTINCT labels, got {labels2}"
)
print("PASS (allocate_by_solver): same supplier, two different vehicle types -> distinct unit labels")

print()
print("ALL SUPPLIER-HIRE-NUMBERING TESTS PASSED")

import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile, SupplierOffering
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# No in-house drivers/vehicles at all -> forces supplier pass for a flagged group.
jobs = [
    Job(row_number=1, sr="1", start_dt=dt(8), end_dt=dt(10),
        vehicle_type_required="Bus", same_driver_key="GS"),
    Job(row_number=2, sr="2", start_dt=dt(11), end_dt=dt(12),
        vehicle_type_required="Bus", same_driver_key="GS"),
    Job(row_number=3, sr="3", start_dt=dt(9), end_dt=dt(11),
        vehicle_type_required="Bus", same_driver_key="GS"),  # overlaps row 1 -- should still reuse same hire
]
drivers = []
vehicles = []
offerings = [
    SupplierOffering(supplier_id=1, supplier_name="AL LAITH TRANSPORT", vehicle_type="Bus",
                      rate_per_hour=50, max_available_per_day=5),
]

allocate(jobs, drivers, vehicles, offerings)
for j in jobs:
    print(f"SR{j.sr} {j.start_dt.strftime('%H:%M')}-{j.end_dt.strftime('%H:%M')} -> "
          f"{j.assignment_note} unit={j.assigned_supplier_unit} unresolved={j.unresolved}")

units = {j.assigned_supplier_unit for j in jobs}
# All three should resolve to ONE supplier unit label family (first is bare name,
# later reuses say "SAME <name>"), i.e. only 1 unit hired despite the overlap.
assert all(j.assigned_supplier_unit for j in jobs), "all rows should have gotten a supplier unit"
base_names = {u.replace("SAME ", "") for u in units}
assert len(base_names) == 1, f"expected exactly one supplier unit reused for the whole group, got {units}"
print("PASS: supplier reuse-within-group + overlap relaxation works (only one unit hired)")

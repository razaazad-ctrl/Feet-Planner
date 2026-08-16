import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import DriverProfile
from app.excel_import import Job
from app.ui.plan_day_tab import _compute_recheck_issues


def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)


def in_house_job(sr, start_h, end_h, driver_id, driver_name, vehicle_id=None, plate="",
                  vehicle_type_required="Bus", same_driver_key=""):
    j = Job(row_number=int(sr), sr=sr, start_dt=dt(start_h), end_dt=dt(end_h),
            vehicle_type_required=vehicle_type_required, same_driver_key=same_driver_key)
    j.assigned_driver_id = driver_id
    j.assigned_driver_name = driver_name
    j.assigned_vehicle_id = vehicle_id
    j.assigned_vehicle_plate = plate
    j.unresolved = False
    return j


# 1. Driver double-booking is flagged.
overlap_a = in_house_job("1", 8, 12, driver_id=1, driver_name="AHMED")
overlap_b = in_house_job("2", 10, 14, driver_id=1, driver_name="AHMED")
issues = _compute_recheck_issues([overlap_a, overlap_b], [], {})
assert issues.get("1") and issues.get("2"), "Overlapping trips for the same driver must both be flagged"
assert "Clash" in issues["1"][0]
print("PASS: driver double-booking flagged")

# 2. Same-Driver-grouped overlap is NOT flagged (legitimate back-and-forth rows).
grouped_a = in_house_job("1", 8, 12, driver_id=1, driver_name="AHMED", same_driver_key="EVENT-9")
grouped_b = in_house_job("2", 8, 12, driver_id=1, driver_name="AHMED", same_driver_key="EVENT-9")
issues = _compute_recheck_issues([grouped_a, grouped_b], [], {})
assert not issues, "Same-Driver-grouped overlapping rows must NOT be flagged as a clash"
print("PASS: Same-Driver-group overlap correctly exempted")

# 3. Vehicle double-booking is flagged.
veh_a = in_house_job("1", 8, 12, driver_id=1, driver_name="AHMED", vehicle_id=5, plate="A 111")
veh_b = in_house_job("2", 10, 14, driver_id=2, driver_name="RAZA", vehicle_id=5, plate="A 111")
issues = _compute_recheck_issues([veh_a, veh_b], [], {})
assert issues.get("1") and issues.get("2"), "Overlapping trips on the same vehicle must both be flagged"
assert "vehicle" in issues["1"][0].lower()
print("PASS: vehicle double-booking flagged")

# 4. Daily ceiling breach is flagged.
long_job = in_house_job("1", 6, 20, driver_id=1, driver_name="AHMED")  # 14h span
drivers = [DriverProfile(id=1, name="AHMED", working_hours_per_day=9.0, max_working_hours_per_day=12.0)]
issues = _compute_recheck_issues([long_job], drivers, {})
assert issues.get("1"), "A shift spanning past max_working_hours_per_day must be flagged"
assert "daily limit" in issues["1"][0]
print("PASS: daily ceiling breach flagged")

# 5. Monthly overtime breach is flagged even when the daily ceiling itself is fine.
mild_ot_job = in_house_job("1", 6, 17, driver_id=1, driver_name="AHMED")  # 11h span, 2h overtime today
drivers = [DriverProfile(id=1, name="AHMED", working_hours_per_day=9.0, max_working_hours_per_day=12.0,
                          max_overtime_hours_per_month=1.0, month_overtime_so_far=0.0)]
issues = _compute_recheck_issues([mild_ot_job], drivers, {})
assert issues.get("1"), "2h of today's overtime against a 1h/month cap must be flagged"
assert "monthly" in issues["1"][0].lower()
print("PASS: monthly overtime breach flagged")

# 6. A Same-Driver group legitimately split across two DIFFERENT drivers
# (e.g. one driver couldn't cover the whole group -- see
# AI/04_BUSINESS_RULES.md's "Same Driver Column" section: this is a
# documented SOFT preference the engine honors "if possible," not a hard
# rule) must NOT be flagged. An earlier version of this function treated
# any split as a violation -- caught in real use when a fresh, optimal,
# 0-unresolved Run Planning result (which legitimately splits large
# groups when one driver can't cover the whole thing) got 47 false-positive
# "Same Driver rule" flags from ReCheck before the planner had touched
# anything. This check guards against that regression.
sd_a = in_house_job("1", 8, 10, driver_id=1, driver_name="AHMED", same_driver_key="EVENT-9")
sd_b = in_house_job("2", 11, 13, driver_id=2, driver_name="RAZA", same_driver_key="EVENT-9")
issues = _compute_recheck_issues([sd_a, sd_b], [], {})
assert not issues, f"A Same-Driver group split across different drivers must NOT be flagged, got: {issues}"
print("PASS: Same-Driver group legitimately split across drivers is correctly NOT flagged")

# 7. Wrong vehicle type is flagged.
wrong_type_job = in_house_job("1", 8, 10, driver_id=1, driver_name="AHMED", vehicle_id=7, plate="A 222",
                               vehicle_type_required="23 Seater Bus")
issues = _compute_recheck_issues([wrong_type_job], [], {7: "Sedan"})
assert issues.get("1"), "An assigned vehicle whose type doesn't match the job's requirement must be flagged"
assert "Wrong vehicle type" in issues["1"][0]
print("PASS: wrong vehicle type flagged")

# 8. A fully clean plan produces zero issues.
clean_a = in_house_job("1", 8, 10, driver_id=1, driver_name="AHMED", vehicle_id=1, plate="A 1",
                        vehicle_type_required="Bus")
clean_b = in_house_job("2", 11, 13, driver_id=2, driver_name="RAZA", vehicle_id=2, plate="A 2",
                        vehicle_type_required="Bus")
drivers = [
    DriverProfile(id=1, name="AHMED", working_hours_per_day=9.0, max_working_hours_per_day=12.0),
    DriverProfile(id=2, name="RAZA", working_hours_per_day=9.0, max_working_hours_per_day=12.0),
]
issues = _compute_recheck_issues([clean_a, clean_b], drivers, {1: "Bus", 2: "Bus"})
assert not issues, f"A fully legal plan must produce zero issues, got: {issues}"
print("PASS: clean plan produces zero issues")

print()
print("ALL RECHECK TESTS PASSED")

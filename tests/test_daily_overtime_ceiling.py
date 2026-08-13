import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from datetime import datetime
from app.allocation_engine import allocate, DriverProfile, VehicleProfile
from app.excel_import import Job

def dt(h, m=0, d=15):
    return datetime(2026, 2, d, h, m)

# HR-002 rework: the daily ceiling is no longer a hardcoded constant --
# it's DriverProfile.max_working_hours_per_day, a real per-driver field the
# planner sets (paired with working_hours_per_day, e.g. 9/12). Reproduce the
# exact reported symptom: a driver with plenty of unused monthly overtime
# should NOT be able to get a ~22h single day (7 AM to 5 AM next day), even
# though that's well under a 60h/month cap.
jobs = [
    Job(row_number=1, sr="1", start_dt=dt(7, d=15), end_dt=dt(23, 59, d=15), vehicle_type_required="Bus"),
    Job(row_number=2, sr="2", start_dt=dt(0, 5, d=16), end_dt=dt(5, 0, d=16), vehicle_type_required="Bus"),
]
drivers = [DriverProfile(id=1, name="AMPLE_MONTHLY_BUDGET_DRIVER", license_types=["Bus"],
                          working_hours_per_day=9.0, max_working_hours_per_day=12.0,
                          max_overtime_hours_per_month=60.0, month_overtime_so_far=0.0)]
vehicles = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs, drivers, vehicles, [])

total_hours_today = sum((j.end_dt - j.start_dt).total_seconds()/3600 for j in jobs if j.assigned_driver_id == 1)
print(f"Hours actually given to this driver on day 1: {total_hours_today}")
assert total_hours_today <= 12.0 + 1e-6, \
    f"Driver was given {total_hours_today}h in one day -- daily ceiling (max_working_hours_per_day=12h) not enforced!"
print("PASS: driver capped at 12h/day (max_working_hours_per_day) even with 60h/month unused")

# Confirm a driver CAN still use overtime up to the daily ceiling, just not beyond
jobs2 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(18), vehicle_type_required="Bus")]  # 12h exactly
drivers2 = [DriverProfile(id=1, name="EXACT_DAILY_CEILING_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_working_hours_per_day=12.0,
                           max_overtime_hours_per_month=60.0)]
vehicles2 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs2, drivers2, vehicles2, [])
assert jobs2[0].assigned_driver_id == 1, "A 12h day (exactly at max_working_hours_per_day) should still be assignable"
print("PASS: exactly-12h day still assignable (no off-by-one over-restriction)")

# Confirm going 1 minute past the ceiling is rejected
jobs3 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(18, 1), vehicle_type_required="Bus")]  # 12h01m
drivers3 = [DriverProfile(id=1, name="OVER_DAILY_CEILING_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_working_hours_per_day=12.0,
                           max_overtime_hours_per_month=60.0)]
vehicles3 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs3, drivers3, vehicles3, [])
assert jobs3[0].assigned_driver_id is None, "12h01m should be rejected -- 1 minute past the daily ceiling"
print("PASS: 1 minute past the daily ceiling is correctly rejected")

# Confirm the fail-closed default: if max_working_hours_per_day is left
# blank, the ceiling falls back to working_hours_per_day (zero daily
# overtime allowed) rather than reopening the old unlimited-day bug.
jobs4 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(16), vehicle_type_required="Bus")]  # 10h
drivers4 = [DriverProfile(id=1, name="NO_DAILY_CEILING_SET_DRIVER", license_types=["Bus"],
                           working_hours_per_day=9.0, max_working_hours_per_day=None,
                           max_overtime_hours_per_month=60.0)]
vehicles4 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs4, drivers4, vehicles4, [])
assert jobs4[0].assigned_driver_id is None, \
    "With max_working_hours_per_day left blank, a 10h job (1h of overtime) must be refused -- ceiling defaults to working_hours_per_day (9h), not unlimited"
print("PASS: blank max_working_hours_per_day defaults to zero daily overtime (fail-closed)")

# --- Daily MINIMUM hours (new HR-002 requirement): a driver used at all
# that day must reach at least working_hours_per_day. With only one
# driver available and no way to move the short day to anyone else, an
# under-minimum day must be released entirely rather than kept.
jobs5 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(11), vehicle_type_required="Bus")]  # 5h -- under the 9h minimum
drivers5 = [DriverProfile(id=1, name="ONLY_DRIVER_SHORT_DAY", license_types=["Bus"],
                           working_hours_per_day=9.0, max_working_hours_per_day=12.0)]
vehicles5 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs5, drivers5, vehicles5, [])
assert jobs5[0].assigned_driver_id is None and jobs5[0].unresolved, \
    "A 5h day is below the 9h minimum, and with no other driver to move it to, it must be released as unresolved, not kept"
print("PASS: an under-minimum day with no other driver available is released as unresolved")

# Even with a second qualifying, idle driver available, a lone 5h job is
# still below EITHER driver's 9h minimum -- moving it just relocates the
# same problem, so it should still end up unresolved rather than silently
# accepted on driver 2.
jobs6 = [Job(row_number=1, sr="1", start_dt=dt(6), end_dt=dt(11), vehicle_type_required="Bus")]  # 5h
drivers6 = [
    DriverProfile(id=1, name="ORIGINAL_SHORT_DAY_DRIVER", license_types=["Bus"],
                  working_hours_per_day=9.0, max_working_hours_per_day=12.0),
    DriverProfile(id=2, name="STANDBY_DRIVER", license_types=["Bus"],
                  working_hours_per_day=9.0, max_working_hours_per_day=12.0),
]
vehicles6 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus")]
allocate(jobs6, drivers6, vehicles6, [])
print(f"Under-minimum day resolution: assigned_driver_id={jobs6[0].assigned_driver_id}, unresolved={jobs6[0].unresolved}")
assert jobs6[0].assigned_driver_id is None and jobs6[0].unresolved, \
    "A lone 5h job is still below the 9h minimum for whichever driver ends up with it -- moving it doesn't fix that"
print("PASS: repair pass correctly recognises a move alone can't fix a day that's short on total volume")

# --- Repair pass SUCCESS path: driver A gets a short day, but driver B is
# already independently working most of a day and has room under B's own
# ceiling (with enough monthly overtime allowance) to absorb A's short job
# too -- the repair pass should move A's job onto B, leaving A completely
# free (0h, legal) and B at a legal, if longer, day.
# Job times chosen so the combined SPAN (not just summed duration) stays
# legal under the 2026-08-10 duty-span correction: driver A's job ends
# right when driver B's begins (08:00-11:00 then 11:00-20:00), so
# consolidating gives a 12h span (08:00-20:00), exactly at the ceiling --
# not the 14h a 2-hour gap between the two jobs would have produced.
jobs7 = [
    Job(row_number=1, sr="1", start_dt=dt(8), end_dt=dt(11), vehicle_type_required="Bus"),   # driver A's only job: 3h (short)
    Job(row_number=2, sr="2", start_dt=dt(11), end_dt=dt(20), vehicle_type_required="Bus"),  # driver B: 9h on its own (meets minimum)
]
drivers7 = [
    DriverProfile(id=1, name="DRIVER_A_SHORT", license_types=["Bus"],
                  working_hours_per_day=9.0, max_working_hours_per_day=12.0, max_overtime_hours_per_month=60.0),
    DriverProfile(id=2, name="DRIVER_B_ROOM", license_types=["Bus"],
                  working_hours_per_day=9.0, max_working_hours_per_day=12.0, max_overtime_hours_per_month=60.0),
]
vehicles7 = [VehicleProfile(id=1, plate="BUS-1", vehicle_type="Bus"),
             VehicleProfile(id=2, plate="BUS-2", vehicle_type="Bus")]
allocate(jobs7, drivers7, vehicles7, [])
for j in jobs7:
    print(f"SR{j.sr} -> assigned_driver_id={j.assigned_driver_id} unresolved={j.unresolved} note={j.assignment_note}")
assert jobs7[0].assigned_driver_id == jobs7[1].assigned_driver_id, \
    "Both jobs should end up on the same driver -- the short 3h job should be moved onto whichever driver already has the 9h job, making a legal 12h day"
assert not jobs7[0].unresolved and not jobs7[1].unresolved
print("PASS: repair pass successfully consolidates a short day onto a driver with spare room, instead of leaving it unresolved")

print()
print("ALL DAILY OVERTIME CEILING TESTS PASSED")

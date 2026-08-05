"""
allocation_engine.py

The deterministic core of the planning logic -- no AI involved here.

- Driver hard rules (working hours/day, off days, license types, monthly
  overtime cap) come from STRUCTURED fields, not free-text line parsing --
  free text can silently fail to match and go unenforced, which is
  exactly what caused a driver to be over-scheduled in earlier testing.
- Overtime is monthly, not a hard daily wall: a driver can work beyond
  their normal daily hours as long as their running MONTHLY overtime
  total (pulled from finalized-day history, see db.finalized_jobs) stays
  under their configured monthly cap. No cap configured = unlimited
  overtime for that driver.
- In-house is always tried first. Suppliers are used only when no
  compliant in-house driver+vehicle pair exists.
- Supplier hiring is DYNAMIC: the planner only configures rate/type/daily
  availability per supplier -- the app decides at planning time how many
  distinct units to hire and names them itself:
    1st hire of the day  -> "SUPPLIER NAME"
    2nd hire of the day  -> "SUPPLIER NAME 1"
    Nth hire of the day  -> "SUPPLIER NAME {N-1}"
    reusing an existing hire for a later job -> "SAME <label>"
  Priority is to REUSE an already-hired unit before hiring a new one
  (minimize headcount for the day) even if that means one supplier
  driver works more hours -- only supplier daily-availability limits and
  timing conflicts force a new hire.
- Cross-day supplier fairness (equal business opportunity over time) is
  a secondary tiebreak: among several suppliers who could all take a
  brand-new hire, the one with the least cumulative historical hours
  (db.get_supplier_cumulative_hours) is preferred.

Event-chain reasoning ("should this driver wait on-site for the next
stage of the same event, or come back and get reassigned") is NOT done
here -- that's a judgment call that belongs to the AI layer on top of
this, informed by real travel-time data.

--------------------------------------------------------------------------
"Same Driver" groups (deterministic, planner-flagged -- not AI)
--------------------------------------------------------------------------
The planner can paste the same text into the "Same Driver" column on
several rows to flag "one driver should do all of these, back and forth,
if at all possible." This is enforced here, in the deterministic engine,
because it is an explicit planner instruction, not a judgment call --
Rule 3 (planner has final authority) and Rule 9 (AI assists, never
controls the deterministic allocation) both apply.

Confirmed behaviour (agreed with the project owner before implementing):
1. Within one flagged group, overlapping times for the SAME driver are
   allowed and are not treated as a double-booking conflict. In practice
   this covers cases like a truck parked on-site for a long window while
   the same driver also has a nested/overlapping row for the same event --
   the planner has already judged this is one person's job, not two
   people's. This relaxation ONLY applies between rows sharing the same
   "Same Driver" value; overlap against any job outside that group is
   still a hard conflict as normal.
2. When one driver cannot cover the whole flagged group (their hours run
   out, or a later row needs a vehicle type they're not licensed for),
   the engine does NOT hard-code a time-based or vehicle-type-based split
   rule. Instead it always tries to REUSE the driver(s) already assigned
   to that group first (mirroring the existing supplier reuse-before-hire
   pattern), and only brings in an additional driver when none of the
   group's current driver(s) still qualify for the next row. In practice
   this naturally produces a vehicle-type split (a driver only licensed
   for Chiller Trucks keeps every Chiller row in the group; Bus rows fall
   to whichever driver is licensed for those) or a time/hours split
   (once a driver's daily/monthly hours are exhausted, a fresh driver
   picks up the rest) -- whichever the real constraint actually is for
   that group, without guessing.
3. The same reuse-before-hire treatment and overlap relaxation is applied
   to supplier hires for flagged rows that fall through to the supplier
   pass, for the same "fewest units possible" reasoning.
4. Hour totals (`occupied_seconds`, monthly-overtime projection) use the
   TRUE UNION of a driver's time intervals, not a naive sum -- two rows in
   the same flagged group that overlap in time (e.g. two simultaneous
   pickups on one truck) count once, not once each. This was originally a
   documented simplification (summing, and deliberately erring toward
   overstating hours per Rule 6) but real PLANNED.xlsx data confirmed the
   overstatement was large enough to falsely trip the daily ceiling in
   routine cases, not just edge cases -- fixed 2026-08-03. See
   `_merged_hours()` below and HR-002 in the scheduling rules spec.
"""
import re
from dataclasses import dataclass, field
from datetime import timedelta, date, datetime, time

# Confirmed with the project owner, 2026-08-03 (see TB-001 in the
# scheduling rules spec): a planner-set end time already accounts for
# travel back to base -- e.g. a 05:00-08:00 job followed by an 08:00-11:00
# job for the same driver is intentional; 08:00 IS the return-to-base
# time, chosen by the planner, not a shorthand that still needs a buffer
# added on top. A non-zero buffer here was double-counting travel time the
# planner had already built into their chosen times, and was blocking real
# back-to-back continuations between UNRELATED orders (not just "Same
# Driver"-flagged ones) that the planner clearly intended. Set to 0:
# adjacent (touching, zero-gap) jobs for the same driver/vehicle are not a
# conflict; genuine time overlap still is. FUTURE WORK, not built yet:
# once live travel-time lookups (Google Maps API) are wired in, gaps
# between jobs at genuinely different locations should be checked against
# real drive time instead of trusting the planner's manual timing --
# that's a distance-aware replacement for this flat constant, not a
# reason to raise it back to a flat non-zero value now.
DEFAULT_TRAVEL_BUFFER_MINUTES = 0

# --- HR-002 rework --------------------------------------------------------
# The daily ceiling used to be a single hardcoded constant
# (MAX_OVERTIME_HOURS_PER_DAY = 2.0) with no UI field to change it. It is
# now DriverProfile.max_working_hours_per_day -- a real, per-driver,
# planner-configurable field (paired with working_hours_per_day, e.g. 9/12)
# -- see _daily_ceiling_for() below. The reasoning for having a hard daily
# ceiling at all is unchanged: the monthly overtime bucket alone is not
# enough, since without a same-day cap a driver with plenty of unused
# monthly overtime could still be given one absurd single day (e.g. 7 AM to
# 5 AM the next day, ~22h). If a driver has no max_working_hours_per_day
# configured, the ceiling falls back to their working_hours_per_day (i.e.
# zero overtime allowed that day) -- fail-closed, matching the existing
# precedent for max_overtime_hours_per_month=None elsewhere in this file,
# rather than silently reopening the old unlimited-single-day bug.
#
# Also new in this rework: a MINIMUM daily-hours rule -- a driver who is
# used at all on a given day must reach at least working_hours_per_day that
# day. This can't be enforced as a simple per-job filter the way the
# ceiling is, because the engine assigns jobs one at a time in time order
# and doesn't know a driver's full-day total until the day's last job has
# been considered. It's enforced instead as a repair pass that runs after
# the normal allocation -- see _repair_minimum_daily_hours() below.


@dataclass
class DriverProfile:
    id: int
    name: str
    working_hours_per_day: float = None      # None = no daily baseline known
    shift_period: str = None                 # 'morning' | 'evening' | None = no restriction (see HR-002 rework)
    max_working_hours_per_day: float = None  # hard daily ceiling; None = falls back to working_hours_per_day
    license_types: list = field(default_factory=list)
    off_days: list = field(default_factory=list)          # lowercase weekday names
    max_overtime_hours_per_month: float = None            # None = unlimited overtime
    total_hours_per_month_target: float = None            # informational only
    month_overtime_so_far: float = 0.0                    # from history, excludes today

    # runtime state, reset per planning run
    occupied_seconds: float = 0.0
    busy_intervals: list = field(default_factory=list)


@dataclass
class VehicleProfile:
    id: int
    plate: str
    vehicle_type: str
    in_workshop: bool = False
    busy_intervals: list = field(default_factory=list)


@dataclass
class SupplierOffering:
    supplier_id: int
    supplier_name: str
    vehicle_type: str
    rate_per_hour: float
    max_available_per_day: int
    cumulative_hours_history: float = 0.0  # for cross-day fairness tiebreaking


@dataclass
class SupplierHire:
    """One dynamically-created hired unit for the day."""
    supplier_id: int
    supplier_name: str
    vehicle_type: str
    instance_number: int  # 1-based; 1st hire displays with no number
    busy_intervals: list = field(default_factory=list)
    already_used: bool = False  # becomes True after its first job -> later jobs say "SAME ..."

    @property
    def label(self):
        if self.instance_number == 1:
            return self.supplier_name
        return f"{self.supplier_name} {self.instance_number - 1}"


def build_driver_profiles(conn, db):
    """Reads drivers + their structured hard-rule fields. Excluded drivers
    are skipped entirely. Month-to-date overtime is pulled from finalized
    history so the monthly cap can be enforced correctly from day one."""
    profiles = []
    today = date.today()
    for row in db.list_drivers(conn):
        if row["excluded_from_planning"]:
            continue
        working_hours = row["working_hours_per_day"]
        month_overtime = 0.0
        if working_hours and hasattr(db, "get_driver_month_overtime_hours"):
            month_overtime = db.get_driver_month_overtime_hours(
                conn, row["id"], today.year, today.month, working_hours
            )
        profiles.append(DriverProfile(
            id=row["id"],
            name=row["name"],
            working_hours_per_day=working_hours,
            shift_period=row["shift_period"],
            max_working_hours_per_day=row["max_working_hours_per_day"],
            license_types=[t.strip() for t in (row["license_types"] or "").split(",") if t.strip()],
            off_days=[d.strip().lower() for d in (row["off_days"] or "").split(",") if d.strip()],
            max_overtime_hours_per_month=row["max_overtime_hours_per_month"],
            total_hours_per_month_target=row["total_hours_per_month_target"],
            month_overtime_so_far=month_overtime,
        ))
    return profiles


def build_vehicle_profiles(conn, db):
    return [
        VehicleProfile(id=row["id"], plate=row["plate"], vehicle_type=row["vehicle_type"],
                        in_workshop=bool(row["in_workshop"]) or bool(row["excluded_from_planning"]))
        for row in db.list_vehicles(conn)
    ]


def build_supplier_offerings(conn, db):
    offerings = []
    for row in db.list_all_supplier_offerings(conn):
        cumulative = db.get_supplier_cumulative_hours(conn, row["supplier_id"])
        offerings.append(SupplierOffering(
            supplier_id=row["supplier_id"],
            supplier_name=row["supplier_name"],
            vehicle_type=row["vehicle_type"],
            rate_per_hour=row["rate_per_hour"],
            max_available_per_day=row["max_available_per_day"],
            cumulative_hours_history=cumulative,
        ))
    return offerings


def _weekday_name(dt):
    return dt.strftime("%A").lower()


def _overlaps_with_buffer(existing_intervals, start_dt, end_dt, buffer_minutes, ignore_group_key=None):
    """Each interval is (start, end, group_key). group_key is None for jobs
    with no "Same Driver" flag. When ignore_group_key is set, intervals
    tagged with that same group_key are skipped -- i.e. overlap between two
    rows the planner explicitly flagged as "same driver, back and forth" is
    not a conflict. Overlap against anything else (a different group, or no
    group at all) is still checked normally."""
    buffer = timedelta(minutes=buffer_minutes)
    for (s, e, g) in existing_intervals:
        if ignore_group_key is not None and g == ignore_group_key:
            continue
        if start_dt < e + buffer and s < end_dt + buffer:
            return True
    return False


def _group_key_of(job):
    key = (getattr(job, "same_driver_key", "") or "").strip()
    return key or None


def _type_matches(required_type, candidate_type):
    return required_type.strip().lower() == candidate_type.strip().lower()


def _vehicle_type_needs_vehicle(vehicle_type_required):
    """NEW-004 fix (2026-08-03): a 'Driver Only' row needs a qualified
    driver but no physical vehicle at all -- confirmed by a real
    PLANNED.xlsx row using this exact type text. Case-insensitive."""
    return (vehicle_type_required or "").strip().lower() != "driver only"


def _driver_qualifies_for_type(driver, vehicle_type):
    if not driver.license_types:
        # No license types configured -> treated as qualified for nothing,
        # forcing the planner to explicitly set this rather than silently
        # allowing an unconfigured driver onto any job.
        return False
    return any(_type_matches(vehicle_type, t) for t in driver.license_types)


def _driver_is_off(driver, job_date, allow_override_days):
    weekday = job_date.strftime("%A").lower()
    if weekday in driver.off_days and job_date not in allow_override_days.get(driver.id, set()):
        return True
    return False


# --- HR-002 rework: morning/evening shift window --------------------------
# The planner never fixes an exact shift-start clock time up front anymore.
# They just mark a driver "morning" or "evening" (a simple, planner-picked
# label -- not something the software computes or rotates automatically).
# The actual first-job time for a given day is whatever the plan produces,
# and is only known -- and announced to the driver -- after planning.
# Evening starts at noon; adjust here if the business ever needs a
# different cutoff (kept as one constant rather than a UI field for now,
# since the planner only asked for a simple morning/evening split).
SHIFT_PERIOD_EVENING_CUTOFF_HOUR = 12


def _job_matches_shift_period(job_start_dt, shift_period):
    """True if this job is allowed for a driver with the given shift_period.
    shift_period is 'morning', 'evening', or None (no restriction -- same
    fail-open behaviour as every other optional hard-rule field here)."""
    if shift_period is None:
        return True
    is_evening_job = job_start_dt.time() >= time(SHIFT_PERIOD_EVENING_CUTOFF_HOUR, 0)
    if shift_period == "evening":
        return is_evening_job
    if shift_period == "morning":
        return not is_evening_job
    return True  # unrecognized value -- fail open rather than block everything


def _fill_gaps_with_unresolved_jobs(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days):
    """OPT-001/002/003 fix (2026-08-03), confirmed with the project owner
    against a real UNPLANNED.xlsx run: a driver ended up with two jobs
    12h apart (13:00-15:00 and 22:00-01:00, only 5h actual work) while a
    job that would have fit neatly into that 7h hole (16:00-20:00) was
    left completely unassigned. Root cause: the main pass has no way to
    know about a gap until AFTER both of its bounding jobs are assigned --
    jobs are processed strictly in start-time order, so the later bounding
    job is never assigned yet when an earlier job that would sit inside
    that gap is being considered (see the NOTE at chosen_driver's
    selection in allocate(), above).

    This runs once, after the main pass (and after suppliers), and looks
    at every still-genuinely-unresolved job: if some driver's day now has
    a bounded gap (an existing job before AND after, with the normal
    travel buffer) that this job fits into -- respecting every other hard
    rule (license, off-day, shift, overlap, daily ceiling, monthly
    overtime) -- it's assigned there instead of staying unresolved.

    Deliberately conservative: only touches jobs that are still fully
    unresolved (no driver AND no supplier), never reclaims a job already
    given to a supplier back to in-house -- unwinding a SupplierHire
    safely (renumbering, freeing capacity) is a separate, riskier piece of
    work not attempted here. Skips "Same Driver" grouped jobs, left to
    that mechanism instead. Runs once, not looped: filling one gap only
    ever removes room for other jobs, never creates new room.
    """
    allow_override_days = allow_override_days or {}
    filled_any = False
    for job in jobs:
        if not job.unresolved or job.assigned_driver_id is not None or job.assigned_supplier_unit is not None:
            continue
        if job.start_dt is None or job.end_dt is None:
            continue
        if _group_key_of(job):
            continue
        job_hours = (job.end_dt - job.start_dt).total_seconds() / 3600.0
        best_driver = None
        for d in driver_pool:
            if not _driver_qualifies_for_type(d, job.vehicle_type_required):
                continue
            if _driver_is_off(d, job.start_dt.date(), allow_override_days):
                continue
            if not _job_matches_shift_period(job.start_dt, d.shift_period):
                continue
            if not _driver_has_bounded_gap_fit(d, job.start_dt, job.end_dt, travel_buffer_minutes):
                continue  # only interested in a genuine gap-fill here
            if _overlaps_with_buffer(d.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes):
                continue
            if d.working_hours_per_day is not None:
                projected = _merged_hours(d.busy_intervals + [(job.start_dt, job.end_dt)])
                ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                if projected > ceiling + 1e-9:
                    continue
                overtime = max(0.0, projected - d.working_hours_per_day)
                if d.max_overtime_hours_per_month is not None:
                    if d.month_overtime_so_far + overtime > d.max_overtime_hours_per_month + 1e-9:
                        continue
                elif overtime > 0:
                    continue
            if best_driver is None or d.occupied_seconds < best_driver.occupied_seconds:
                best_driver = d
        if best_driver is None:
            continue
        new_vehicle = None
        if _vehicle_type_needs_vehicle(job.vehicle_type_required):
            for v in vehicle_pool:
                if _type_matches(job.vehicle_type_required, v.vehicle_type) and not _overlaps_with_buffer(
                        v.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes):
                    new_vehicle = v
                    break
            if new_vehicle is None:
                continue
        job.assigned_driver_id = best_driver.id
        job.assigned_driver_name = best_driver.name
        job.assigned_vehicle_id = new_vehicle.id if new_vehicle else None
        job.assigned_vehicle_plate = new_vehicle.plate if new_vehicle else ""
        job.unresolved = False
        job.assignment_note = f"In-house: {best_driver.name} [filled an existing gap in their day]"
        best_driver.busy_intervals.append((job.start_dt, job.end_dt, None))
        best_driver.occupied_seconds = _merged_hours(best_driver.busy_intervals) * 3600.0
        if new_vehicle:
            new_vehicle.busy_intervals.append((job.start_dt, job.end_dt, None))
        filled_any = True
    return filled_any


def _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days):
    """HR-002 rework: a driver who is used at all on a given day must reach
    at least their configured working_hours_per_day that day. The main
    allocation pass above can't guarantee this directly (it assigns jobs
    one at a time in time order and doesn't know a driver's full-day total
    until the day's last job has been considered), so this runs afterward
    as a best-effort repair: for every driver left with a non-zero,
    under-minimum day, try to move ALL of that driver's jobs for that day
    to other qualifying drivers with spare room. If every job can be
    moved, commit the move. If even one can't, release the whole day back
    to unresolved instead of leaving an illegal short day in place --
    moving only some of the jobs would just create a different
    under-minimum day rather than fixing anything.

    Scope: only touches jobs with no "Same Driver" group tag. Grouped jobs
    are an explicit planner instruction and are left alone.

    This is a greedy, best-effort heuristic, not a global optimizer -- it
    can leave a fixable case unfixed if an earlier move in the same pass
    used up the room that would have fixed a later one. allocate() calls
    this in a small loop (see below) so a fix made in one pass can enable
    another fix in the next, up to a small iteration cap.

    Correctness notes (fixed 2026-08-03 after a real ping-ponging bug was
    caught in testing -- see tests/test_gap_filling.py and
    CHANGELOG_AI.md): (1) each (day, driver) pair's job list and total
    hours are recomputed FRESH from `jobs` right before it's processed,
    never from a snapshot taken at the start of this call -- an earlier
    fix in the same pass can change what a later pair's driver actually
    has, and processing against a stale list caused jobs to be moved back
    and forth between two drivers instead of settling. (2) when several
    of one driver's jobs are being moved to the same new driver in one
    batch, each already-planned-but-not-yet-committed move in that batch
    is counted against the new driver's projected hours/overlap check for
    the next job in the same batch -- otherwise two moves could each look
    individually legal but together silently exceed that driver's daily
    ceiling.

    Returns True if anything changed (so the caller knows whether another
    pass might help), False otherwise.
    """
    allow_override_days = allow_override_days or {}
    driver_by_id = {d.id: d for d in driver_pool}
    vehicle_by_id = {v.id: v for v in vehicle_pool}

    candidate_keys = set()
    for job in jobs:
        if job.assigned_driver_id is None or job.start_dt is None or job.end_dt is None:
            continue
        candidate_keys.add((job.start_dt.date(), job.assigned_driver_id))

    changed = False
    for day, driver_id in sorted(candidate_keys):
        driver = driver_by_id.get(driver_id)
        if driver is None or driver.working_hours_per_day is None:
            continue

        # Recomputed FRESH every time -- see correctness note (1) above.
        # Includes EVERY job assigned to this driver that day, grouped or
        # not: total_hours has to reflect the driver's true day, even
        # though only the ungrouped subset is ever movable (see below).
        # Uses merged/deduplicated hours (2026-08-03 hour-accounting fix,
        # see _merged_hours above) -- two overlapping same-time rows in a
        # flagged group count once, not once each.
        all_day_jobs = [
            j for j in jobs
            if j.assigned_driver_id == driver_id and j.start_dt is not None and j.start_dt.date() == day
        ]
        total_hours = _merged_hours([(j.start_dt, j.end_dt) for j in all_day_jobs])
        if total_hours <= 1e-9 or total_hours >= driver.working_hours_per_day - 1e-9:
            continue  # unused, already fixed by an earlier pair in this pass, or already meets the minimum

        # If any of the driver's jobs that day are inside a "Same Driver"
        # group, this day can't be fixed: those jobs are an explicit
        # planner instruction and are never moved (see docstring scope).
        # Moving only the ungrouped remainder would never help here --
        # the group's own hours are what's driving the shortfall, or at
        # best moving the rest away only makes the driver's day smaller.
        # Confirmed against a real PLANNED.xlsx that this is a genuine,
        # accepted real-world pattern (a driver's day built almost
        # entirely from one flagged group's hours, under the 9h baseline,
        # with no legal way to top it up) -- not a bug to "fix" by force.
        if any(_group_key_of(j) for j in all_day_jobs):
            continue

        day_jobs = all_day_jobs  # all ungrouped, confirmed above
        moves = []  # (job, new_driver, new_vehicle)
        feasible = True
        # Tracks hours/intervals tentatively added to a candidate driver
        # within THIS batch, before anything is actually committed -- see
        # correctness note (2) above.
        tentative_intervals = {}
        tentative_vehicle_intervals = {}
        for job in day_jobs:
            new_driver = None
            for d in driver_pool:
                if d.id == driver_id:
                    continue
                if not _driver_qualifies_for_type(d, job.vehicle_type_required):
                    continue
                if _driver_is_off(d, day, allow_override_days):
                    continue
                if not _job_matches_shift_period(job.start_dt, d.shift_period):
                    continue
                combined_busy = d.busy_intervals + tentative_intervals.get(d.id, [])
                if _overlaps_with_buffer(combined_busy, job.start_dt, job.end_dt, travel_buffer_minutes):
                    continue
                if d.working_hours_per_day is not None:
                    projected = _merged_hours(combined_busy + [(job.start_dt, job.end_dt)])
                    ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                    if projected > ceiling + 1e-9:
                        continue
                    overtime = max(0.0, projected - d.working_hours_per_day)
                    if d.max_overtime_hours_per_month is not None:
                        if d.month_overtime_so_far + overtime > d.max_overtime_hours_per_month + 1e-9:
                            continue
                    elif overtime > 0:
                        continue
                new_driver = d
                break
            if new_driver is None:
                feasible = False
                break
            new_vehicle = None
            if _vehicle_type_needs_vehicle(job.vehicle_type_required):
                for v in vehicle_pool:
                    combined_vehicle_busy = v.busy_intervals + tentative_vehicle_intervals.get(v.id, [])
                    if _type_matches(job.vehicle_type_required, v.vehicle_type) and not _overlaps_with_buffer(
                            combined_vehicle_busy, job.start_dt, job.end_dt, travel_buffer_minutes):
                        new_vehicle = v
                        break
                if new_vehicle is None:
                    feasible = False
                    break
                tentative_vehicle_intervals.setdefault(new_vehicle.id, []).append((job.start_dt, job.end_dt, None))
            tentative_intervals.setdefault(new_driver.id, []).append((job.start_dt, job.end_dt, None))
            moves.append((job, new_driver, new_vehicle))

        def _release(job):
            driver.busy_intervals = [iv for iv in driver.busy_intervals if iv != (job.start_dt, job.end_dt, None)]
            driver.occupied_seconds = _merged_hours(driver.busy_intervals) * 3600.0
            old_vehicle = vehicle_by_id.get(job.assigned_vehicle_id)
            if old_vehicle:
                old_vehicle.busy_intervals = [iv for iv in old_vehicle.busy_intervals if iv != (job.start_dt, job.end_dt, None)]

        if feasible:
            for job, new_driver, new_vehicle in moves:
                _release(job)
                job.assigned_driver_id = new_driver.id
                job.assigned_driver_name = new_driver.name
                job.assigned_vehicle_id = new_vehicle.id if new_vehicle else None
                job.assigned_vehicle_plate = new_vehicle.plate if new_vehicle else ""
                job.assignment_note = f"In-house: {new_driver.name} [reassigned to meet {driver.name}'s daily minimum]"
                new_driver.busy_intervals.append((job.start_dt, job.end_dt, None))
                new_driver.occupied_seconds = _merged_hours(new_driver.busy_intervals) * 3600.0
                if new_vehicle:
                    new_vehicle.busy_intervals.append((job.start_dt, job.end_dt, None))
            changed = True
        else:
            for job in day_jobs:
                _release(job)
                job.assigned_driver_id = None
                job.assigned_driver_name = ""
                job.assigned_vehicle_id = None
                job.assigned_vehicle_plate = ""
                job.unresolved = True
                job.assignment_note = (
                    f"Below {driver.name}'s minimum daily hours ({total_hours:.1f}h < "
                    f"{driver.working_hours_per_day:.1f}h required) and no other qualifying "
                    f"driver had room -- needs manual review"
                )
            changed = True

    return changed


# --- OPT-001/OPT-002/OPT-003 fix (2026-08-03) ---------------------------
# Confirmed by the project owner testing against a real UNPLANNED.xlsx:
# the engine was scattering a driver across a wide time window with a big
# idle hole in the middle (e.g. 13:00-15:00 then 22:00-01:00 -- a 12h span
# for only 5h of actual work) while a job that would have fit neatly into
# that hole (16:00-20:00) was left completely unassigned. Root cause: the
# engine had no concept of a driver's existing "gap" as something to
# actively prefer filling -- candidate selection only ever used
# occupied_seconds (least-occupied-first fairness), which has no reason to
# favor a driver whose schedule happens to have a hole over an idle driver
# with none. This didn't cause overlap conflicts (that check was always
# correct), it just never tried to consolidate.
def _driver_has_bounded_gap_fit(driver, job_start, job_end, travel_buffer_minutes):
    """True if this job would sit strictly BETWEEN two of the driver's
    already-assigned jobs (with the normal travel buffer on both sides) --
    i.e. genuinely fills an existing hole in their day, as opposed to
    simply extending their day earlier or later. Deliberately narrow: this
    does not fire for a driver who is merely idle all day (no existing
    jobs) or one who'd only be extended forward/back, since those aren't
    the "wasted idle gap" pattern this is meant to fix."""
    buffer = timedelta(minutes=travel_buffer_minutes)
    has_earlier = any(iv_end + buffer <= job_start for iv_start, iv_end, _ in driver.busy_intervals)
    has_later = any(job_end + buffer <= iv_start for iv_start, iv_end, _ in driver.busy_intervals)
    return has_earlier and has_later


# --- Hour-accounting fix (2026-08-03) ------------------------------------
# The module docstring above (point 4) documented, as a deliberate known
# simplification, that occupied_seconds summed every row's duration even
# when two rows in the same "Same Driver" group overlap in time (e.g. two
# simultaneous pickups on one truck, same driver, identical start/end).
# The project owner tested against a real PLANNED.xlsx and confirmed this
# is a real practical problem, not just a theoretical one: a driver with
# several such overlapping pairs across a day (worth ~11h of ACTUAL work)
# was showing ~17h of SUMMED "occupied" time, which falsely exceeds any
# realistic daily ceiling and blocks legitimate further assignment (or
# wrongly moves/unresolves jobs via the HR-005 repair pass) even though
# the driver is nowhere near actually full. This is the "deliberate
# follow-up" the docstring flagged. Fixed here: occupied_seconds is now
# always the UNION (deduplicated) total of a driver's busy_intervals, not
# a running sum -- two overlapping rows for the same real-world duty
# period count once, exactly like a human dispatcher would count them.
def _merged_hours(intervals):
    """Total hours covered by the UNION of the given intervals -- two
    overlapping or touching intervals count once, not once each. Accepts
    2-tuples (start, end) or longer tuples (start, end, ...); anything
    past the first two elements is ignored."""
    if not intervals:
        return 0.0
    spans = sorted((iv[0], iv[1]) for iv in intervals)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return sum((e - s).total_seconds() for s, e in merged) / 3600.0


def allocate(jobs, drivers, vehicles, supplier_offerings,
             allowed_driver_ids=None, allowed_supplier_ids=None,
             allow_override_days=None, travel_buffer_minutes=DEFAULT_TRAVEL_BUFFER_MINUTES):
    """
    Mutates each Job in `jobs` with assignment info. Returns the same list.
    """
    allow_override_days = allow_override_days or {}

    driver_pool = [d for d in drivers if allowed_driver_ids is None or d.id in allowed_driver_ids]
    vehicle_pool = [v for v in vehicles if not v.in_workshop]
    offering_pool = [o for o in supplier_offerings if allowed_supplier_ids is None or o.supplier_id in allowed_supplier_ids]

    # Runtime registry of hires created so far today, keyed by (supplier_id, vehicle_type)
    hires_by_key = {}

    # "Same Driver" group registries -- see module docstring for the agreed
    # rules. Keyed by the planner-pasted group text (job.same_driver_key).
    group_drivers = {}          # group_key -> [DriverProfile, ...] in first-used order
    group_vehicle_by_driver = {}  # (group_key, driver_id) -> VehicleProfile last used for this driver in this group
    group_supplier_hires = {}   # group_key -> [SupplierHire, ...] already used for this group

    for job in sorted(jobs, key=lambda j: j.start_dt or j.row_number):
        if job.start_dt is None or job.end_dt is None:
            job.unresolved = True
            job.assignment_note = "Could not parse date/time for this row"
            continue

        job_date = job.start_dt.date()
        job_hours = (job.end_dt - job.start_dt).total_seconds() / 3600.0
        group_key = _group_key_of(job)
        note_suffix = " [Same Driver group]" if group_key else ""

        # ---------------------------------------------------- in-house pass
        candidates = []
        for d in driver_pool:
            if not _driver_qualifies_for_type(d, job.vehicle_type_required):
                continue
            if _driver_is_off(d, job_date, allow_override_days):
                continue
            if not _job_matches_shift_period(job.start_dt, d.shift_period):
                continue
            # SAME-DRIVER GROUP OVERLAP FIX (2026-08-03): the relaxation
            # below exists so a driver can legitimately have two
            # overlapping/simultaneous rows in one flagged group when
            # they're genuinely the SAME physical vehicle serving two
            # orders at once (e.g. one truck, two simultaneous pickups).
            # It must NOT apply when the group's rows need a DIFFERENT
            # vehicle type -- a driver cannot be behind the wheel of a
            # Chiller Truck and a Bus at the same time just because both
            # rows share a "Same Driver" tag. Confirmed as a real bug: a
            # real test run put one driver on a Chiller Truck 23:00-00:00
            # AND a Bus 23:00-01:00 simultaneously, both inside the same
            # flagged group. Only relax the overlap check for THIS driver
            # if they have no established vehicle for this group yet
            # (nothing to conflict with), or if their established vehicle
            # for this group is the same TYPE as what this job needs.
            established_vehicle = group_vehicle_by_driver.get((group_key, d.id)) if group_key else None
            vehicle_type_consistent = (
                established_vehicle is None
                or _type_matches(job.vehicle_type_required, established_vehicle.vehicle_type)
            )
            effective_group_key = group_key if (group_key and vehicle_type_consistent) else None
            if _overlaps_with_buffer(d.busy_intervals, job.start_dt, job.end_dt,
                                      travel_buffer_minutes, ignore_group_key=effective_group_key):
                continue

            if d.working_hours_per_day is not None:
                projected_today_hours = _merged_hours(d.busy_intervals + [(job.start_dt, job.end_dt)])
                # Hard daily ceiling -- applies regardless of how much monthly
                # overtime allowance remains. See HR-002 rework note above.
                daily_ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                if projected_today_hours > daily_ceiling + 1e-9:
                    continue
                projected_today_overtime = max(0.0, projected_today_hours - d.working_hours_per_day)
                if d.max_overtime_hours_per_month is not None:
                    projected_month_overtime = d.month_overtime_so_far + projected_today_overtime
                    if projected_month_overtime > d.max_overtime_hours_per_month:
                        continue
                elif projected_today_overtime > 0:
                    # No monthly overtime cap configured for this driver.
                    # Confirmed with the project owner: this must NOT mean
                    # "unlimited hours in a single day" -- working_hours_per_day
                    # is still a hard daily ceiling (0 overtime allowed)
                    # whenever no explicit monthly overtime allowance exists.
                    # (Previously this whole block was skipped when
                    # max_overtime_hours_per_month was blank, silently
                    # letting a driver work unlimited hours in one day --
                    # fixed here.)
                    continue
            candidates.append(d)

        if candidates:
            # Prefer a driver already assigned to this same flagged group, if
            # they still qualify for this row -- keeps the group to as few
            # drivers as possible instead of always picking the globally
            # least-occupied driver. Falls back to normal fairness ranking
            # when there's no group yet, or none of the group's current
            # driver(s) qualify for this particular row.
            #
            # NOTE (2026-08-03): gap-filling is deliberately NOT done here.
            # Jobs are processed strictly in start-time order, so a job
            # that would sit *between* two of a driver's bookings is always
            # evaluated before the LATER of those two bookings has been
            # assigned to anyone -- there's no way to know about that gap
            # yet in a single forward pass. See _fill_gaps_with_unresolved_jobs()
            # below, which runs as a post-pass instead, once every driver's
            # day is actually known. See OPT-001/002/003 in the scheduling
            # rules spec.
            # NEW-007 fix (2026-08-03): reserve narrowly-licensed
            # "specialist" drivers for the jobs only they (or few others)
            # can do, instead of spending their limited hours on a job a
            # broadly-licensed "generalist" driver could equally take.
            # Real example from the project owner: a driver licensed ONLY
            # for "10 Ton Chiller Truck" had every one of that day's
            # Chiller Truck requests fit cleanly in their hours -- but if
            # an earlier, non-exclusive job (one a generalist could also
            # cover) had been given to them first, it could burn hours
            # that were needed for the Chiller-only requests later that
            # day, pushing those to supplier/unresolved unnecessarily even
            # though a driver who could ONLY do them was sitting idle.
            #
            # Scoped to UNGROUPED jobs only. Tried applying this to a
            # "Same Driver" group's first-ever assignment too and it
            # backfired: it can steal a group's opening job away from a
            # specialist toward a generalist (since neither is in
            # group_drivers[group_key] yet, both fall into the same
            # candidate pool this ranking applies to), fragmenting a block
            # of work that should have stayed on one driver from the
            # start. Group continuity (group_candidates below, and
            # least-occupied fairness for a group's first assignment)
            # already handles consolidation correctly on its own and
            # takes priority; this heuristic only kicks in once there's no
            # group to defer to.
            group_candidates = [d for d in candidates if group_key and d in group_drivers.get(group_key, [])]
            if group_candidates or group_key:
                chosen_driver = min(group_candidates or candidates, key=lambda d: d.occupied_seconds)
            else:
                chosen_driver = min(candidates, key=lambda d: (-len(d.license_types), d.occupied_seconds))

            # NEW-004 fix (2026-08-03): a "Driver Only" row needs a
            # qualified driver but no physical vehicle at all -- confirmed
            # by a real PLANNED.xlsx row (a driver-only assignment with no
            # vehicle involved). Previously this always fell through to
            # unresolved/supplier since no VehicleProfile could ever match
            # a type that doesn't correspond to a real vehicle.
            if not _vehicle_type_needs_vehicle(job.vehicle_type_required):
                job.assigned_driver_id = chosen_driver.id
                job.assigned_driver_name = chosen_driver.name
                job.assigned_vehicle_id = None
                job.assigned_vehicle_plate = ""
                job.assignment_note = f"In-house: {chosen_driver.name} (driver only, no vehicle){note_suffix}"
                chosen_driver.busy_intervals.append((job.start_dt, job.end_dt, group_key))
                chosen_driver.occupied_seconds = _merged_hours(chosen_driver.busy_intervals) * 3600.0
                if group_key:
                    group_drivers.setdefault(group_key, [])
                    if chosen_driver not in group_drivers[group_key]:
                        group_drivers[group_key].append(chosen_driver)
                continue

            matching_vehicles = [
                v for v in vehicle_pool
                if _type_matches(job.vehicle_type_required, v.vehicle_type)
                and not _overlaps_with_buffer(v.busy_intervals, job.start_dt, job.end_dt,
                                               travel_buffer_minutes, ignore_group_key=group_key)
            ]
            if matching_vehicles:
                # Prefer the same vehicle this driver already used for this
                # group, if it's still free and still the right type.
                preferred_vehicle = group_vehicle_by_driver.get((group_key, chosen_driver.id)) if group_key else None
                chosen_vehicle = preferred_vehicle if preferred_vehicle in matching_vehicles else matching_vehicles[0]

                job.assigned_driver_id = chosen_driver.id
                job.assigned_driver_name = chosen_driver.name
                job.assigned_vehicle_id = chosen_vehicle.id
                job.assigned_vehicle_plate = chosen_vehicle.plate
                job.assignment_note = f"In-house: {chosen_driver.name}{note_suffix}"

                chosen_driver.busy_intervals.append((job.start_dt, job.end_dt, group_key))
                chosen_driver.occupied_seconds = _merged_hours(chosen_driver.busy_intervals) * 3600.0
                chosen_vehicle.busy_intervals.append((job.start_dt, job.end_dt, group_key))

                if group_key:
                    group_drivers.setdefault(group_key, [])
                    if chosen_driver not in group_drivers[group_key]:
                        group_drivers[group_key].append(chosen_driver)
                    group_vehicle_by_driver[(group_key, chosen_driver.id)] = chosen_vehicle
                continue
            # Qualified driver but no matching in-house vehicle -> fall through to supplier.

        # ---------------------------------------------------- supplier pass
        matching_offerings = [o for o in offering_pool if _type_matches(job.vehicle_type_required, o.vehicle_type)]
        if not matching_offerings:
            job.unresolved = True
            job.assignment_note = "No qualifying in-house or supplier resource available"
            continue

        # Priority 1: reuse an already-hired unit that's free now. If this
        # row is part of a flagged group, prefer a unit already used for
        # THAT group first (same "fewest units" reasoning as for drivers),
        # then fall back to any free reusable unit as before.
        reusable_hire = None
        if group_key:
            for hire in group_supplier_hires.get(group_key, []):
                if _type_matches(job.vehicle_type_required, hire.vehicle_type) and not _overlaps_with_buffer(
                        hire.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes, ignore_group_key=group_key):
                    reusable_hire = hire
                    break
        if reusable_hire is None:
            for o in matching_offerings:
                key = (o.supplier_id, o.vehicle_type)
                for hire in hires_by_key.get(key, []):
                    if not _overlaps_with_buffer(hire.busy_intervals, job.start_dt, job.end_dt,
                                                  travel_buffer_minutes, ignore_group_key=group_key):
                        reusable_hire = hire
                        break
                if reusable_hire:
                    break

        if reusable_hire:
            reusable_hire.busy_intervals.append((job.start_dt, job.end_dt, group_key))
            label = reusable_hire.label
            job.assigned_supplier_unit = f"SAME {label}" if reusable_hire.already_used else label
            job.assigned_supplier_id = reusable_hire.supplier_id
            reusable_hire.already_used = True
            job.assignment_note = f"Supplier: {reusable_hire.supplier_name}{note_suffix}"
            if group_key:
                group_supplier_hires.setdefault(group_key, [])
                if reusable_hire not in group_supplier_hires[group_key]:
                    group_supplier_hires[group_key].append(reusable_hire)
            continue

        # Priority 2: hire a new unit. Pick the offering with capacity left,
        # preferring the supplier with the least cumulative historical
        # hours (cross-day fairness) among those tied on capacity.
        hireable = []
        for o in matching_offerings:
            key = (o.supplier_id, o.vehicle_type)
            already_hired_count = len(hires_by_key.get(key, []))
            if o.max_available_per_day is None or already_hired_count < o.max_available_per_day:
                hireable.append(o)

        if not hireable:
            job.unresolved = True
            job.assignment_note = "No qualifying in-house or supplier resource available (all suppliers at daily capacity)"
            continue

        chosen_offering = min(hireable, key=lambda o: o.cumulative_hours_history)
        key = (chosen_offering.supplier_id, chosen_offering.vehicle_type)
        instance_number = len(hires_by_key.get(key, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id,
            supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type,
            instance_number=instance_number,
        )
        new_hire.busy_intervals.append((job.start_dt, job.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        if group_key:
            group_supplier_hires.setdefault(group_key, []).append(new_hire)

        job.assigned_supplier_unit = new_hire.label
        job.assigned_supplier_id = new_hire.supplier_id
        job.assignment_note = f"Supplier: {new_hire.supplier_name}{note_suffix}"

    # OPT-001/002/003 fix: try to slot any still-unresolved job into a
    # genuine gap in an existing driver's day before falling back to the
    # minimum-hours repair pass below -- see _fill_gaps_with_unresolved_jobs()
    # docstring. Run before the minimum-hours pass since filling a gap
    # adds hours to that driver, which can itself resolve an
    # under-minimum day without any reassignment being needed.
    _fill_gaps_with_unresolved_jobs(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days)

    # HR-002 rework: enforce the daily-minimum-hours rule now that the full
    # day's in-house assignments are known. Looped a few times since one
    # fix can free up room that makes another fix possible. See
    # _repair_minimum_daily_hours() docstring for what this does and does
    # not guarantee.
    for _ in range(5):
        if not _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days):
            break

    return jobs

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


def _established_group_vehicle_type(jobs, driver_id, group_key):
    """Returns the vehicle_type_required already used by this driver for
    this Same-Driver group (from any job already assigned to them, group-
    wide, not just today), or None if they have no rows in this group yet.
    Used by the widened HR-005 repair pass (2026-08-04) so a group being
    moved onto a new driver still respects the SD-004 rule -- a driver
    can't be put on two different vehicle types at once just because both
    rows share a group tag."""
    if not group_key:
        return None
    for j in jobs:
        if j.assigned_driver_id == driver_id and _group_key_of(j) == group_key:
            return j.vehicle_type_required
    return None


def _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days,
                                 settled_job_ids=None):
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

    Scope (widened 2026-08-04): can now move a driver's grouped ("Same
    Driver") jobs too, not just ungrouped ones -- confirmed with the
    project owner as a real fix, not an assumption, after a day where 84%
    of rows were flagged meant this pass almost never ran at all (see
    CHANGELOG_AI.md). A whole group is always moved to ONE new driver
    together (never split across the move) and SD-004 vehicle-type
    consistency is still enforced on the new driver. If no single driver
    can legally take the group, the day is still released to unresolved,
    same as before -- this remains a real, expected outcome sometimes,
    not a bug.

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

    settled_job_ids: a set of Python id(job) values, shared across every
    call within one allocate() run (see the 2026-08-04 note below). Once a
    job has been moved by this function, its id is added here and it is
    never moved again for the rest of this run. WITHOUT this, widening the
    scope to grouped days (2026-08-04) caused real thrashing: a short
    group would be moved onto a driver with room, but that driver's OWN
    day (now including the freshly-received group) could itself look
    under-minimum to the next pass, so a DIFFERENT group would get moved
    onto them next, and so on -- groups visibly bounced between drivers
    across the 5-pass loop instead of settling. Marking moved jobs as
    settled trades a small amount of theoretical optimality (a group
    moved early might not be the best possible final placement) for a
    guaranteed, deterministic stop -- consistent with this function's
    documented "best-effort heuristic, not a global optimizer" framing.

    Returns True if anything changed (so the caller knows whether another
    pass might help), False otherwise.
    """
    allow_override_days = allow_override_days or {}
    if settled_job_ids is None:
        settled_job_ids = set()
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

        # Stability guard (2026-08-04) -- see settled_job_ids note above.
        # If any job here was already moved earlier in this same run, this
        # day is considered settled: leave it exactly as the earlier move
        # placed it, even if it's still technically under-minimum. This is
        # what stops groups from being bounced between drivers pass after
        # pass without ever converging.
        if any(id(j) in settled_job_ids for j in all_day_jobs):
            continue

        # WIDENED 2026-08-04 (project owner confirmed, see CHANGELOG_AI.md):
        # grouped ("Same Driver") days used to be skipped entirely here --
        # confirmed against a real PLANNED.xlsx as sometimes genuinely
        # unfixable (a group's own hours ARE the shortfall). But on a
        # dataset where most rows carry a Same Driver tag, that skip meant
        # this whole repair pass rarely ran at all, and severe imbalance
        # (some drivers at 2h, others near their ceiling) went completely
        # unaddressed. Now grouped days are attempted too: the WHOLE
        # group is moved together onto one new driver where possible
        # (never split across the move, since that would break the
        # planner's explicit "same driver" instruction), respecting every
        # hard rule including SD-004 vehicle-type consistency. If the move
        # genuinely isn't feasible (no single driver can take the whole
        # group), the day is released to unresolved exactly as before --
        # this is still a real possibility and not treated as a bug.
        day_jobs = all_day_jobs
        moves = []  # (job, new_driver, new_vehicle)
        feasible = True
        # Tracks hours/intervals tentatively added to a candidate driver
        # within THIS batch, before anything is actually committed -- see
        # correctness note (2) above. Intervals are tagged with each job's
        # own group_key (not always None) so the same-group overlap
        # relaxation still applies correctly to later jobs in this batch.
        tentative_intervals = {}
        tentative_vehicle_intervals = {}
        # group_key -> driver.id already chosen for this group WITHIN this
        # batch -- tried first for the group's later rows, so a group
        # being moved lands on as few new drivers as possible (mirrors
        # SD-002/SD-003 in the main pass) instead of fragmenting.
        tentative_group_leader = {}

        tentative_group_vehicle = {}  # (driver_id, group_key) -> vehicle_type_required

        def tentative_group_vehicle_for(new_driver_id, group_key):
            if (new_driver_id, group_key) in tentative_group_vehicle:
                return tentative_group_vehicle[(new_driver_id, group_key)]
            return _established_group_vehicle_type(jobs, new_driver_id, group_key)

        def _driver_is_feasible_for(d, job, group_key):
            """Returns (True, effective_group_key) if d can legally take this
            job right now (given tentative state so far in this batch), or
            (False, None) if not. effective_group_key is the group_key to
            use for the overlap check -- None means "no group relaxation",
            which is a normal, successful outcome for an ungrouped job or
            for a grouped job whose vehicle type doesn't match what d
            already has established for that group (SD-004)."""
            if d.id == driver_id:
                return False, None  # can't move a job back onto the driver we're trying to fix
            if not _driver_qualifies_for_type(d, job.vehicle_type_required):
                return False, None
            if _driver_is_off(d, day, allow_override_days):
                return False, None
            if not _job_matches_shift_period(job.start_dt, d.shift_period):
                return False, None
            established_type = tentative_group_vehicle_for(d.id, group_key) if group_key else None
            vehicle_type_consistent = established_type is None or _type_matches(job.vehicle_type_required, established_type)
            effective_group_key = group_key if (group_key and vehicle_type_consistent) else None
            combined_busy = d.busy_intervals + tentative_intervals.get(d.id, [])
            if _overlaps_with_buffer(combined_busy, job.start_dt, job.end_dt,
                                      travel_buffer_minutes, ignore_group_key=effective_group_key):
                return False, None
            if d.working_hours_per_day is not None:
                projected = _merged_hours(combined_busy + [(job.start_dt, job.end_dt)])
                ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                if projected > ceiling + 1e-9:
                    return False, None
                overtime = max(0.0, projected - d.working_hours_per_day)
                if d.max_overtime_hours_per_month is not None:
                    if d.month_overtime_so_far + overtime > d.max_overtime_hours_per_month + 1e-9:
                        return False, None
                elif overtime > 0:
                    return False, None
            return True, effective_group_key

        for job in day_jobs:
            job_group_key = _group_key_of(job)
            new_driver = None
            effective_group_key = None

            # Prefer whichever driver this batch already picked for the
            # SAME group, if they still qualify -- keeps the group intact
            # on one driver instead of fragmenting it across the move.
            if job_group_key and job_group_key in tentative_group_leader:
                preferred = driver_by_id.get(tentative_group_leader[job_group_key])
                if preferred is not None:
                    ok, egk = _driver_is_feasible_for(preferred, job, job_group_key)
                    if ok:
                        new_driver, effective_group_key = preferred, egk

            if new_driver is None:
                # Search in least-occupied-first order, not driver_pool's
                # natural (alphabetical) order (fixed 2026-08-04). Picking
                # the first alphabetical match meant a driver who simply
                # hadn't had their own turn processed yet in this same
                # pass could look "busy" and get skipped in favor of one
                # who happened to already be freed moments earlier by an
                # unrelated fix -- a real, observed processing-order
                # artifact, not a fairness decision. Ranking by current
                # occupied hours picks the genuinely freest qualifying
                # driver regardless of iteration order.
                for d in sorted(driver_pool, key=lambda x: x.occupied_seconds):
                    ok, egk = _driver_is_feasible_for(d, job, job_group_key)
                    if ok:
                        new_driver, effective_group_key = d, egk
                        break

            if new_driver is None:
                feasible = False
                break

            new_vehicle = None
            if _vehicle_type_needs_vehicle(job.vehicle_type_required):
                for v in vehicle_pool:
                    combined_vehicle_busy = v.busy_intervals + tentative_vehicle_intervals.get(v.id, [])
                    if _type_matches(job.vehicle_type_required, v.vehicle_type) and not _overlaps_with_buffer(
                            combined_vehicle_busy, job.start_dt, job.end_dt,
                            travel_buffer_minutes, ignore_group_key=job_group_key):
                        new_vehicle = v
                        break
                if new_vehicle is None:
                    feasible = False
                    break
                tentative_vehicle_intervals.setdefault(new_vehicle.id, []).append((job.start_dt, job.end_dt, job_group_key))
            tentative_intervals.setdefault(new_driver.id, []).append((job.start_dt, job.end_dt, job_group_key))
            if job_group_key:
                tentative_group_leader[job_group_key] = new_driver.id
                tentative_group_vehicle[(new_driver.id, job_group_key)] = job.vehicle_type_required
            moves.append((job, new_driver, new_vehicle, job_group_key))

        def _release(job):
            tag = _group_key_of(job)
            driver.busy_intervals = [iv for iv in driver.busy_intervals if iv != (job.start_dt, job.end_dt, tag)]
            driver.occupied_seconds = _merged_hours(driver.busy_intervals) * 3600.0
            old_vehicle = vehicle_by_id.get(job.assigned_vehicle_id)
            if old_vehicle:
                old_vehicle.busy_intervals = [iv for iv in old_vehicle.busy_intervals if iv != (job.start_dt, job.end_dt, tag)]

        if feasible:
            for job, new_driver, new_vehicle, job_group_key in moves:
                _release(job)
                job.assigned_driver_id = new_driver.id
                job.assigned_driver_name = new_driver.name
                job.assigned_vehicle_id = new_vehicle.id if new_vehicle else None
                job.assigned_vehicle_plate = new_vehicle.plate if new_vehicle else ""
                group_note = " [Same Driver group]" if job_group_key else ""
                job.assignment_note = f"In-house: {new_driver.name} [reassigned to meet {driver.name}'s daily minimum]{group_note}"
                new_driver.busy_intervals.append((job.start_dt, job.end_dt, job_group_key))
                new_driver.occupied_seconds = _merged_hours(new_driver.busy_intervals) * 3600.0
                if new_vehicle:
                    new_vehicle.busy_intervals.append((job.start_dt, job.end_dt, job_group_key))
                settled_job_ids.add(id(job))
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


def _rebalance_idle_drivers(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days,
                             settled_job_ids=None):
    """
    NEW (2026-08-06): "every driver has real work" is now a first-class
    goal, not just "no driver is illegally under-minimum." Confirmed with
    the project owner directly, after comparing real output to the real
    PLANNED.xlsx reference: the human plan uses all 11 drivers, none below
    7.5h, while both automated versions were leaving 2 drivers sitting at
    a fully idle 0h -- legal under HR-005 (a driver "not used at all" has
    no minimum to violate), but not what the real planner does. 0h drivers
    were themselves usually a SIDE EFFECT of _repair_minimum_daily_hours
    freeing a short day entirely to fix someone else's minimum, with
    nothing afterward ever revisiting that now-idle driver.

    For every driver sitting at 0h who is otherwise available (not
    excluded, has working_hours_per_day configured), tries to accumulate
    enough real work to reach their OWN minimum -- pulling first from
    anything still fully unresolved (free, no one loses anything), then
    from genuine SURPLUS on other drivers (hours above THEIR OWN minimum,
    never dropping a donor below it). Candidates are only TENTATIVELY
    accumulated; nothing is actually committed to `jobs`/driver state
    unless the total reaches the idle driver's minimum.

    CRITICAL correctness rule, found via a real regression while building
    this: a driver can legitimately be released to 0h by
    _repair_minimum_daily_hours because there simply isn't enough
    reachable work to bring them to minimum (see
    tests/test_daily_overtime_ceiling.py's single-driver case). If this
    function greedily grabbed the first unresolved/surplus job it found
    without checking the FULL total, it could hand that same driver a
    single job well under their minimum -- recreating the exact violation
    the repair pass just fixed, or even handing the SAME job right back to
    the SAME driver it was just released from, causing the two passes to
    fight each other indefinitely. Fixed by never mutating real state
    until the FULL accumulated total for one idle driver reaches their
    minimum; if it can't be reached, everything tentative for that driver
    is simply discarded (nothing was ever committed, so there's nothing to
    undo) and the driver is correctly left at a legal 0h.

    Scoped to ungrouped jobs only, in this first version -- pulling a
    whole "Same Driver" group away from a donor to rescue an idle driver
    is a further step, not attempted here. Respects settled_job_ids, the
    same stability guard _repair_minimum_daily_hours uses.

    Returns True if anything changed, so the caller can loop it alongside
    the other rearrangement passes until stable.
    """
    allow_override_days = allow_override_days or {}
    if settled_job_ids is None:
        settled_job_ids = set()
    changed = False

    idle_drivers = [d for d in driver_pool if d.working_hours_per_day is not None and d.occupied_seconds <= 1e-9]

    def _feasible(idle, job, accumulated):
        if job.start_dt is None or job.end_dt is None or _group_key_of(job):
            return False
        if not _driver_qualifies_for_type(idle, job.vehicle_type_required):
            return False
        if _driver_is_off(idle, job.start_dt.date(), allow_override_days):
            return False
        if not _job_matches_shift_period(job.start_dt, idle.shift_period):
            return False
        if _overlaps_with_buffer(accumulated, job.start_dt, job.end_dt, travel_buffer_minutes):
            return False
        ceiling = _driver_ceiling(idle)
        projected = _merged_hours([(s, e) for s, e, _ in accumulated] + [(job.start_dt, job.end_dt)])
        if ceiling is not None and projected > ceiling + 1e-9:
            return False
        return True

    for idle in idle_drivers:
        minimum = idle.working_hours_per_day
        accumulated = []          # tentative (start, end, None) intervals for this idle driver
        planned = []               # [{'job':, 'kind': 'unresolved'|'donor', 'donor':}]
        tentative_donor_removed = {}  # donor.id -> [(start,end), ...] tentatively pulled this batch

        picked_job_ids = set()

        for _ in range(20):  # hard cap, real datasets are nowhere near this
            if _merged_hours([(s, e) for s, e, _ in accumulated]) >= minimum - 1e-9:
                break
            candidate = None  # (job, kind, donor)

            for job in jobs:
                if not job.unresolved or id(job) in picked_job_ids:
                    continue
                if _feasible(idle, job, accumulated):
                    candidate = (job, "unresolved", None)
                    break

            if candidate is None:
                for donor in sorted(driver_pool, key=lambda d: -d.occupied_seconds):
                    if donor.id == idle.id or donor.working_hours_per_day is None:
                        continue
                    if donor.occupied_seconds / 3600.0 <= donor.working_hours_per_day + 1e-9:
                        continue  # no real surplus -- don't touch
                    # Two different lists on purpose: donor_all_jobs is their
                    # TRUE current workload (used to compute what they'd be
                    # left with), donor_pullable is the subset we're allowed
                    # to actually move (excludes anything already settled
                    # this run, e.g. a job the repair pass just placed on
                    # them -- don't re-move it, but it still counts toward
                    # their real remaining hours if something ELSE is
                    # pulled instead). Conflating these two was a real bug
                    # found while testing: it let a donor's remaining hours
                    # look like 0 (fully idle) when they'd actually still
                    # have a settled job left, silently recreating an
                    # under-minimum day for the donor.
                    donor_all_jobs = [j for j in jobs if j.assigned_driver_id == donor.id and not _group_key_of(j)]
                    donor_pullable = [
                        j for j in donor_all_jobs
                        if id(j) not in settled_job_ids and id(j) not in picked_job_ids
                    ]
                    found = None
                    for job in sorted(donor_pullable, key=lambda j: (j.end_dt - j.start_dt)):
                        if job.start_dt is None or job.end_dt is None:
                            continue
                        remaining_hours = _merged_hours([
                            (x.start_dt, x.end_dt) for x in donor_all_jobs if x is not job
                        ])
                        if 1e-9 < remaining_hours < donor.working_hours_per_day - 1e-9:
                            continue  # would push the donor into a NEW illegal short day
                        if _feasible(idle, job, accumulated):
                            found = job
                            break
                    if found is not None:
                        candidate = (found, "donor", donor)
                        break

            if candidate is None:
                break  # nothing more reachable for this driver

            job, kind, donor = candidate
            accumulated.append((job.start_dt, job.end_dt, None))
            planned.append({"job": job, "kind": kind, "donor": donor})
            picked_job_ids.add(id(job))
            if kind == "donor":
                tentative_donor_removed.setdefault(donor.id, []).append((job.start_dt, job.end_dt))

        total = _merged_hours([(s, e) for s, e, _ in accumulated])
        if total < minimum - 1e-9:
            continue  # couldn't reach the minimum -- discard everything tentative, driver stays legally idle

        # COMMIT -- only reached if the full accumulated total clears the minimum.
        for step in planned:
            job, kind, donor = step["job"], step["kind"], step["donor"]
            if kind == "donor":
                donor.busy_intervals = [iv for iv in donor.busy_intervals if iv != (job.start_dt, job.end_dt, None)]
                donor.occupied_seconds = _merged_hours(donor.busy_intervals) * 3600.0
                job.assigned_driver_id = idle.id
                job.assigned_driver_name = idle.name
                job.assignment_note = f"In-house: {idle.name} [idle-driver rescue: rebalanced from {donor.name}]"
            else:
                new_vehicle = None
                if _vehicle_type_needs_vehicle(job.vehicle_type_required):
                    for v in vehicle_pool:
                        if _type_matches(job.vehicle_type_required, v.vehicle_type) and not _overlaps_with_buffer(
                                v.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes):
                            new_vehicle = v
                            break
                job.assigned_driver_id = idle.id
                job.assigned_driver_name = idle.name
                job.assigned_vehicle_id = new_vehicle.id if new_vehicle else None
                job.assigned_vehicle_plate = new_vehicle.plate if new_vehicle else ""
                job.unresolved = False
                job.assignment_note = f"In-house: {idle.name} [idle-driver rescue: unresolved job]"
                if new_vehicle:
                    new_vehicle.busy_intervals.append((job.start_dt, job.end_dt, None))
            idle.busy_intervals.append((job.start_dt, job.end_dt, None))
            settled_job_ids.add(id(job))
        idle.occupied_seconds = _merged_hours(idle.busy_intervals) * 3600.0
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

    # NEW (2026-08-04): projected fairness for picking a group's FIRST
    # driver. Confirmed with the project owner as a real problem, not a
    # guess: on a real day where most rows carry a Same Driver tag, the
    # old rule ("pick whoever has fewest occupied hours RIGHT NOW") only
    # looks at the single opening row of a group -- it has no idea whether
    # that group is a 2h errand or an 11h event. A driver who happens to
    # be idle when a big group starts can end up carrying that whole
    # group while everyone else stays comparatively empty, and there is
    # no mechanism to reconsider once picked (SD-002/SD-003 lock the group
    # to that driver from then on). Precomputing each group's total merged
    # hours up front lets the initial pick account for the WHOLE group's
    # likely load, not just its first row -- a real look-ahead instead of
    # a one-instant snapshot. This does not change anything for jobs with
    # no group, or for a group's SECOND+ row (which still prefers staying
    # on the group's already-established driver per SD-002/SD-003).
    group_total_hours = {}
    for j in jobs:
        gk = _group_key_of(j)
        if gk and j.start_dt and j.end_dt:
            group_total_hours.setdefault(gk, []).append((j.start_dt, j.end_dt))
    group_total_hours = {gk: _merged_hours(spans) for gk, spans in group_total_hours.items()}

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
            if group_candidates:
                # Group already has a leader for this job's type -- keep it
                # (SD-002/SD-003), same as before.
                chosen_driver = min(group_candidates, key=lambda d: d.occupied_seconds)
            elif group_key:
                # Fresh group: project each candidate's occupied hours PLUS
                # this group's total estimated hours, not just this row's --
                # see the group_total_hours note above. Falls back to
                # job_hours if the group's total wasn't precomputed for any
                # reason (defensive only, should always be present).
                projected_group_hours = group_total_hours.get(group_key, job_hours)
                chosen_driver = min(candidates, key=lambda d: d.occupied_seconds + projected_group_hours * 3600.0)
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
    # 2026-08-06: _rebalance_idle_drivers() now runs alongside it in the
    # same loop, sharing one settled_job_ids set -- "every driver has real
    # work" is a first-class goal now, not just "no driver is illegally
    # under-minimum." See that function's docstring.
    settled_job_ids = set()  # see _repair_minimum_daily_hours docstring -- stops groups thrashing between drivers
    for _ in range(5):
        changed_repair = _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                       allow_override_days, settled_job_ids=settled_job_ids)
        changed_idle = _rebalance_idle_drivers(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                allow_override_days, settled_job_ids=settled_job_ids)
        if not (changed_repair or changed_idle):
            break

    return jobs


# ==========================================================================
# allocate_by_merit() -- NEW allocation strategy (2026-08-06), built
# alongside allocate() rather than replacing it (Rule 1/10/13: preserve
# existing behaviour, extend rather than rewrite, prove it incrementally
# before switching anything over). See CHANGELOG_AI.md for the full design
# discussion with the project owner. Not yet wired into the UI.
#
# Confirmed design, in order:
#   1. Partition into morning/evening pools (drivers AND jobs) up front,
#      solved mostly independently, instead of one single greedy pass.
#   2. PRE-MERGE, pairs only: two "Same Driver" rows with the same vehicle
#      type and start/end times within 1h of each other are combined into
#      one internal PlanningUnit spanning min(start)-max(end) before
#      allocation ever runs -- this turns the genuinely-simultaneous case
#      (one truck, two orders) into a single decision instead of a
#      runtime overlap-relaxation special case. 3+ rows in one cluster are
#      NOT all merged together -- only ever pairs; a third row falls
#      through to ordinary group-continuity handling. The export always
#      shows each original row separately, all pointing to whichever one
#      driver/vehicle the merged decision produced.
#   3. SEEDING, diversified by EVENT not by raw job: when handing out each
#      driver's first job of the shift, prefer covering a NEW event over
#      giving a second job from an event some other driver already
#      started -- keeps an event's footprint on as few drivers as
#      possible, so it stays easy to peel off to a supplier later if it
#      needs to. A driver who is the ONLY one qualified for a job (license
#      scarcity) takes it immediately whenever there's no timing/ceiling
#      conflict, regardless of the seeding queue.
#   4. FILL: once every driver that can be seeded has been, remaining
#      units go to whoever has the most room relative to their own
#      working_hours_per_day, not just raw list order.
#   5. REARRANGEMENT: working_hours_per_day is the driver's floor once
#      they're used at all; max_working_hours_per_day is the ceiling.
#      Left blank, the ceiling equals working_hours_per_day itself (a
#      fixed day, not a range) -- confirmed by the project owner against
#      the real PLANNED.xlsx, where the only two drivers who land at
#      exactly 9h are precisely the two with a blank max. After the
#      seed+fill pass, the existing gap-fill and minimum-hours repair
#      passes run in a loop as a rearrangement stage, exactly as they do
#      for allocate() -- moving/relocating jobs until every used driver's
#      day is within [working_hours_per_day, ceiling] or the shortfall is
#      genuinely unfixable (released to unresolved, not silently kept
#      illegal).
# ==========================================================================


@dataclass
class PlanningUnit:
    """One decision unit for allocate_by_merit() -- either a single Job, or
    two Jobs pre-merged into one span (see the pairs-only pre-merge rule
    above). Never more than 2 jobs. All hard-rule checks and the
    driver/vehicle decision happen once per unit; the result is then
    copied onto every Job in `jobs` so the export always shows original
    rows individually."""
    jobs: list
    start_dt: datetime
    end_dt: datetime
    vehicle_type_required: str
    same_driver_key: str
    event_id: str

    @property
    def hours(self):
        return (self.end_dt - self.start_dt).total_seconds() / 3600.0


def build_planning_units(jobs):
    """Pairs-only pre-merge: within each non-blank Same Driver group, scan
    jobs (sorted by start time) and greedily pair up the first two that
    share a vehicle type and start/end within 1h of each other. Each job
    is used in at most one pair. Anything left over (ungrouped jobs, or
    grouped jobs that didn't find a merge partner, or a 3rd+ row in a
    bigger cluster) becomes its own single-job PlanningUnit."""
    used_ids = set()
    units = []

    by_group = {}
    for j in jobs:
        gk = _group_key_of(j)
        if gk:
            by_group.setdefault(gk, []).append(j)

    for gk, group_jobs in by_group.items():
        ordered = sorted(group_jobs, key=lambda j: j.start_dt or datetime.min)
        for i, j1 in enumerate(ordered):
            if id(j1) in used_ids:
                continue
            for j2 in ordered[i + 1:]:
                if id(j2) in used_ids:
                    continue
                if not _type_matches(j1.vehicle_type_required, j2.vehicle_type_required):
                    continue
                start_diff_min = abs((j1.start_dt - j2.start_dt).total_seconds()) / 60.0
                end_diff_min = abs((j1.end_dt - j2.end_dt).total_seconds()) / 60.0
                if start_diff_min <= 60 and end_diff_min <= 60:
                    used_ids.add(id(j1))
                    used_ids.add(id(j2))
                    units.append(PlanningUnit(
                        jobs=[j1, j2],
                        start_dt=min(j1.start_dt, j2.start_dt),
                        end_dt=max(j1.end_dt, j2.end_dt),
                        vehicle_type_required=j1.vehicle_type_required,
                        same_driver_key=gk,
                        event_id=j1.event_id,
                    ))
                    break

    for j in jobs:
        if id(j) not in used_ids:
            units.append(PlanningUnit(
                jobs=[j], start_dt=j.start_dt, end_dt=j.end_dt,
                vehicle_type_required=j.vehicle_type_required,
                same_driver_key=_group_key_of(j) or "", event_id=j.event_id,
            ))
    return units


def _shift_of(dt):
    return "evening" if dt.time() >= time(SHIFT_PERIOD_EVENING_CUTOFF_HOUR, 0) else "morning"


def _driver_matches_shift_pool(driver, shift):
    return driver.shift_period is None or driver.shift_period == shift


def _driver_ceiling(driver):
    """working_hours_per_day is the floor once a driver is used at all;
    max_working_hours_per_day is the true ceiling. Left blank, the
    ceiling equals working_hours_per_day itself -- a fixed day, not a
    range (confirmed against the real PLANNED.xlsx: the only drivers who
    land at exactly their working_hours_per_day are precisely the ones
    with no max configured)."""
    if driver.max_working_hours_per_day is not None:
        return driver.max_working_hours_per_day
    return driver.working_hours_per_day


def _unit_driver_feasible(d, unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver):
    """Same hard-rule set as allocate()'s main candidate filter, applied to
    a PlanningUnit instead of a single Job. Returns (True, effective_group_key)
    or (False, None)."""
    if not _driver_qualifies_for_type(d, unit.vehicle_type_required):
        return False, None
    if _driver_is_off(d, unit.start_dt.date(), allow_override_days):
        return False, None
    if not _job_matches_shift_period(unit.start_dt, d.shift_period):
        return False, None
    group_key = unit.same_driver_key or None
    established_vehicle = group_vehicle_by_driver.get((group_key, d.id)) if group_key else None
    vehicle_type_consistent = (
        established_vehicle is None or _type_matches(unit.vehicle_type_required, established_vehicle.vehicle_type)
    )
    effective_group_key = group_key if (group_key and vehicle_type_consistent) else None
    if _overlaps_with_buffer(d.busy_intervals, unit.start_dt, unit.end_dt,
                              travel_buffer_minutes, ignore_group_key=effective_group_key):
        return False, None
    if d.working_hours_per_day is not None:
        projected = _merged_hours(d.busy_intervals + [(unit.start_dt, unit.end_dt)])
        ceiling = _driver_ceiling(d)
        if ceiling is not None and projected > ceiling + 1e-9:
            return False, None
        overtime = max(0.0, projected - d.working_hours_per_day)
        if d.max_overtime_hours_per_month is not None:
            if d.month_overtime_so_far + overtime > d.max_overtime_hours_per_month + 1e-9:
                return False, None
        elif overtime > 0:
            return False, None
    return True, effective_group_key


def _commit_unit(unit, driver, vehicle, effective_group_key, group_drivers, group_vehicle_by_driver, note_tag):
    for j in unit.jobs:
        j.assigned_driver_id = driver.id
        j.assigned_driver_name = driver.name
        j.assigned_vehicle_id = vehicle.id if vehicle else None
        j.assigned_vehicle_plate = vehicle.plate if vehicle else ""
        j.unresolved = False
        note_suffix = " [Same Driver group]" if unit.same_driver_key else ""
        j.assignment_note = f"In-house: {driver.name}{note_suffix} [{note_tag}]"
    driver.busy_intervals.append((unit.start_dt, unit.end_dt, effective_group_key))
    driver.occupied_seconds = _merged_hours(driver.busy_intervals) * 3600.0
    if vehicle:
        vehicle.busy_intervals.append((unit.start_dt, unit.end_dt, effective_group_key))
    if unit.same_driver_key:
        group_drivers.setdefault(unit.same_driver_key, [])
        if driver not in group_drivers[unit.same_driver_key]:
            group_drivers[unit.same_driver_key].append(driver)
        if vehicle:
            group_vehicle_by_driver[(unit.same_driver_key, driver.id)] = vehicle


def _allocate_shift(shift_units, shift_drivers, vehicle_pool, travel_buffer_minutes,
                     allow_override_days, group_drivers, group_vehicle_by_driver):
    """Phases 3+4 for one shift pool: event-diversified seeding (with
    license-scarcity override), then merit-based fill of everything left.
    Returns the list of units that still couldn't be placed in-house."""
    seeded_driver_ids = set()
    claimed_event_ids = set()
    remaining = []

    def qualifiers(unit):
        return [d for d in shift_drivers if _driver_qualifies_for_type(d, unit.vehicle_type_required)]

    for unit in sorted(shift_units, key=lambda u: u.start_dt):
        group_key = unit.same_driver_key or None

        # Continuity first: an already-established group driver who still
        # qualifies always takes the next row in their own group, exactly
        # as in allocate() (SD-002/SD-003) -- this is independent of
        # seeding/fill and always checked first.
        chosen, egk = None, None
        for d in group_drivers.get(group_key, []) if group_key else []:
            ok, e = _unit_driver_feasible(d, unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
            if ok:
                chosen, egk = d, e
                break

        note_tag = "continuity"

        # License scarcity overrides seeding -- the ONLY qualified driver
        # takes the job immediately if free, whether or not they've
        # already been seeded this shift.
        if chosen is None:
            q = qualifiers(unit)
            if len(q) == 1:
                ok, e = _unit_driver_feasible(q[0], unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                if ok:
                    chosen, egk = q[0], e
                    note_tag = "specialist, only qualifier"

        # Seeding: prefer covering a NEW event over a second job from an
        # event someone else already started.
        if chosen is None and unit.event_id and unit.event_id not in claimed_event_ids:
            candidates = []
            for d in qualifiers(unit):
                if d.id in seeded_driver_ids:
                    continue
                ok, e = _unit_driver_feasible(d, unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                if ok:
                    candidates.append((d, e))
            if candidates:
                chosen, egk = min(candidates, key=lambda pair: (-len(pair[0].license_types), pair[0].occupied_seconds))
                note_tag = "seeded, new event"

        if chosen is not None:
            vehicle, found = _find_vehicle_for_unit(unit, vehicle_pool, travel_buffer_minutes, egk)
            if found:
                _commit_unit(unit, chosen, vehicle, egk, group_drivers, group_vehicle_by_driver, note_tag)
                seeded_driver_ids.add(chosen.id)
                if unit.event_id:
                    claimed_event_ids.add(unit.event_id)
                continue
            # driver qualified but no vehicle free -- fall through to fill/supplier
        remaining.append(unit)

    still_remaining = []
    for unit in remaining:
        group_key = unit.same_driver_key or None
        chosen, egk = None, None
        for d in group_drivers.get(group_key, []) if group_key else []:
            ok, e = _unit_driver_feasible(d, unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
            if ok:
                chosen, egk = d, e
                break
        if chosen is None:
            candidates = []
            for d in qualifiers(unit):
                ok, e = _unit_driver_feasible(d, unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                if ok:
                    candidates.append((d, e))
            if candidates:
                # Merit fill: whoever has the most room relative to their
                # own working_hours_per_day (furthest under their target)
                # goes first, specialist-reservation as the tiebreak.
                chosen, egk = min(candidates, key=lambda pair: (pair[0].occupied_seconds, -len(pair[0].license_types)))
        if chosen is not None:
            vehicle, found = _find_vehicle_for_unit(unit, vehicle_pool, travel_buffer_minutes, egk)
            if found:
                _commit_unit(unit, chosen, vehicle, egk, group_drivers, group_vehicle_by_driver, "fill")
                continue
        still_remaining.append(unit)
    return still_remaining


def _find_vehicle_for_unit(unit, vehicle_pool, travel_buffer_minutes, effective_group_key):
    if not _vehicle_type_needs_vehicle(unit.vehicle_type_required):
        return None, True
    for v in vehicle_pool:
        if _type_matches(unit.vehicle_type_required, v.vehicle_type) and not _overlaps_with_buffer(
                v.busy_intervals, unit.start_dt, unit.end_dt,
                travel_buffer_minutes, ignore_group_key=effective_group_key):
            return v, True
    return None, False


def allocate_by_merit(jobs, drivers, vehicles, supplier_offerings,
                       allowed_driver_ids=None, allowed_supplier_ids=None,
                       allow_override_days=None, travel_buffer_minutes=DEFAULT_TRAVEL_BUFFER_MINUTES):
    """The new shift-partitioned, merit-based strategy described above.
    Mutates and returns `jobs`, same contract as allocate(). Not yet wired
    into the UI -- exists so it can be tested and compared against real
    data before anything switches over to it (Rule 13)."""
    allow_override_days = allow_override_days or {}
    driver_pool = [d for d in drivers if allowed_driver_ids is None or d.id in allowed_driver_ids]
    vehicle_pool = [v for v in vehicles if not v.in_workshop]
    offering_pool = [o for o in supplier_offerings if allowed_supplier_ids is None or o.supplier_id in allowed_supplier_ids]

    for j in jobs:
        if j.start_dt is None or j.end_dt is None:
            j.unresolved = True
            j.assignment_note = "Could not parse date/time for this row"

    valid_jobs = [j for j in jobs if j.start_dt is not None and j.end_dt is not None]
    units = build_planning_units(valid_jobs)

    group_drivers = {}
    group_vehicle_by_driver = {}

    leftover_units = []
    for shift in ("morning", "evening"):
        shift_units = [u for u in units if _shift_of(u.start_dt) == shift]
        shift_drivers = [d for d in driver_pool if _driver_matches_shift_pool(d, shift)]
        leftover_units.extend(
            _allocate_shift(shift_units, shift_drivers, vehicle_pool, travel_buffer_minutes,
                             allow_override_days, group_drivers, group_vehicle_by_driver)
        )

    # Supplier fallback for anything the in-house seed+fill couldn't place
    # -- same reuse-before-hire logic as allocate()'s supplier pass,
    # applied per leftover unit (both jobs in a merged pair go to the same
    # hire together, same as the in-house side).
    hires_by_key = {}
    group_supplier_hires = {}
    for unit in sorted(leftover_units, key=lambda u: u.start_dt):
        group_key = unit.same_driver_key or None
        note_suffix = " [Same Driver group]" if group_key else ""
        matching_offerings = [o for o in offering_pool if _type_matches(unit.vehicle_type_required, o.vehicle_type)]
        if not matching_offerings:
            for j in unit.jobs:
                j.unresolved = True
                j.assignment_note = "No qualifying in-house or supplier resource available"
            continue

        reusable_hire = None
        if group_key:
            for hire in group_supplier_hires.get(group_key, []):
                if _type_matches(unit.vehicle_type_required, hire.vehicle_type) and not _overlaps_with_buffer(
                        hire.busy_intervals, unit.start_dt, unit.end_dt, travel_buffer_minutes, ignore_group_key=group_key):
                    reusable_hire = hire
                    break
        if reusable_hire is None:
            for o in matching_offerings:
                key = (o.supplier_id, o.vehicle_type)
                for hire in hires_by_key.get(key, []):
                    if not _overlaps_with_buffer(hire.busy_intervals, unit.start_dt, unit.end_dt,
                                                  travel_buffer_minutes, ignore_group_key=group_key):
                        reusable_hire = hire
                        break
                if reusable_hire:
                    break

        if reusable_hire:
            reusable_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
            label = reusable_hire.label
            supplier_text = f"SAME {label}" if reusable_hire.already_used else label
            reusable_hire.already_used = True
            for j in unit.jobs:
                j.assigned_supplier_unit = supplier_text
                j.assigned_supplier_id = reusable_hire.supplier_id
                j.assignment_note = f"Supplier: {reusable_hire.supplier_name}{note_suffix}"
            if group_key:
                group_supplier_hires.setdefault(group_key, [])
                if reusable_hire not in group_supplier_hires[group_key]:
                    group_supplier_hires[group_key].append(reusable_hire)
            continue

        hireable = []
        for o in matching_offerings:
            key = (o.supplier_id, o.vehicle_type)
            already_hired_count = len(hires_by_key.get(key, []))
            if o.max_available_per_day is None or already_hired_count < o.max_available_per_day:
                hireable.append(o)
        if not hireable:
            for j in unit.jobs:
                j.unresolved = True
                j.assignment_note = "No qualifying in-house or supplier resource available (all suppliers at daily capacity)"
            continue

        chosen_offering = min(hireable, key=lambda o: o.cumulative_hours_history)
        key = (chosen_offering.supplier_id, chosen_offering.vehicle_type)
        instance_number = len(hires_by_key.get(key, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id, supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type, instance_number=instance_number,
        )
        new_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        if group_key:
            group_supplier_hires.setdefault(group_key, []).append(new_hire)
        for j in unit.jobs:
            j.assigned_supplier_unit = new_hire.label
            j.assigned_supplier_id = new_hire.supplier_id
            j.assignment_note = f"Supplier: {new_hire.supplier_name}{note_suffix}"

    # REARRANGEMENT stage (Phase 5): reuse the same safety-net passes
    # allocate() uses, looped until stable -- fills any remaining bounded
    # gaps, moves/relocates under-minimum days (grouped or not) until
    # every used driver is within [working_hours_per_day, ceiling] or the
    # shortfall is genuinely unfixable and released to unresolved, and
    # (2026-08-06) actively rescues any driver left fully idle -- "every
    # driver has real work" is a first-class goal, not just "no driver is
    # illegally under-minimum." See _rebalance_idle_drivers()'s docstring.
    settled_job_ids = set()  # shared across every pass in this loop -- fixed 2026-08-06, was being reset every iteration
    for _ in range(6):
        changed_gap = _fill_gaps_with_unresolved_jobs(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days)
        changed_repair = _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                       allow_override_days, settled_job_ids=settled_job_ids)
        changed_idle = _rebalance_idle_drivers(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                allow_override_days, settled_job_ids=settled_job_ids)
        if not (changed_gap or changed_repair or changed_idle):
            break

    return jobs


# ==========================================================================
# allocate_by_anchor() -- NEW strategy (2026-08-06), built on top of the
# same PlanningUnit machinery as allocate_by_merit(). Confirmed design
# with the project owner:
#   1. Shift partition + pairs-only pre-merge, same as allocate_by_merit.
#   2. ANCHOR each driver's day intentionally, most-constrained (narrowest
#      license) drivers first: give them their earliest-available
#      qualifying job as the FIRST anchor, then compute their target
#      finish time (first job's end + their ceiling) and search for a
#      job ending as close as possible to -- but not after -- that target
#      as the LAST anchor. This sizes a driver's day up front instead of
#      hoping a good shape falls out of a sequence of one-job decisions.
#   3. MIDDLE FILL: everything else goes to whoever it fits, least-
#      occupied-first.
#   4. Bounded SWAP REPAIR (2-3 rounds, capped): for each still-unresolved
#      unit, look for a driver who could take it if ONE of their existing
#      single-job assignments moved elsewhere, and only commit the swap
#      if that displaced job actually finds a legal new home -- a strict,
#      verified improvement, never a net-zero shuffle.
#   5. Supplier fallback, then the same rearrangement safety net
#      (gap-fill + minimum-hours repair + idle-driver rescue) as the
#      other two strategies.
# Goal, set by the project owner: reach ZERO unresolved on the real
# UNPLANNED.xlsx (matching PLANNED.xlsx, which has none) before testing
# against a larger file to see how/when supplier fallback should trigger.
# ==========================================================================


def _driver_ordered_most_constrained_first(shift_drivers):
    return sorted(shift_drivers, key=lambda d: (len(d.license_types), d.id))


def _release_unit(unit, driver, vehicle_pool):
    """Undoes a committed unit's effect on a driver (and any vehicle used
    for it), matched by (start, end) since a unit's tag can vary. Leaves
    the underlying Jobs unassigned -- caller is expected to immediately
    re-commit them somewhere, this is never a final state on its own."""
    driver.busy_intervals = [iv for iv in driver.busy_intervals if not (iv[0] == unit.start_dt and iv[1] == unit.end_dt)]
    driver.occupied_seconds = _merged_hours(driver.busy_intervals) * 3600.0
    for j in unit.jobs:
        if j.assigned_vehicle_id is not None:
            v = next((vv for vv in vehicle_pool if vv.id == j.assigned_vehicle_id), None)
            if v:
                v.busy_intervals = [iv for iv in v.busy_intervals if not (iv[0] == unit.start_dt and iv[1] == unit.end_dt)]
        j.assigned_driver_id = None
        j.assigned_driver_name = ""
        j.assigned_vehicle_id = None
        j.assigned_vehicle_plate = ""


def _anchor_and_fill_shift(shift_units, shift_drivers, vehicle_pool, travel_buffer_minutes,
                            allow_override_days, group_drivers, group_vehicle_by_driver):
    """Phases 2-3 for one shift pool: anchor each driver's first and last
    job (most-constrained drivers first), then fill everything else.
    Returns the units still unplaced."""
    remaining = list(shift_units)

    for driver in _driver_ordered_most_constrained_first(shift_drivers):
        candidates = sorted(
            [u for u in remaining if _driver_qualifies_for_type(driver, u.vehicle_type_required)],
            key=lambda u: u.start_dt,
        )
        first_pick = None
        for u in candidates:
            ok, egk = _unit_driver_feasible(driver, u, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
            if ok:
                first_pick = (u, egk)
                break
        if first_pick is None:
            continue  # nothing at all fits this driver right now -- left for the fill phase
        unit1, egk1 = first_pick
        vehicle1, found1 = _find_vehicle_for_unit(unit1, vehicle_pool, travel_buffer_minutes, egk1)
        if not found1:
            continue
        _commit_unit(unit1, driver, vehicle1, egk1, group_drivers, group_vehicle_by_driver, "anchor-first")
        remaining.remove(unit1)

        ceiling = _driver_ceiling(driver)
        if ceiling is None:
            continue
        free_from = unit1.end_dt
        target_end = free_from + timedelta(hours=ceiling)

        best_last, best_end = None, None
        for u in remaining:
            if u.start_dt < free_from or u.end_dt > target_end + timedelta(minutes=1):
                continue
            if not _driver_qualifies_for_type(driver, u.vehicle_type_required):
                continue
            ok, egk = _unit_driver_feasible(driver, u, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
            if not ok:
                continue
            if best_end is None or u.end_dt > best_end:
                best_last, best_end = (u, egk), u.end_dt
        if best_last is not None:
            unit2, egk2 = best_last
            vehicle2, found2 = _find_vehicle_for_unit(unit2, vehicle_pool, travel_buffer_minutes, egk2)
            if found2:
                _commit_unit(unit2, driver, vehicle2, egk2, group_drivers, group_vehicle_by_driver, "anchor-last")
                remaining.remove(unit2)

    progressed = True
    while progressed:
        progressed = False
        for u in sorted(remaining, key=lambda u: u.start_dt):
            group_key = u.same_driver_key or None
            chosen, egk = None, None
            for d in (group_drivers.get(group_key, []) if group_key else []):
                if d not in shift_drivers:
                    continue
                ok, e = _unit_driver_feasible(d, u, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                if ok:
                    chosen, egk = d, e
                    break
            if chosen is None:
                cands = []
                for d in shift_drivers:
                    if not _driver_qualifies_for_type(d, u.vehicle_type_required):
                        continue
                    ok, e = _unit_driver_feasible(d, u, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                    if ok:
                        cands.append((d, e))
                if cands:
                    chosen, egk = min(cands, key=lambda pair: (pair[0].occupied_seconds, -len(pair[0].license_types)))
            if chosen is not None:
                v, found = _find_vehicle_for_unit(u, vehicle_pool, travel_buffer_minutes, egk)
                if found:
                    _commit_unit(u, chosen, v, egk, group_drivers, group_vehicle_by_driver, "middle-fill")
                    remaining.remove(u)
                    progressed = True
    return remaining


def _swap_repair(units, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days,
                  group_drivers, group_vehicle_by_driver, max_rounds=3):
    """Bounded local search: for each still-unresolved unit, look for a
    driver who could take it if exactly ONE of their existing single-job,
    ungrouped units moved elsewhere -- and only commits the swap if that
    displaced unit actually finds a legal new home. Never a net-zero
    shuffle; every committed swap strictly reduces the unresolved count
    by one. Capped at max_rounds full passes, since each pass is at worst
    O(units x drivers x drivers)."""
    def is_unresolved(u):
        return u.jobs[0].unresolved

    def current_driver_id(u):
        return u.jobs[0].assigned_driver_id

    for _ in range(max_rounds):
        progressed = False
        for U in [u for u in units if is_unresolved(u)]:
            placed = False
            for D in driver_pool:
                if not _driver_qualifies_for_type(D, U.vehicle_type_required):
                    continue
                D_units = sorted(
                    [u for u in units if current_driver_id(u) == D.id and not u.same_driver_key and len(u.jobs) == 1],
                    key=lambda u: u.hours,
                )
                for J in D_units:
                    reduced = [iv for iv in D.busy_intervals if not (iv[0] == J.start_dt and iv[1] == J.end_dt)]
                    if _overlaps_with_buffer(reduced, U.start_dt, U.end_dt, travel_buffer_minutes):
                        continue
                    if D.working_hours_per_day is not None:
                        ceiling = _driver_ceiling(D)
                        projected = _merged_hours(reduced + [(U.start_dt, U.end_dt)])
                        if ceiling is not None and projected > ceiling + 1e-9:
                            continue
                    if _driver_is_off(D, U.start_dt.date(), allow_override_days) or not _job_matches_shift_period(U.start_dt, D.shift_period):
                        continue
                    new_home = None
                    for E in driver_pool:
                        if E.id == D.id or not _driver_qualifies_for_type(E, J.vehicle_type_required):
                            continue
                        ok, egk = _unit_driver_feasible(E, J, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                        if ok:
                            new_home = (E, egk)
                            break
                    if new_home is None:
                        continue
                    E, egk_j = new_home
                    v_j, found_v_j = _find_vehicle_for_unit(J, vehicle_pool, travel_buffer_minutes, egk_j)
                    if not found_v_j:
                        continue
                    v_u, found_v_u = _find_vehicle_for_unit(U, vehicle_pool, travel_buffer_minutes, None)
                    if not found_v_u:
                        continue
                    _release_unit(J, D, vehicle_pool)
                    _commit_unit(J, E, v_j, egk_j, group_drivers, group_vehicle_by_driver, "swap-relocated")
                    _commit_unit(U, D, v_u, None, group_drivers, group_vehicle_by_driver, "swap-placed")
                    for jb in J.jobs + U.jobs:
                        jb.unresolved = False
                    placed = True
                    progressed = True
                    break
                if placed:
                    break
            # if not placed, U stays unresolved for this round
        if not progressed:
            break
    return units


def allocate_by_anchor(jobs, drivers, vehicles, supplier_offerings,
                        allowed_driver_ids=None, allowed_supplier_ids=None,
                        allow_override_days=None, travel_buffer_minutes=DEFAULT_TRAVEL_BUFFER_MINUTES,
                        swap_rounds=3):
    """The anchor-first-and-last-job strategy described above. Mutates and
    returns `jobs`, same contract as allocate()/allocate_by_merit(). Not
    yet wired into the UI -- built to be tested and compared on real data
    first (Rule 13)."""
    allow_override_days = allow_override_days or {}
    driver_pool = [d for d in drivers if allowed_driver_ids is None or d.id in allowed_driver_ids]
    vehicle_pool = [v for v in vehicles if not v.in_workshop]
    offering_pool = [o for o in supplier_offerings if allowed_supplier_ids is None or o.supplier_id in allowed_supplier_ids]

    for j in jobs:
        if j.start_dt is None or j.end_dt is None:
            j.unresolved = True
            j.assignment_note = "Could not parse date/time for this row"

    valid_jobs = [j for j in jobs if j.start_dt is not None and j.end_dt is not None]
    units = build_planning_units(valid_jobs)

    group_drivers = {}
    group_vehicle_by_driver = {}

    leftover = []
    for shift in ("morning", "evening"):
        shift_units = [u for u in units if _shift_of(u.start_dt) == shift]
        shift_drivers = [d for d in driver_pool if _driver_matches_shift_pool(d, shift)]
        leftover.extend(
            _anchor_and_fill_shift(shift_units, shift_drivers, vehicle_pool, travel_buffer_minutes,
                                    allow_override_days, group_drivers, group_vehicle_by_driver)
        )

    for u in leftover:
        for j in u.jobs:
            j.unresolved = True
            j.assignment_note = "No qualifying in-house resource available yet (pre-swap)"

    _swap_repair(units, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days,
                 group_drivers, group_vehicle_by_driver, max_rounds=swap_rounds)

    # Supplier fallback for whatever's still unresolved after swap repair.
    hires_by_key = {}
    group_supplier_hires = {}
    still_unresolved_units = [u for u in units if u.jobs[0].unresolved]
    for unit in sorted(still_unresolved_units, key=lambda u: u.start_dt):
        group_key = unit.same_driver_key or None
        note_suffix = " [Same Driver group]" if group_key else ""
        matching_offerings = [o for o in offering_pool if _type_matches(unit.vehicle_type_required, o.vehicle_type)]
        if not matching_offerings:
            for j in unit.jobs:
                j.unresolved = True
                j.assignment_note = "No qualifying in-house or supplier resource available"
            continue
        reusable_hire = None
        if group_key:
            for hire in group_supplier_hires.get(group_key, []):
                if _type_matches(unit.vehicle_type_required, hire.vehicle_type) and not _overlaps_with_buffer(
                        hire.busy_intervals, unit.start_dt, unit.end_dt, travel_buffer_minutes, ignore_group_key=group_key):
                    reusable_hire = hire
                    break
        if reusable_hire is None:
            for o in matching_offerings:
                key = (o.supplier_id, o.vehicle_type)
                for hire in hires_by_key.get(key, []):
                    if not _overlaps_with_buffer(hire.busy_intervals, unit.start_dt, unit.end_dt,
                                                  travel_buffer_minutes, ignore_group_key=group_key):
                        reusable_hire = hire
                        break
                if reusable_hire:
                    break
        if reusable_hire:
            reusable_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
            label = reusable_hire.label
            supplier_text = f"SAME {label}" if reusable_hire.already_used else label
            reusable_hire.already_used = True
            for j in unit.jobs:
                j.assigned_supplier_unit = supplier_text
                j.assigned_supplier_id = reusable_hire.supplier_id
                j.assignment_note = f"Supplier: {reusable_hire.supplier_name}{note_suffix}"
                j.unresolved = False
            if group_key:
                group_supplier_hires.setdefault(group_key, [])
                if reusable_hire not in group_supplier_hires[group_key]:
                    group_supplier_hires[group_key].append(reusable_hire)
            continue
        hireable = []
        for o in matching_offerings:
            key = (o.supplier_id, o.vehicle_type)
            already_hired_count = len(hires_by_key.get(key, []))
            if o.max_available_per_day is None or already_hired_count < o.max_available_per_day:
                hireable.append(o)
        if not hireable:
            for j in unit.jobs:
                j.unresolved = True
                j.assignment_note = "No qualifying in-house or supplier resource available (all suppliers at daily capacity)"
            continue
        chosen_offering = min(hireable, key=lambda o: o.cumulative_hours_history)
        key = (chosen_offering.supplier_id, chosen_offering.vehicle_type)
        instance_number = len(hires_by_key.get(key, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id, supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type, instance_number=instance_number,
        )
        new_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        if group_key:
            group_supplier_hires.setdefault(group_key, []).append(new_hire)
        for j in unit.jobs:
            j.assigned_supplier_unit = new_hire.label
            j.assigned_supplier_id = new_hire.supplier_id
            j.assignment_note = f"Supplier: {new_hire.supplier_name}{note_suffix}"
            j.unresolved = False

    settled_job_ids = set()
    for _ in range(6):
        changed_gap = _fill_gaps_with_unresolved_jobs(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days)
        changed_repair = _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                       allow_override_days, settled_job_ids=settled_job_ids)
        changed_idle = _rebalance_idle_drivers(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                allow_override_days, settled_job_ids=settled_job_ids)
        if not (changed_gap or changed_repair or changed_idle):
            break

    return jobs

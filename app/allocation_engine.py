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
    # in_workshop dropped from this check 2026-08-14 -- deprecated
    # alongside the Vehicles tab's old separate "In Workshop" toggle
    # (see db.py's _MIGRATIONS comment). excluded_from_planning, driven
    # by the tab's single Active/Deactive checkbox, is now the only
    # thing that makes a vehicle unavailable for planning.
    return [
        VehicleProfile(id=row["id"], plate=row["plate"], vehicle_type=row["vehicle_type"],
                        in_workshop=bool(row["excluded_from_planning"]))
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


def _job_matches_shift_period(job_start_dt, shift_period, busy_intervals=None):
    """True if this job is allowed for a driver with the given shift_period.
    shift_period is 'morning', 'evening', or None (no restriction -- same
    fail-open behaviour as every other optional hard-rule field here).

    HR-002 addendum (confirmed against a real human-planned day, where a
    'morning' driver was routinely given jobs running into the afternoon
    as a natural continuation of a day already under way -- e.g. a job
    07:00-15:00 followed by another at 16:00-19:00 for the same 'morning'
    driver): shift_period is NOT a wall across the driver's whole day, it
    only gates which half of the day their FIRST job can start in. Once a
    driver already has any job on this same calendar date, later jobs that
    day are governed purely by the normal overlap/hour-ceiling rules, not
    re-checked against the shift window -- exactly the project owner's own
    framing: "if a driver is in morning shift 07:00 that means his shift
    will end 16:00 if the 12hr max field is empty... he can definitely get
    a job or two after 12:00." busy_intervals (the driver's existing
    committed intervals, real or a hypothetical override) is what "already
    has a job that day" is checked against; passing None preserves the
    original always-enforce behaviour, so any call site not yet updated to
    pass real state fails safe rather than silently getting the relaxation."""
    if shift_period is None:
        return True
    if busy_intervals:
        job_date = job_start_dt.date()
        if any(iv[0].date() == job_date for iv in busy_intervals):
            return True  # already working this day -- the window only gates the FIRST job
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
            if not _job_matches_shift_period(job.start_dt, d.shift_period, busy_intervals=d.busy_intervals):
                continue
            if not _driver_has_bounded_gap_fit(d, job.start_dt, job.end_dt, travel_buffer_minutes):
                continue  # only interested in a genuine gap-fill here
            if _overlaps_with_buffer(d.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes):
                continue
            if d.working_hours_per_day is not None:
                same_day = _same_day_intervals(d.busy_intervals, job.start_dt.date()) + [(job.start_dt, job.end_dt)]
                projected_span = _day_span_hours(same_day)
                ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                if projected_span > ceiling + 1e-9:
                    continue
                # Monthly overtime BUDGET is ALSO span-based (2026-08-10,
                # confirmed directly by the project owner): working_hours_per_day
                # is the total legal daily hours; any SPAN beyond it is
                # overtime, deducted from max_overtime_hours_per_month. (An
                # earlier version of this fix tried sum-based overtime
                # instead -- reverted; the project owner clarified overtime
                # tracks the same duty-span concept as the daily ceiling,
                # not summed job duration. A leftover duplicate line from
                # that reverted attempt silently overwrote this correct
                # span-based value with a reference to an undefined
                # `projected_sum` variable -- a live NameError landmine,
                # found and removed 2026-08-14, Phase 22.)
                overtime = max(0.0, projected_span - d.working_hours_per_day)
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
        # not: span_hours has to reflect the driver's true day, even
        # though only the ungrouped subset is ever movable (see below).
        # Duty SPAN (2026-08-10 correction, confirmed directly by the
        # project owner with a concrete example) -- first job's start to
        # last job's end, NOT summed job duration. Summed duration
        # ("hours worked") has no hard rule of its own; it remains purely
        # a fairness/tie-breaking figure (see occupied_seconds elsewhere
        # in this module) and must never again decide legality here.
        all_day_jobs = [
            j for j in jobs
            if j.assigned_driver_id == driver_id and j.start_dt is not None and j.start_dt.date() == day
        ]
        span_hours = _day_span_hours([(j.start_dt, j.end_dt) for j in all_day_jobs])
        if span_hours <= 1e-9 or span_hours >= driver.working_hours_per_day - 1e-9:
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
            combined_busy = d.busy_intervals + tentative_intervals.get(d.id, [])
            if not _job_matches_shift_period(job.start_dt, d.shift_period, busy_intervals=combined_busy):
                return False, None
            established_type = tentative_group_vehicle_for(d.id, group_key) if group_key else None
            vehicle_type_consistent = established_type is None or _type_matches(job.vehicle_type_required, established_type)
            effective_group_key = group_key if (group_key and vehicle_type_consistent) else None
            if _overlaps_with_buffer(combined_busy, job.start_dt, job.end_dt,
                                      travel_buffer_minutes, ignore_group_key=effective_group_key):
                return False, None
            if d.working_hours_per_day is not None:
                projected_span = _day_span_hours(combined_busy + [(job.start_dt, job.end_dt)])
                ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                if projected_span > ceiling + 1e-9:
                    return False, None
                # Monthly overtime BUDGET is span-based too (2026-08-10) --
                # same metric as the ceiling just above.
                overtime = max(0.0, projected_span - d.working_hours_per_day)
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
            # Matches by (start, end) only, NOT the group tag -- a real bug
            # found here: the interval was originally stored with the
            # EFFECTIVE group key (which SD-004's vehicle-consistency rule
            # can set to None even for a job that DOES have a same_driver_key,
            # if its vehicle type didn't match what the group had already
            # established). Matching against _group_key_of(job) -- the RAW
            # key -- could silently fail to find the real stored tuple,
            # leaving a phantom busy interval behind on the driver or
            # vehicle forever (confirmed: a vehicle showed a busy interval
            # with no job behind it at all, permanently blocking that slot).
            # (start, end)-only matching is what _release_unit already uses
            # everywhere else in this module, and is safe here for the same
            # reason: this function only ever touches UNGROUPED jobs (see
            # the group-day skip above), and two different ungrouped jobs
            # can never legitimately share an identical (start, end) on the
            # same driver in the first place.
            driver.busy_intervals = [iv for iv in driver.busy_intervals if not (iv[0] == job.start_dt and iv[1] == job.end_dt)]
            driver.occupied_seconds = _merged_hours(driver.busy_intervals) * 3600.0
            old_vehicle = vehicle_by_id.get(job.assigned_vehicle_id)
            if old_vehicle:
                old_vehicle.busy_intervals = [iv for iv in old_vehicle.busy_intervals if not (iv[0] == job.start_dt and iv[1] == job.end_dt)]

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
                    f"Below {driver.name}'s minimum daily span ({span_hours:.1f}h < "
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
        if not _job_matches_shift_period(job.start_dt, idle.shift_period, busy_intervals=accumulated):
            return False
        if _overlaps_with_buffer(accumulated, job.start_dt, job.end_dt, travel_buffer_minutes):
            return False
        ceiling = _driver_ceiling(idle)
        projected_span = _day_span_hours([(s, e) for s, e, _ in accumulated] + [(job.start_dt, job.end_dt)])
        if ceiling is not None and projected_span > ceiling + 1e-9:
            return False
        return True

    for idle in idle_drivers:
        minimum = idle.working_hours_per_day
        accumulated = []          # tentative (start, end, None) intervals for this idle driver
        planned = []               # [{'job':, 'kind': 'unresolved'|'donor', 'donor':}]
        tentative_donor_removed = {}  # donor.id -> [(start,end), ...] tentatively pulled this batch

        picked_job_ids = set()

        for _ in range(20):  # hard cap, real datasets are nowhere near this
            if _day_span_hours([(s, e) for s, e, _ in accumulated]) >= minimum - 1e-9:
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
                        # Duty SPAN of what the donor would be left with if
                        # this specific job were pulled away -- NOT summed
                        # duration (2026-08-10 correction). Removing a job
                        # that isn't one of the donor's bounding first/last
                        # jobs doesn't shrink their span at all; removing a
                        # bounding one might. Either way, span is the right
                        # thing to check against their own minimum here.
                        remaining_span = _day_span_hours([
                            (x.start_dt, x.end_dt) for x in donor_all_jobs if x is not job
                        ])
                        if 1e-9 < remaining_span < donor.working_hours_per_day - 1e-9:
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

        total_span = _day_span_hours([(s, e) for s, e, _ in accumulated])
        if total_span < minimum - 1e-9:
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


# --- Duty-span hard-rule correction (2026-08-10) --------------------------
# The daily working_hours_per_day / max_working_hours_per_day hard rule was,
# until this fix, checked against _merged_hours() above -- the SUM of a
# driver's actual job durations (deduplicated for overlaps). The project
# owner corrected this directly, with a concrete example: a driver with
# three jobs (2h + 3h + 3h = 8h of "hours worked") should NOT be blocked
# from having that day be legal, or forced to pick up more work to reach
# 9h of SUMMED duration -- the 9h/12h hard rule is measured against the
# driver's duty SPAN instead: strictly the time from their FIRST job's
# start to their LAST job's end, regardless of how much actual job time
# falls inside that window or how large the gaps between jobs are. Summed
# job duration ("hours worked") has NO hard rule of its own at all -- it
# remains exactly what it was for fairness/tie-breaking purposes (see
# occupied_seconds, still computed via _merged_hours, still the basis for
# least-occupied-first candidate ranking and the solver's balance
# objective), but it must never again be compared against
# working_hours_per_day/max_working_hours_per_day to decide legality.
# This was, in effect, the project's own long-standing open OPT-001
# "duty span" question (first raised 2026-08-03, left undecided ever
# since) -- now resolved directly by the project owner: span for the hard
# rule, summed duration for fairness only, never the reverse.
def _day_span_hours(intervals):
    """The driver's duty SPAN for a set of same-day intervals: from the
    EARLIEST start to the LATEST end, not the summed/merged duration of
    the intervals themselves (contrast with _merged_hours above, which
    remains the correct function for fairness/tie-breaking purposes).
    Accepts 2-tuples (start, end) or longer; anything past the first two
    elements is ignored."""
    if not intervals:
        return 0.0
    starts = [iv[0] for iv in intervals]
    ends = [iv[1] for iv in intervals]
    return (max(ends) - min(starts)).total_seconds() / 3600.0


def _same_day_intervals(intervals, the_date):
    """Filters a driver's busy_intervals down to only those STARTING on
    the given calendar date -- the same day-grouping convention already
    used elsewhere in this module (e.g. _repair_minimum_daily_hours)."""
    return [iv for iv in intervals if iv[0].date() == the_date]


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
    # Separate from hires_by_key (which stays scoped by (supplier, vehicle
    # type) for correct reuse-matching -- a hire tied to one vehicle type
    # must never be reused for a job needing a different type): this tracks
    # every hire of a given supplier across ALL vehicle types today, so the
    # displayed unit numbering ("Name", "Name 1", "Name 2"...) stays unique
    # per supplier for the whole day. Bug fixed 2026-08-15 (Phase 29d): see
    # the matching comment in the other three allocation strategies for the
    # full writeup.
    hires_by_supplier = {}

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
            if not _job_matches_shift_period(job.start_dt, d.shift_period, busy_intervals=d.busy_intervals):
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
                projected_today_span = _day_span_hours(d.busy_intervals + [(job.start_dt, job.end_dt)])
                # Hard daily ceiling -- applies regardless of how much monthly
                # overtime allowance remains. See HR-002 rework note above.
                # Measured against duty SPAN (first job start to last job
                # end), not summed job duration -- see the 2026-08-10
                # correction note above _day_span_hours().
                daily_ceiling = d.max_working_hours_per_day if d.max_working_hours_per_day is not None else d.working_hours_per_day
                if projected_today_span > daily_ceiling + 1e-9:
                    continue
                # Monthly overtime BUDGET is ALSO span-based (2026-08-10,
                # confirmed directly by the project owner) -- overtime is
                # any SPAN beyond working_hours_per_day, deducted from
                # max_overtime_hours_per_month, same metric as the ceiling
                # just above.
                projected_today_overtime = max(0.0, projected_today_span - d.working_hours_per_day)
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
        instance_number = len(hires_by_supplier.get(chosen_offering.supplier_id, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id,
            supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type,
            instance_number=instance_number,
        )
        new_hire.busy_intervals.append((job.start_dt, job.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        hires_by_supplier.setdefault(chosen_offering.supplier_id, []).append(new_hire)
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


def _unit_driver_feasible(d, unit, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver,
                           busy_intervals_override=None):
    """Same hard-rule set as allocate()'s main candidate filter, applied to
    a PlanningUnit instead of a single Job. Returns (True, effective_group_key)
    or (False, None).

    busy_intervals_override: when given, checked INSTEAD of d.busy_intervals --
    used by _swap_repair's whole-bundle feasibility check, which needs to ask
    "would this fit against a hypothetical (reduced, or tentatively-extended)
    set of intervals" without mutating the real driver yet."""
    if not _driver_qualifies_for_type(d, unit.vehicle_type_required):
        return False, None
    if _driver_is_off(d, unit.start_dt.date(), allow_override_days):
        return False, None
    busy_intervals = d.busy_intervals if busy_intervals_override is None else busy_intervals_override
    if not _job_matches_shift_period(unit.start_dt, d.shift_period, busy_intervals=busy_intervals):
        return False, None
    group_key = unit.same_driver_key or None
    established_vehicle = group_vehicle_by_driver.get((group_key, d.id)) if group_key else None
    vehicle_type_consistent = (
        established_vehicle is None or _type_matches(unit.vehicle_type_required, established_vehicle.vehicle_type)
    )
    effective_group_key = group_key if (group_key and vehicle_type_consistent) else None
    if _overlaps_with_buffer(busy_intervals, unit.start_dt, unit.end_dt,
                              travel_buffer_minutes, ignore_group_key=effective_group_key):
        return False, None
    if d.working_hours_per_day is not None:
        projected_span = _day_span_hours(busy_intervals + [(unit.start_dt, unit.end_dt)])
        ceiling = _driver_ceiling(d)
        if ceiling is not None and projected_span > ceiling + 1e-9:
            return False, None
        # Monthly overtime BUDGET is span-based too (2026-08-10) -- same
        # metric as the ceiling just above.
        overtime = max(0.0, projected_span - d.working_hours_per_day)
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


def _find_vehicle_for_unit(unit, vehicle_pool, travel_buffer_minutes, effective_group_key, extra_busy=None):
    """extra_busy: optional {vehicle_id: [(start, end, group_key), ...]} of
    tentative reservations not yet written onto the real VehicleProfile
    objects -- used when checking a whole bundle of units against one
    candidate vehicle pool without mutating anything until every unit in
    the bundle is confirmed feasible."""
    if not _vehicle_type_needs_vehicle(unit.vehicle_type_required):
        return None, True
    extra_busy = extra_busy or {}
    for v in vehicle_pool:
        combined = v.busy_intervals + extra_busy.get(v.id, [])
        if _type_matches(unit.vehicle_type_required, v.vehicle_type) and not _overlaps_with_buffer(
                combined, unit.start_dt, unit.end_dt,
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
    # Separate from hires_by_key (which stays scoped by (supplier, vehicle
    # type) for correct reuse-matching -- a hire tied to one vehicle type
    # must never be reused for a job needing a different type): this tracks
    # every hire of a given supplier across ALL vehicle types today, so the
    # displayed unit numbering ("Name", "Name 1", "Name 2"...) stays unique
    # per supplier for the whole day. Bug fixed 2026-08-15 (Phase 29d):
    # instance_number used to be computed from hires_by_key alone, so hiring
    # the same supplier for two DIFFERENT vehicle types on the same day gave
    # both hires the identical unnumbered label (each was "1st" within its
    # own type bucket) -- two genuinely different physical units displayed
    # as if they were the same one, confirmed via a real run and reported by
    # the project owner as a ReCheck-flagged "vehicle clash" that turned out
    # to be a real label collision, not a false positive.
    hires_by_supplier = {}
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
        instance_number = len(hires_by_supplier.get(chosen_offering.supplier_id, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id, supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type, instance_number=instance_number,
        )
        new_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        hires_by_supplier.setdefault(chosen_offering.supplier_id, []).append(new_hire)
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


def _bundle_units_for_driver(units, driver_id):
    """Every displaceable 'bundle' currently sitting on this driver: each
    ungrouped unit stands alone as its own 1-item bundle, and each distinct
    Same-Driver group this driver is part of becomes ONE multi-item bundle
    covering every unit that group has on this driver. Grouped work is
    always displaced as a whole, never split -- the same whole-group-move
    principle already established for HR-005 (_repair_minimum_daily_hours):
    partially relocating a flagged group would just create a different,
    equally-illegal shortfall elsewhere rather than fixing anything.
    Smallest-hours-first, since displacing less is more likely to still
    leave room for everything else on this driver's day."""
    driver_units = [u for u in units if u.jobs[0].assigned_driver_id == driver_id]
    bundles = []
    seen_groups = set()
    for u in driver_units:
        if not u.same_driver_key:
            bundles.append([u])
        elif u.same_driver_key not in seen_groups:
            seen_groups.add(u.same_driver_key)
            bundles.append([x for x in driver_units if x.same_driver_key == u.same_driver_key])
    bundles.sort(key=lambda b: sum(x.hours for x in b))
    return bundles


def _bundle_fits_driver(bundle, E, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver, start_busy=None):
    """Checks whether an ENTIRE bundle (a single ungrouped unit, or every
    unit belonging to one Same-Driver group) could be committed to driver E
    all together -- each unit's feasibility checked against a starting
    schedule (E's real one by default, or a hypothetical reduced one if
    start_busy is given -- used by the chain search below to ask "would
    this fit on E if E's own bundle X were moved out of the way first")
    PLUS every earlier unit in this same bundle already tentatively
    placed, so two individually-legal-looking moves can't jointly bust a
    daily ceiling the way a naive one-at-a-time check could miss. Returns a
    list of (unit, effective_group_key) in commit order on success, or None
    if any unit in the bundle can't fit."""
    tentative = list(E.busy_intervals) if start_busy is None else list(start_busy)
    plan = []
    for u in sorted(bundle, key=lambda x: x.start_dt):
        ok, egk = _unit_driver_feasible(E, u, allow_override_days, travel_buffer_minutes,
                                         group_vehicle_by_driver, busy_intervals_override=tentative)
        if not ok:
            return None
        tentative = tentative + [(u.start_dt, u.end_dt, egk)]
        plan.append((u, egk))
    return plan


def _find_vehicles_for_bundle(plan, vehicle_pool, travel_buffer_minutes):
    """Finds a vehicle for every (unit, effective_group_key) in a bundle
    plan, tracking tentative reservations across the bundle so two units in
    the same plan don't silently double-book one physical vehicle unless
    their group tag explicitly allows the overlap. Returns a list of
    (unit, vehicle_or_None) on success, or None if any unit can't get one."""
    extra_busy = {}
    result = []
    for u, egk in plan:
        v, found = _find_vehicle_for_unit(u, vehicle_pool, travel_buffer_minutes, egk, extra_busy=extra_busy)
        if not found:
            return None
        if v is not None:
            extra_busy.setdefault(v.id, []).append((u.start_dt, u.end_dt, egk))
        result.append((u, v))
    return result


SWAP_REPAIR_CHAIN_DEPTH = 2  # bounded augmenting-path depth for _try_place_bundle_chain --
                              # small dataset (dozens of jobs, ~11 drivers), so even this is
                              # cheap; each extra hop covers one more "driver who's only
                              # blocked because THEY'RE blocking someone else" layer.


def _try_place_bundle_chain(bundle, units, driver_pool, vehicle_pool, allow_override_days, travel_buffer_minutes,
                             group_vehicle_by_driver, visited_driver_ids, depth_remaining):
    """Bounded-depth augmenting-path search (the same idea used in classic
    bipartite-matching/assignment algorithms): finds a legal home for
    `bundle` (a single unit, or a whole Same-Driver group -- see
    _bundle_units_for_driver), either by placing it directly onto some
    not-yet-visited driver with genuine room, or -- if depth_remaining > 0
    -- by displacing one of THAT driver's own bundles and recursively
    finding a home for the displaced bundle too, chaining through as many
    drivers as depth_remaining allows. Each driver can appear at most once
    per chain (visited_driver_ids), so this can never cycle and is always
    bounded by min(depth_remaining, len(driver_pool)).

    This exists because a single-hop swap (_swap_repair's main loop) can
    fail even when a fix genuinely exists: sometimes driver E could take
    the displaced bundle, but only if ONE of E's own jobs moved to a THIRD
    driver first -- a two-hop chain, invisible to a search that only ever
    tries "does it fit right now."

    Returns a list of (bundle, target_driver, plan, vehicle_plan) describing
    every relocation needed, outermost bundle first, or None if no chain up
    to this depth works. Purely a search -- nothing is mutated. The caller
    only commits once the ENTIRE returned chain has been confirmed
    feasible end to end (see _commit_chain)."""
    for E in driver_pool:
        if E.id in visited_driver_ids:
            continue
        plan = _bundle_fits_driver(bundle, E, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
        if plan is None:
            continue
        vehicle_plan = _find_vehicles_for_bundle(plan, vehicle_pool, travel_buffer_minutes)
        if vehicle_plan is not None:
            return [(bundle, E, plan, vehicle_plan)]

    if depth_remaining <= 0:
        return None

    for E in driver_pool:
        if E.id in visited_driver_ids:
            continue
        for E_bundle in _bundle_units_for_driver(units, E.id):
            reduced = [
                iv for iv in E.busy_intervals
                if not any(iv[0] == x.start_dt and iv[1] == x.end_dt for x in E_bundle)
            ]
            plan = _bundle_fits_driver(bundle, E, allow_override_days, travel_buffer_minutes,
                                        group_vehicle_by_driver, start_busy=reduced)
            if plan is None:
                continue
            vehicle_plan = _find_vehicles_for_bundle(plan, vehicle_pool, travel_buffer_minutes)
            if vehicle_plan is None:
                continue
            rest = _try_place_bundle_chain(E_bundle, units, driver_pool, vehicle_pool, allow_override_days,
                                            travel_buffer_minutes, group_vehicle_by_driver,
                                            visited_driver_ids | {E.id}, depth_remaining - 1)
            if rest is not None:
                return [(bundle, E, plan, vehicle_plan)] + rest
    return None


def _commit_chain(chain, driver_pool, vehicle_pool, group_drivers, group_vehicle_by_driver):
    """Commits every relocation found by _try_place_bundle_chain, in order:
    releases every bundle from wherever it's CURRENTLY sitting first (read
    fresh off the job's own assigned_driver_id -- never stored ahead of
    time, since nothing has moved yet when this starts), then lands every
    bundle on its new home. Releasing everything before committing anything
    keeps a mid-chain slot from looking falsely occupied by a bundle that's
    about to vacate it anyway."""
    driver_by_id = {d.id: d for d in driver_pool}
    for bundle, target_driver, plan, vehicle_plan in chain:
        current_driver = driver_by_id[bundle[0].jobs[0].assigned_driver_id]
        for x in bundle:
            _release_unit(x, current_driver, vehicle_pool)
    for bundle, target_driver, plan, vehicle_plan in chain:
        for x, v in vehicle_plan:
            egk = next(e for (uu, e) in plan if uu is x)
            _commit_unit(x, target_driver, v, egk, group_drivers, group_vehicle_by_driver, "chain-swap-relocated")
        for x in bundle:
            for jb in x.jobs:
                jb.unresolved = False


def _swap_repair(units, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days,
                  group_drivers, group_vehicle_by_driver, max_rounds=3):
    """Bounded local search: for each still-unresolved unit U, look for a
    driver D who could take U if one existing 'bundle' of D's work moved
    elsewhere -- a bundle being either a single ungrouped unit, or a WHOLE
    Same-Driver group D is part of (never split, see _bundle_units_for_driver)
    -- and only commits the swap if that entire displaced bundle actually
    finds one legal new home, driver AND vehicle(s), all together. Tries the
    cheap single-hop case first (does the displaced bundle fit somewhere
    directly); if that fails, falls back to a bounded multi-hop chain search
    (_try_place_bundle_chain) that can also displace a SECOND bundle out of
    the way to make room for the first -- this is what actually resolves a
    day where every driver is individually blocked, but only because
    they're each blocking someone else in a solvable cycle-free chain.
    Never a net-zero shuffle; every committed move strictly reduces the
    unresolved count by one. Capped at max_rounds full passes, since each
    pass is at worst O(units x drivers x drivers x chain_depth)."""
    def is_unresolved(u):
        return u.jobs[0].unresolved

    for _ in range(max_rounds):
        progressed = False
        for U in [u for u in units if is_unresolved(u)]:
            placed = False
            for D in driver_pool:
                if not _driver_qualifies_for_type(D, U.vehicle_type_required):
                    continue

                # Cheapest case first, and the one the original version of
                # this function missed entirely: D may already have genuine
                # free capacity -- e.g. freed up by an HR-005 release that
                # happened after the last swap-repair pass -- in which case
                # U just needs to be placed directly, no displacement of
                # anything required. Checking every driver's real schedule
                # here (not a reduced/hypothetical one) before ever
                # considering a swap keeps a driver with genuine room from
                # being skipped just because swap-repair only knows how to
                # think in terms of displacement.
                direct_ok, direct_egk = _unit_driver_feasible(D, U, allow_override_days, travel_buffer_minutes,
                                                                group_vehicle_by_driver)
                if direct_ok:
                    v_direct, found_v_direct = _find_vehicle_for_unit(U, vehicle_pool, travel_buffer_minutes, direct_egk)
                    if found_v_direct:
                        _commit_unit(U, D, v_direct, direct_egk, group_drivers, group_vehicle_by_driver, "direct-fit")
                        for jb in U.jobs:
                            jb.unresolved = False
                        placed = True
                        progressed = True
                        break

                for bundle in _bundle_units_for_driver(units, D.id):
                    reduced = [
                        iv for iv in D.busy_intervals
                        if not any(iv[0] == x.start_dt and iv[1] == x.end_dt for x in bundle)
                    ]
                    if _overlaps_with_buffer(reduced, U.start_dt, U.end_dt, travel_buffer_minutes):
                        continue
                    if D.working_hours_per_day is not None:
                        ceiling = _driver_ceiling(D)
                        projected_span = _day_span_hours(reduced + [(U.start_dt, U.end_dt)])
                        if ceiling is not None and projected_span > ceiling + 1e-9:
                            continue
                    if _driver_is_off(D, U.start_dt.date(), allow_override_days) or not _job_matches_shift_period(U.start_dt, D.shift_period, busy_intervals=reduced):
                        continue

                    # Single-hop attempt: does the displaced bundle fit
                    # directly onto some other driver right now?
                    new_home_plan = None
                    for E in driver_pool:
                        if E.id == D.id:
                            continue
                        plan = _bundle_fits_driver(bundle, E, allow_override_days, travel_buffer_minutes, group_vehicle_by_driver)
                        if plan is not None:
                            new_home_plan = (E, plan)
                            break

                    if new_home_plan is not None:
                        E, plan = new_home_plan
                        vehicle_plan = _find_vehicles_for_bundle(plan, vehicle_pool, travel_buffer_minutes)
                        v_u, found_v_u = (None, False)
                        if vehicle_plan is not None:
                            v_u, found_v_u = _find_vehicle_for_unit(U, vehicle_pool, travel_buffer_minutes, None)
                        if vehicle_plan is not None and found_v_u:
                            for x in bundle:
                                _release_unit(x, D, vehicle_pool)
                            for x, v in vehicle_plan:
                                egk = next(e for (uu, e) in plan if uu is x)
                                _commit_unit(x, E, v, egk, group_drivers, group_vehicle_by_driver, "swap-relocated")
                            _commit_unit(U, D, v_u, None, group_drivers, group_vehicle_by_driver, "swap-placed")
                            for x in bundle:
                                for jb in x.jobs:
                                    jb.unresolved = False
                            for jb in U.jobs:
                                jb.unresolved = False
                            placed = True
                            progressed = True
                            break
                        # Single-hop found a driver-side fit but no vehicle
                        # was free there -- still worth trying the deeper
                        # chain search below, since a different branch of
                        # the chain may route around the vehicle conflict.

                    # Multi-hop fallback: maybe no driver has room for the
                    # displaced bundle RIGHT NOW, but one WOULD if one of
                    # THEIR jobs moved to a third driver first. Bounded by
                    # SWAP_REPAIR_CHAIN_DEPTH so this always terminates.
                    v_u, found_v_u = _find_vehicle_for_unit(U, vehicle_pool, travel_buffer_minutes, None)
                    if not found_v_u:
                        continue
                    chain = _try_place_bundle_chain(bundle, units, driver_pool, vehicle_pool, allow_override_days,
                                                     travel_buffer_minutes, group_vehicle_by_driver,
                                                     {D.id}, SWAP_REPAIR_CHAIN_DEPTH)
                    if chain is None:
                        continue

                    # _commit_chain releases every bundle in the chain --
                    # including this outermost one, still sitting on D --
                    # from wherever it's CURRENTLY assigned before landing
                    # each on its new home. Don't pre-release `bundle` here
                    # too: it's already chain[0], and releasing it twice
                    # would clear its assigned_driver_id before _commit_chain
                    # gets a chance to read where it's coming from.
                    _commit_chain(chain, driver_pool, vehicle_pool, group_drivers, group_vehicle_by_driver)
                    _commit_unit(U, D, v_u, None, group_drivers, group_vehicle_by_driver, "chain-swap-placed")
                    for jb in U.jobs:
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
    # Separate from hires_by_key (which stays scoped by (supplier, vehicle
    # type) for correct reuse-matching -- a hire tied to one vehicle type
    # must never be reused for a job needing a different type): this tracks
    # every hire of a given supplier across ALL vehicle types today, so the
    # displayed unit numbering ("Name", "Name 1", "Name 2"...) stays unique
    # per supplier for the whole day. Bug fixed 2026-08-15 (Phase 29d):
    # instance_number used to be computed from hires_by_key alone, so hiring
    # the same supplier for two DIFFERENT vehicle types on the same day gave
    # both hires the identical unnumbered label (each was "1st" within its
    # own type bucket) -- two genuinely different physical units displayed
    # as if they were the same one, confirmed via a real run and reported by
    # the project owner as a ReCheck-flagged "vehicle clash" that turned out
    # to be a real label collision, not a false positive.
    hires_by_supplier = {}
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
        instance_number = len(hires_by_supplier.get(chosen_offering.supplier_id, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id, supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type, instance_number=instance_number,
        )
        new_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        hires_by_supplier.setdefault(chosen_offering.supplier_id, []).append(new_hire)
        if group_key:
            group_supplier_hires.setdefault(group_key, []).append(new_hire)
        for j in unit.jobs:
            j.assigned_supplier_unit = new_hire.label
            j.assigned_supplier_id = new_hire.supplier_id
            j.assignment_note = f"Supplier: {new_hire.supplier_name}{note_suffix}"
            j.unresolved = False

    settled_job_ids = set()
    # Outer loop: rearrangement (gap-fill / HR-005 minimum-hours / idle-driver
    # rescue) and swap-repair each can create new opportunities for the
    # other -- most concretely, HR-005 releasing a whole grouped day back to
    # unresolved happens AFTER swap-repair's first (and previously only)
    # pass, so those released units never got a chance at a swap. Looping
    # the pair a bounded few times lets a later swap pick up what an earlier
    # HR-005 release freed, and vice versa, without risking an unbounded
    # back-and-forth (each inner stage is itself change-detecting and exits
    # early once nothing moves).
    for _ in range(10):
        any_rearrange_changed = False
        for _ in range(6):
            changed_gap = _fill_gaps_with_unresolved_jobs(jobs, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days)
            changed_repair = _repair_minimum_daily_hours(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                           allow_override_days, settled_job_ids=settled_job_ids)
            changed_idle = _rebalance_idle_drivers(jobs, driver_pool, vehicle_pool, travel_buffer_minutes,
                                                    allow_override_days, settled_job_ids=settled_job_ids)
            if changed_gap or changed_repair or changed_idle:
                any_rearrange_changed = True
            else:
                break

        unresolved_before_swap = sum(1 for u in units if u.jobs[0].unresolved)
        _swap_repair(units, driver_pool, vehicle_pool, travel_buffer_minutes, allow_override_days,
                     group_drivers, group_vehicle_by_driver, max_rounds=swap_rounds)
        unresolved_after_swap = sum(1 for u in units if u.jobs[0].unresolved)
        swap_progressed = unresolved_after_swap < unresolved_before_swap

        if not any_rearrange_changed and not swap_progressed:
            break

    return jobs

# ==========================================================================
# allocate_by_solver() -- NEW strategy (2026-08-08), built on Google OR-Tools'
# CP-SAT constraint solver instead of a hand-written heuristic. Added after
# three separate real bugs were found and fixed one at a time while chasing
# the last few unresolved jobs with allocate_by_anchor()'s bounded chain
# search (a phantom busy-interval left behind by a mismatched release-tuple
# comparison, a driver never re-checked after capacity freed up elsewhere,
# and a scope gap where the chain search couldn't see far enough) -- each a
# genuine, subtle bug, and each the kind of thing a greedy/local-search
# heuristic will keep producing one at a time as new edge cases surface.
# The project owner asked, after seeing this pattern, what method real
# fleet/crew-scheduling systems actually use for this problem class: a
# constraint solver. Instead of writing step-by-step placement logic, every
# hard rule is stated as a constraint and the actual goal (zero unresolved,
# then zero supplier use, then balanced hours) as an objective, and the
# solver searches the space directly and provably rather than through a
# sequence of hand-coded heuristic passes.
#
# SCOPE OF THIS FIRST VERSION -- deliberately narrower than the other three
# strategies in two specific, disclosed ways (Rule 16):
#   1. "Same Driver" group cohesion for NON-overlapping members gets a real
#      soft-preference bonus in the objective (added after testing directly
#      against a real human-planned day: a 4-row group spanning 13:00-15:00,
#      17:00-19:00, and a 23:00-01:00 pair, none of them overlapping each
#      other at all, was kept entirely on one driver in the real plan purely
#      as a preference -- the first version of this strategy had no
#      incentive to do that, and left one of those rows unresolved as a
#      direct result even though every row individually had somewhere to
#      go). What's still NOT modeled is genuine overlap-relaxation for group
#      members beyond what build_planning_units() already pre-merges (exact
#      pairs, same vehicle type, times within 1h, handled automatically as
#      one decision) -- if two DIFFERENT units share a same_driver_key AND
#      genuinely overlap in time, they're still constrained with ORDINARY
#      overlap rules and can never legally land on the same driver here,
#      even though the real engine's SD-004-aware relaxation would allow it
#      for a matching vehicle type. This is a real, disclosed, narrower
#      boundary than allocate()/allocate_by_merit()/allocate_by_anchor(),
#      not a silent gap -- the dominant real-world overlapping case (two
#      simultaneous same-vehicle pickups) is still handled correctly via
#      pre-merging; what's left out is specifically 3+-way simultaneous
#      group overlaps, a rarer pattern not yet confirmed as a real problem.
#   2. Supplier hiring is NOT modeled inside the solver itself (dynamically
#      naming/numbering hired units the way the solver would need to is a
#      different kind of combinatorial problem, and modeling it exactly
#      would roughly double the size of this change). Instead: the solver
#      is given ONLY in-house drivers/vehicles, with an explicit "unresolved"
#      escape valve per unit; whatever's still unresolved after the solver
#      finishes is then run through the SAME reuse-before-hire supplier pass
#      allocate() already uses, unchanged, matching Rule 1 (extend, don't
#      reinvent) and Rule 7 (suppliers are the fallback, and are actually a
#      separate, already-solved sub-problem, not a novel part of this one).
#
# Everything else -- license/vehicle-type matching, off-days, the shift
# window rule (gates only a driver's first job of the day, not a wall
# across the whole day -- confirmed against a real PLANNED file), the daily
# floor/ceiling (folding in the monthly-overtime-budget interaction), and
# no-double-booking for both drivers and vehicles -- is modeled as a real
# hard constraint, not approximated.
# ==========================================================================

def _solver_effective_ceiling_minutes(driver):
    """Combines the two layered daily-hours checks _unit_driver_feasible()
    already applies into ONE effective per-driver daily ceiling, in minutes
    (CP-SAT needs integers). Both layers are measured against a driver's
    duty SPAN (first assigned job's start to last assigned job's end), NOT
    summed job duration (2026-08-10 correction, confirmed directly by the
    project owner: working_hours_per_day is "the total legal working hours
    allowed / day, anything over this will be overtime" -- overtime tracks
    the same duty-span concept as the daily ceiling, deducted from
    max_overtime_hours_per_month exactly like the heuristic engines do):
    the flat daily ceiling (max_working_hours_per_day, or working_hours_per_day
    if that's blank), AND -- separately -- if max_overtime_hours_per_month is
    blank, NO daily overtime is allowed at all regardless of what the daily
    ceiling says (a blank monthly budget means zero overtime, matching the
    project's established precedent), so the effective ceiling collapses to
    working_hours_per_day in that case; if a monthly budget IS configured,
    the effective ceiling is further capped by however much of that budget
    is still unspent this month. Returns None if working_hours_per_day
    itself isn't configured (no daily rule at all for this driver)."""
    if driver.working_hours_per_day is None:
        return None
    floor_minutes = driver.working_hours_per_day * 60.0
    if driver.max_overtime_hours_per_month is None:
        return floor_minutes
    flat_ceiling_minutes = _driver_ceiling(driver) * 60.0
    remaining_monthly_minutes = max(0.0, driver.max_overtime_hours_per_month - driver.month_overtime_so_far) * 60.0
    return min(flat_ceiling_minutes, floor_minutes + remaining_monthly_minutes)


def allocate_by_solver(jobs, drivers, vehicles, supplier_offerings,
                        allowed_driver_ids=None, allowed_supplier_ids=None,
                        allow_override_days=None, travel_buffer_minutes=DEFAULT_TRAVEL_BUFFER_MINUTES,
                        time_limit_seconds=15.0, solver_status_out=None):
    """Constraint-solver strategy (Google OR-Tools CP-SAT). See the module
    comment block above this function for the full design and its two
    disclosed scope boundaries. Mutates and returns `jobs`, same contract as
    the other three strategies. Wired into plan_day_tab.py's "Run Planning"
    as of 2026-08-14 (Phase 22) -- see CHANGELOG_AI.md.

    Requires the `ortools` package. Raises ImportError with a clear message
    if it isn't installed, rather than letting the rest of this module (or
    the app) fail to import.

    solver_status_out: optional dict. If given, this function sets
    solver_status_out["status"] to one of "OPTIMAL", "FEASIBLE", or the
    CP-SAT status name for a no-solution case (e.g. "INFEASIBLE"), so a
    caller (the UI) can surface whether the result is a proven-best plan or
    just the best one found within time_limit_seconds. Purely additive --
    existing callers that don't pass this keep the original bare `jobs`
    return, unaffected."""
    try:
        from ortools.sat.python import cp_model
    except ImportError as e:
        raise ImportError(
            "allocate_by_solver() requires the 'ortools' package (pip install ortools). "
            "As of 2026-08-14 this is the strategy 'Run Planning' uses by default, "
            "so ortools is now a required dependency for normal use of this app "
            "(see requirements.txt)."
        ) from e

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
    n = len(units)
    if n == 0:
        if solver_status_out is not None:
            solver_status_out["status"] = "N/A (no jobs to plan)"
        return jobs

    # ---- Warm-start hint ----------------------------------------------------
    # Adding the Same-Driver group-cohesion bonus (see below) made this model
    # noticeably harder for CP-SAT to converge on from a cold start -- proven
    # by direct testing: wall time went from under a second to over a minute
    # without reaching a proven-optimal status. The standard fix or exactly
    # this ("good model, slow convergence") is a warm-start hint: run a fast
    # heuristic first (allocate_by_anchor, itself validated separately against
    # real data and typically sub-second) and hand its result to the solver as
    # a starting point via model.AddHint(). CP-SAT then searches for
    # improvements FROM there instead of from nothing, which both speeds up
    # convergence and tends to reach a better (or provably optimal) result
    # faster. This never constrains the actual search -- the solver is free
    # to move away from the hint entirely if a better solution exists; it's
    # purely a starting point. Runs on independent COPIES of the drivers/
    # vehicles/jobs so it can't leak any state into the real objects this
    # function will go on to mutate for real.
    import copy
    from dataclasses import replace as _dc_replace
    hint_driver_id = {}
    hint_vehicle_id = {}
    hint_unresolved = {}
    try:
        scratch_drivers = [_dc_replace(d, occupied_seconds=0.0, busy_intervals=[]) for d in driver_pool]
        scratch_vehicles = [_dc_replace(v, busy_intervals=[]) for v in vehicle_pool]
        scratch_jobs = copy.deepcopy(valid_jobs)
        allocate_by_anchor(scratch_jobs, scratch_drivers, scratch_vehicles, offering_pool,
                            allow_override_days=allow_override_days, travel_buffer_minutes=travel_buffer_minutes)
        scratch_by_sr = {j.sr: j for j in scratch_jobs}
        for i, u in enumerate(units):
            scratch_job = scratch_by_sr.get(u.jobs[0].sr)
            if scratch_job is None:
                continue
            if scratch_job.unresolved:
                hint_unresolved[i] = True
            elif scratch_job.assigned_driver_id is not None:
                hint_driver_id[i] = scratch_job.assigned_driver_id
                if scratch_job.assigned_vehicle_id is not None:
                    hint_vehicle_id[i] = scratch_job.assigned_vehicle_id
    except Exception:
        # The hint is purely a speed optimization -- if anything about this
        # scratch run goes wrong for any reason, solve cold rather than fail
        # the whole strategy over what's just a head start.
        pass

    def _minutes(dt_a, dt_b):
        return int(round((dt_b - dt_a).total_seconds() / 60.0))

    def _overlaps(u, w, buffer_minutes):
        buf = timedelta(minutes=buffer_minutes)
        return u.start_dt < w.end_dt + buf and w.start_dt < u.end_dt + buf

    model = cp_model.CpModel()

    # ---- Per-unit compatible driver list (license + off-day only here --
    # shift is sequence-dependent, handled below via has_morning/has_evening
    # linking constraints, not a pre-filter) --------------------------------
    compatible_drivers = []
    for u in units:
        compat = [
            d for d in driver_pool
            if _driver_qualifies_for_type(d, u.vehicle_type_required)
            and not _driver_is_off(d, u.start_dt.date(), allow_override_days)
        ]
        compatible_drivers.append(compat)

    # ---- Per-unit compatible vehicle list ----------------------------------
    needs_vehicle = [_vehicle_type_needs_vehicle(u.vehicle_type_required) for u in units]
    compatible_vehicles = []
    for i, u in enumerate(units):
        if not needs_vehicle[i]:
            compatible_vehicles.append([])
            continue
        compatible_vehicles.append([v for v in vehicle_pool if _type_matches(u.vehicle_type_required, v.vehicle_type)])

    # ---- Decision variables -------------------------------------------------
    x = {}   # (i, driver.id) -> BoolVar
    for i, u in enumerate(units):
        for d in compatible_drivers[i]:
            x[i, d.id] = model.NewBoolVar(f"x_u{i}_d{d.id}")

    veh = {}  # (i, vehicle.id) -> BoolVar
    for i, u in enumerate(units):
        for v in compatible_vehicles[i]:
            veh[i, v.id] = model.NewBoolVar(f"veh_u{i}_v{v.id}")

    unresolved = [model.NewBoolVar(f"unresolved_u{i}") for i in range(n)]

    # Apply the warm-start hint gathered above, now that x/veh/unresolved
    # all exist. Only hints variables the scratch run actually touched --
    # any (unit, driver) or (unit, vehicle) pair not license/type-compatible
    # never got a variable created for it in the first place, so there's
    # nothing to hint there regardless.
    hint_vars, hint_vals = [], []
    for i in range(n):
        if hint_unresolved.get(i):
            hint_vars.append(unresolved[i])
            hint_vals.append(1)
            continue
        did = hint_driver_id.get(i)
        if did is not None and (i, did) in x:
            hint_vars.append(x[i, did])
            hint_vals.append(1)
        vid = hint_vehicle_id.get(i)
        if vid is not None and (i, vid) in veh:
            hint_vars.append(veh[i, vid])
            hint_vals.append(1)
    for hv, hval in zip(hint_vars, hint_vals):
        model.add_hint(hv, hval)

    # Each unit is either assigned to exactly one compatible driver, or
    # unresolved -- never both, never neither.
    for i in range(n):
        driver_terms = [x[i, d.id] for d in compatible_drivers[i]]
        model.Add(sum(driver_terms) + unresolved[i] == 1)

    # If a unit needs a vehicle, it gets exactly one IFF it's actually
    # assigned to a driver; if it needs no vehicle ("Driver Only"), no
    # vehicle variables exist for it at all (nothing to constrain).
    for i, u in enumerate(units):
        if needs_vehicle[i]:
            driver_terms = [x[i, d.id] for d in compatible_drivers[i]]
            vehicle_terms = [veh[i, v.id] for v in compatible_vehicles[i]]
            model.Add(sum(vehicle_terms) == sum(driver_terms))

    # ---- No-double-booking: drivers ----------------------------------------
    # See the module comment block's scope note (1): the Same-Driver overlap
    # exception only applies INSIDE a pre-merged unit (handled automatically,
    # since that's a single decision); any two DIFFERENT units are always
    # mutually exclusive on a shared driver if their times genuinely overlap,
    # regardless of same_driver_key.
    for i in range(n):
        for j2 in range(i + 1, n):
            if not _overlaps(units[i], units[j2], travel_buffer_minutes):
                continue
            shared_driver_ids = set(d.id for d in compatible_drivers[i]) & set(d.id for d in compatible_drivers[j2])
            for did in shared_driver_ids:
                model.Add(x[i, did] + x[j2, did] <= 1)

    # ---- No-double-booking: vehicles ---------------------------------------
    for i in range(n):
        if not needs_vehicle[i]:
            continue
        for j2 in range(i + 1, n):
            if not needs_vehicle[j2] or not _overlaps(units[i], units[j2], travel_buffer_minutes):
                continue
            shared_vehicle_ids = set(v.id for v in compatible_vehicles[i]) & set(v.id for v in compatible_vehicles[j2])
            for vid in shared_vehicle_ids:
                model.Add(veh[i, vid] + veh[j2, vid] <= 1)

    # ---- Shift window: gates only a driver's FIRST job of the day ----------
    # (confirmed against a real human-planned day where a 'morning' driver
    # routinely picked up an afternoon job as a natural continuation of a
    # day already under way -- see _job_matches_shift_period's docstring for
    # the full reasoning). Encoded as: if a 'morning' driver has ANY evening-
    # window unit assigned, they must have AT LEAST ONE morning-window unit
    # assigned too (their real first job); symmetric for 'evening' drivers
    # picking up an early-morning-window unit (e.g. an overnight job rolling
    # into the small hours).
    for d in driver_pool:
        if d.shift_period not in ("morning", "evening"):
            continue
        morning_terms, evening_terms = [], []
        for i, u in enumerate(units):
            if (i, d.id) not in x:
                continue
            is_evening_unit = u.start_dt.time() >= time(SHIFT_PERIOD_EVENING_CUTOFF_HOUR, 0)
            (evening_terms if is_evening_unit else morning_terms).append(x[i, d.id])
        if d.shift_period == "morning" and evening_terms:
            has_morning = model.NewBoolVar(f"has_morning_d{d.id}")
            if morning_terms:
                model.AddMaxEquality(has_morning, morning_terms)
            else:
                model.Add(has_morning == 0)
            for term in evening_terms:
                model.Add(term <= has_morning)
        if d.shift_period == "evening" and morning_terms:
            has_evening = model.NewBoolVar(f"has_evening_d{d.id}")
            if evening_terms:
                model.AddMaxEquality(has_evening, evening_terms)
            else:
                model.Add(has_evening == 0)
            for term in morning_terms:
                model.Add(term <= has_evening)

    # ---- Daily hours: floor (if used at all) and ceiling --------------------
    # HR-005 exemption (already established in allocate()'s
    # _repair_minimum_daily_hours, confirmed against the real PLANNED file):
    # the daily MINIMUM only applies when a driver's day is entirely
    # ungrouped work. If ANY of a driver's units that day belong to a
    # Same-Driver group, the floor is not enforced at all for that day --
    # a real driver in the ground-truth plan (VISWANADHAN) legitimately
    # worked only 5h because every one of his jobs that day was inside one
    # flagged group, and the group's own hours are what's driving the
    # shortfall, not something forcing more (or fewer) hours could fix.
    # The ceiling still applies unconditionally either way -- only the
    # floor gets this exemption.
    #
    # 2026-08-10 duty-span correction (confirmed directly by the project
    # owner with a concrete example): the floor/ceiling hard rule, AND the
    # monthly overtime budget interaction, are BOTH measured against a
    # driver's duty SPAN -- first assigned job's start to last assigned
    # job's end -- NOT the summed duration of their jobs. working_hours_per_day
    # is the total legal daily hours; any SPAN beyond it is overtime,
    # deducted from max_overtime_hours_per_month -- the same duty-span
    # concept throughout, not a separate sum-based one. Summed duration
    # ("hours worked") has no hard rule of its own at all and is used only
    # for fairness (the balance objective further below, via total_minutes
    # -- unchanged, still genuinely sum-based).
    #
    # Modeling a span in CP-SAT requires MIN/MAX over only the units
    # actually assigned to a driver, which needs a channeling trick: for
    # each (unit, driver) pair, an "effective start/end" that collapses to
    # a neutral extreme value when that unit ISN'T assigned, so it can
    # never influence the min/max, then AddMinEquality/AddMaxEquality over
    # all of them. NEUTRAL is a large constant safely outside the real
    # time range spanned by any real dataset this app handles.
    NEUTRAL = 100_000
    epoch = min(u.start_dt for u in units)

    def _abs_minutes(dt):
        return int(round((dt - epoch).total_seconds() / 60.0))

    used = {}
    total_minutes = {}
    span_minutes = {}
    for d in driver_pool:
        my_terms = [(i, x[i, d.id]) for i, u in enumerate(units) if (i, d.id) in x]
        if not my_terms:
            continue
        total = sum(_minutes(units[i].start_dt, units[i].end_dt) * var for i, var in my_terms)
        total_var = model.NewIntVar(0, 24 * 60, f"total_d{d.id}")
        model.Add(total_var == total)
        total_minutes[d.id] = total_var

        used_var = model.NewBoolVar(f"used_d{d.id}")
        model.AddMaxEquality(used_var, [var for _, var in my_terms])
        used[d.id] = used_var

        effective_starts, effective_ends = [], []
        for i, var in my_terms:
            start_abs = _abs_minutes(units[i].start_dt)
            end_abs = _abs_minutes(units[i].end_dt)
            eff_start = model.NewIntVar(0, NEUTRAL, f"eff_start_u{i}_d{d.id}")
            model.Add(eff_start == start_abs).OnlyEnforceIf(var)
            model.Add(eff_start == NEUTRAL).OnlyEnforceIf(var.Not())
            effective_starts.append(eff_start)
            eff_end = model.NewIntVar(-NEUTRAL, NEUTRAL, f"eff_end_u{i}_d{d.id}")
            model.Add(eff_end == end_abs).OnlyEnforceIf(var)
            model.Add(eff_end == -NEUTRAL).OnlyEnforceIf(var.Not())
            effective_ends.append(eff_end)
        first_start = model.NewIntVar(0, NEUTRAL, f"first_start_d{d.id}")
        model.AddMinEquality(first_start, effective_starts)
        last_end = model.NewIntVar(-NEUTRAL, NEUTRAL, f"last_end_d{d.id}")
        model.AddMaxEquality(last_end, effective_ends)
        span_var = model.NewIntVar(-2 * NEUTRAL, 2 * NEUTRAL, f"span_d{d.id}")
        model.Add(span_var == last_end - first_start)
        span_minutes[d.id] = span_var

        effective_ceiling_minutes = _solver_effective_ceiling_minutes(d)
        if effective_ceiling_minutes is not None:
            model.Add(span_var <= int(effective_ceiling_minutes)).OnlyEnforceIf(used_var)

            grouped_terms = [var for i, var in my_terms if units[i].same_driver_key]
            if grouped_terms:
                has_grouped_unit = model.NewBoolVar(f"has_grouped_d{d.id}")
                model.AddMaxEquality(has_grouped_unit, grouped_terms)
                floor_applies = model.NewBoolVar(f"floor_applies_d{d.id}")
                model.Add(floor_applies <= used_var)
                model.Add(floor_applies <= 1 - has_grouped_unit)
                model.Add(floor_applies >= used_var - has_grouped_unit)
            else:
                floor_applies = used_var  # no grouped units possible for this driver at all -- floor always applies when used

            floor_minutes = int(d.working_hours_per_day * 60)
            model.Add(span_var >= floor_minutes).OnlyEnforceIf(floor_applies)

    # ---- Same-Driver group cohesion (soft preference, SD-002/SD-003) ------
    # build_planning_units() already merges exact pairs (same vehicle type,
    # times within 1h) into one unit -- nothing extra needed there, it's a
    # single decision already. But a group can have MORE than 2 members, or
    # members spread far enough apart in time that they never got pre-merged
    # even though they don't conflict with each other at all -- confirmed
    # against a real human-planned day: a 4-row group (13:00-15:00,
    # 17:00-19:00, and a 23:00-01:00 pair) was kept entirely on one driver,
    # with zero time overlap between any of its members, purely as a
    # preference. Originally scoped OUT of v1 on the assumption the dominant
    # pattern was already covered by pre-merged pairs -- but testing directly
    # against that real file showed the solver leaving a job unresolved
    # specifically because nothing told it to prefer consolidating that
    # group, even though every member individually had a place to go.
    #
    # Encoded as: minimize the number of DISTINCT drivers touching each
    # group, rather than a per-PAIR "together" bonus. A first version used
    # pairwise together[i,j,driver] booleans (O(members^2) per group) and
    # measurably slowed the solver down -- confirmed by direct testing, wall
    # time went from under a second to over a minute without even reaching a
    # proven-optimal status, because pairwise terms create a lot of
    # symmetric, equally-good alternative combinations for the search to
    # sift through. touches_group[group,driver] (O(members) per group,
    # linked with simple one-directional >= constraints and left otherwise
    # unconstrained -- the objective's downward pressure does the rest) is
    # the standard leaner way to express "minimize how spread out this group
    # is" and is both correct and cheap.
    group_driver_touch_terms = []
    units_by_group = {}
    for i, u in enumerate(units):
        key = u.same_driver_key or None
        if key:
            units_by_group.setdefault(key, []).append(i)
    for key, member_indices in units_by_group.items():
        if len(member_indices) < 2:
            continue
        candidate_driver_ids = set()
        for i in member_indices:
            candidate_driver_ids.update(d.id for d in compatible_drivers[i])
        for did in candidate_driver_ids:
            member_vars_on_this_driver = [x[i, did] for i in member_indices if (i, did) in x]
            if not member_vars_on_this_driver:
                continue
            touches = model.NewBoolVar(f"touches_{key}_{did}")
            for var in member_vars_on_this_driver:
                model.Add(touches >= var)
            group_driver_touch_terms.append(touches)

    # ---- Objective: minimize unresolved (dominant), then reward Same-Driver
    # group consolidation, then minimize unused capacity among drivers
    # actually used (balances hours -- directly the "job 57 swap" idea the
    # project owner described: redistribute so idle time is spread evenly
    # rather than piling up on one driver), then a small bonus for spreading
    # work across more drivers when it's free to do so. Each tier uses a
    # weight comfortably larger than everything below it could possibly sum
    # to, so higher tiers are never traded away for a gain in a lower one.
    # -------------------------------------------------------------
    BIG_M = 1_000_000
    GROUP_COHESION_WEIGHT = 10_000
    unused_capacity_terms = []
    for d in driver_pool:
        if d.id not in total_minutes:
            continue
        # Fairness/balance term stays SUM-based (total_minutes) -- matches
        # the project owner's explanation that "hours worked" is purely a
        # fairness concern, measured here against the same effective
        # ceiling used for the hard span rule (a driver's summed worked
        # hours can never exceed their span, so this is still a
        # meaningful "how full is this driver" reference).
        ceiling_minutes = _solver_effective_ceiling_minutes(d)
        if ceiling_minutes is None:
            continue
        gap = model.NewIntVar(0, int(ceiling_minutes), f"gap_d{d.id}")
        model.Add(gap == int(ceiling_minutes) - total_minutes[d.id]).OnlyEnforceIf(used[d.id])
        model.Add(gap == 0).OnlyEnforceIf(used[d.id].Not())
        unused_capacity_terms.append(gap)

    driver_count_bonus = sum(used.values()) if used else 0

    model.Minimize(
        BIG_M * sum(unresolved)
        + GROUP_COHESION_WEIGHT * sum(group_driver_touch_terms)
        + sum(unused_capacity_terms)
        - driver_count_bonus
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if solver_status_out is not None:
        solver_status_out["status"] = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # No feasible solution found at all within the time limit (should be
        # rare -- "everyone unresolved" is always feasible, since unresolved
        # has no other constraints) -- fail safe by marking everything
        # unresolved rather than raising, matching every other strategy's
        # behavior when a job simply can't be placed.
        for u in units:
            for j in u.jobs:
                j.unresolved = True
                j.assignment_note = "Solver found no feasible solution within the time limit"
        return jobs

    driver_by_id = {d.id: d for d in driver_pool}
    group_drivers = {}
    group_vehicle_by_driver = {}

    for i, u in enumerate(units):
        if solver.Value(unresolved[i]):
            for j in u.jobs:
                j.unresolved = True
            continue
        chosen_driver = None
        for d in compatible_drivers[i]:
            if solver.Value(x[i, d.id]):
                chosen_driver = d
                break
        chosen_vehicle = None
        if needs_vehicle[i]:
            for v in compatible_vehicles[i]:
                if solver.Value(veh[i, v.id]):
                    chosen_vehicle = v
                    break
        _commit_unit(u, chosen_driver, chosen_vehicle, u.same_driver_key or None,
                     group_drivers, group_vehicle_by_driver, "solver")

    # ---- Post-solve validation (defense in depth) --------------------------
    # A driver hard-rule violation must never ship silently, no matter how
    # it happened -- this re-checks every used driver's actual committed
    # span against their floor/ceiling (respecting the Same-Driver-group
    # exemption) directly from the real Job data, independent of the CP-SAT
    # model's own internal bookkeeping. If this ever fires, it means the
    # constraint encoding itself has a real bug (every solver-returned
    # solution is supposed to satisfy every modeled constraint by
    # definition) -- fail loudly here rather than let a bad result through,
    # matching Rule 2/6 (hard rules must never be silently violated).
    per_driver_committed = {}
    for j in jobs:
        if j.assigned_driver_id is not None and j.start_dt is not None:
            per_driver_committed.setdefault(j.assigned_driver_id, []).append(j)
    for d in driver_pool:
        day_jobs = per_driver_committed.get(d.id)
        if not day_jobs or d.working_hours_per_day is None:
            continue
        has_group = any(j.same_driver_key for j in day_jobs)
        if has_group:
            continue  # HR-005 exemption -- floor doesn't apply
        span = _day_span_hours([(j.start_dt, j.end_dt) for j in day_jobs])
        ceiling = _driver_ceiling(d)
        if span < d.working_hours_per_day - 1e-6 or (ceiling is not None and span > ceiling + 1e-6):
            raise AssertionError(
                f"allocate_by_solver() internal error: {d.name}'s committed duty span ({span:.2f}h) "
                f"violates their hard floor/ceiling ({d.working_hours_per_day}h-{ceiling}h) with no "
                f"Same-Driver-group exemption applicable. This should be impossible under the CP-SAT "
                f"model's constraints -- please report this with the input file, it indicates a real "
                f"bug in the constraint encoding, not a data issue."
            )

    # ---- Supplier fallback for anything the solver left unresolved --------
    # Identical reuse-before-hire logic to allocate()/allocate_by_merit()'s
    # supplier pass, reused rather than reimplemented (Rule 1).
    leftover_units = [u for u in units if u.jobs[0].unresolved]
    hires_by_key = {}
    # Separate from hires_by_key (which stays scoped by (supplier, vehicle
    # type) for correct reuse-matching -- a hire tied to one vehicle type
    # must never be reused for a job needing a different type): this tracks
    # every hire of a given supplier across ALL vehicle types today, so the
    # displayed unit numbering ("Name", "Name 1", "Name 2"...) stays unique
    # per supplier for the whole day. Bug fixed 2026-08-15 (Phase 29d):
    # instance_number used to be computed from hires_by_key alone, so hiring
    # the same supplier for two DIFFERENT vehicle types on the same day gave
    # both hires the identical unnumbered label (each was "1st" within its
    # own type bucket) -- two genuinely different physical units displayed
    # as if they were the same one, confirmed via a real run and reported by
    # the project owner as a ReCheck-flagged "vehicle clash" that turned out
    # to be a real label collision, not a false positive.
    hires_by_supplier = {}
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
                j.unresolved = False
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
        instance_number = len(hires_by_supplier.get(chosen_offering.supplier_id, [])) + 1
        new_hire = SupplierHire(
            supplier_id=chosen_offering.supplier_id, supplier_name=chosen_offering.supplier_name,
            vehicle_type=chosen_offering.vehicle_type, instance_number=instance_number,
        )
        new_hire.busy_intervals.append((unit.start_dt, unit.end_dt, group_key))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)
        hires_by_supplier.setdefault(chosen_offering.supplier_id, []).append(new_hire)
        if group_key:
            group_supplier_hires.setdefault(group_key, []).append(new_hire)
        for j in unit.jobs:
            j.unresolved = False
            j.assigned_supplier_unit = new_hire.label
            j.assigned_supplier_id = new_hire.supplier_id
            j.assignment_note = f"Supplier: {new_hire.supplier_name}{note_suffix}"

    return jobs

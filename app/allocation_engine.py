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
4. Known simplification, documented rather than hidden: hour totals
   (`occupied_seconds`, monthly-overtime projection) still SUM every row's
   duration even when two rows in the same flagged group overlap in time.
   This can overstate a driver's true occupied hours for overlapping rows,
   but never understates them -- given Rule 6 (driver safety is a hard
   constraint, no automatic overtime), overstating hours is the safe
   direction to err in. If this ever needs to be exact (true elapsed-time
   union instead of a sum), that is a deliberate follow-up, not something
   to silently change here.
"""
import re
from dataclasses import dataclass, field
from datetime import timedelta, date, datetime

DEFAULT_TRAVEL_BUFFER_MINUTES = 30


@dataclass
class DriverProfile:
    id: int
    name: str
    working_hours_per_day: float = None      # None = no daily baseline known
    shift_start: str = None                  # raw text from db, e.g. "07:00 AM" or "18:00"; None = no restriction
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
            shift_start=row["shift_start"],
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


_SHIFT_START_FORMATS = ("%I:%M %p", "%I:%M%p", "%H:%M")


def _parse_shift_start_time(raw_text):
    """Parses the free-typed 'Shift start' field (e.g. "07:00 AM" -- the
    format the Drivers tab placeholder suggests -- or a plain 24-hour
    "18:00") into a datetime.time. Returns None if blank or unparseable,
    which means "no shift-start restriction for this driver" -- the same
    fail-open behaviour as every other optional hard-rule field in this
    engine (e.g. working_hours_per_day=None), rather than silently
    blocking every job for a driver whose text didn't parse."""
    if not raw_text:
        return None
    text = str(raw_text).strip()
    for fmt in _SHIFT_START_FORMATS:
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _job_is_before_shift_start(job_start_dt, shift_start_time):
    """True if this job would start before the driver's shift begins that
    day. Compares time-of-day only (not date) -- a driver's shift_start is
    a daily recurring time, not tied to a specific calendar date."""
    if shift_start_time is None:
        return False
    return job_start_dt.time() < shift_start_time


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
            if _job_is_before_shift_start(job.start_dt, _parse_shift_start_time(d.shift_start)):
                continue
            if _overlaps_with_buffer(d.busy_intervals, job.start_dt, job.end_dt,
                                      travel_buffer_minutes, ignore_group_key=group_key):
                continue

            if d.working_hours_per_day is not None:
                projected_today_hours = d.occupied_seconds / 3600.0 + job_hours
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
            group_candidates = [d for d in candidates if group_key and d in group_drivers.get(group_key, [])]
            chosen_driver = min(group_candidates or candidates, key=lambda d: d.occupied_seconds)

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

                job_seconds = (job.end_dt - job.start_dt).total_seconds()
                chosen_driver.occupied_seconds += job_seconds
                chosen_driver.busy_intervals.append((job.start_dt, job.end_dt, group_key))
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

    return jobs

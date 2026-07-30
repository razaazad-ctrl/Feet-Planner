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
"""
from dataclasses import dataclass, field
from datetime import timedelta, date

DEFAULT_TRAVEL_BUFFER_MINUTES = 30


@dataclass
class DriverProfile:
    id: int
    name: str
    working_hours_per_day: float = None      # None = no daily baseline known
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


def _overlaps_with_buffer(existing_intervals, start_dt, end_dt, buffer_minutes):
    buffer = timedelta(minutes=buffer_minutes)
    for (s, e) in existing_intervals:
        if start_dt < e + buffer and s < end_dt + buffer:
            return True
    return False


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

    for job in sorted(jobs, key=lambda j: j.start_dt or j.row_number):
        if job.start_dt is None or job.end_dt is None:
            job.unresolved = True
            job.assignment_note = "Could not parse date/time for this row"
            continue

        job_date = job.start_dt.date()
        job_hours = (job.end_dt - job.start_dt).total_seconds() / 3600.0

        # ---------------------------------------------------- in-house pass
        candidates = []
        for d in driver_pool:
            if not _driver_qualifies_for_type(d, job.vehicle_type_required):
                continue
            if _driver_is_off(d, job_date, allow_override_days):
                continue
            if _overlaps_with_buffer(d.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes):
                continue

            if d.working_hours_per_day is not None and d.max_overtime_hours_per_month is not None:
                projected_today_hours = d.occupied_seconds / 3600.0 + job_hours
                projected_today_overtime = max(0.0, projected_today_hours - d.working_hours_per_day)
                projected_month_overtime = d.month_overtime_so_far + projected_today_overtime
                if projected_month_overtime > d.max_overtime_hours_per_month:
                    continue
            candidates.append(d)

        if candidates:
            chosen_driver = min(candidates, key=lambda d: d.occupied_seconds)
            matching_vehicles = [
                v for v in vehicle_pool
                if _type_matches(job.vehicle_type_required, v.vehicle_type)
                and not _overlaps_with_buffer(v.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes)
            ]
            if matching_vehicles:
                chosen_vehicle = matching_vehicles[0]
                job.assigned_driver_id = chosen_driver.id
                job.assigned_vehicle_id = chosen_vehicle.id
                job.assigned_vehicle_plate = chosen_vehicle.plate
                job.assignment_note = f"In-house: {chosen_driver.name}"

                job_seconds = (job.end_dt - job.start_dt).total_seconds()
                chosen_driver.occupied_seconds += job_seconds
                chosen_driver.busy_intervals.append((job.start_dt, job.end_dt))
                chosen_vehicle.busy_intervals.append((job.start_dt, job.end_dt))
                continue
            # Qualified driver but no matching in-house vehicle -> fall through to supplier.

        # ---------------------------------------------------- supplier pass
        matching_offerings = [o for o in offering_pool if _type_matches(job.vehicle_type_required, o.vehicle_type)]
        if not matching_offerings:
            job.unresolved = True
            job.assignment_note = "No qualifying in-house or supplier resource available"
            continue

        # Priority 1: reuse an already-hired unit (any matching supplier) that's free now.
        reusable_hire = None
        for o in matching_offerings:
            key = (o.supplier_id, o.vehicle_type)
            for hire in hires_by_key.get(key, []):
                if not _overlaps_with_buffer(hire.busy_intervals, job.start_dt, job.end_dt, travel_buffer_minutes):
                    reusable_hire = hire
                    break
            if reusable_hire:
                break

        if reusable_hire:
            reusable_hire.busy_intervals.append((job.start_dt, job.end_dt))
            label = reusable_hire.label
            job.assigned_supplier_unit = f"SAME {label}" if reusable_hire.already_used else label
            job.assigned_supplier_id = reusable_hire.supplier_id
            reusable_hire.already_used = True
            job.assignment_note = f"Supplier: {reusable_hire.supplier_name}"
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
        new_hire.busy_intervals.append((job.start_dt, job.end_dt))
        new_hire.already_used = True
        hires_by_key.setdefault(key, []).append(new_hire)

        job.assigned_supplier_unit = new_hire.label
        job.assigned_supplier_id = new_hire.supplier_id
        job.assignment_note = f"Supplier: {new_hire.supplier_name}"

    return jobs

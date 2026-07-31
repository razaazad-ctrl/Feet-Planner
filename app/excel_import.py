"""
excel_import.py

Reads the daily "requests" Excel file (same layout as the reference PDF,
with Vehicle/Driver columns blank) and turns each row into a structured
Job record the allocation engine can work with.

Also extracts an `event_id` from the Event column (the repeated numeric
code, e.g. "602102 - 602102 - Dubai World Cup DWC 2026 @Meydan" -> "602102")
so the engine can recognize which rows belong to the same event and reason
about them as a chain rather than unrelated one-off trips.

Also reads the "Same Driver" column (planner-pasted free text, typically
the Event text copy-pasted onto every row the planner wants handled by one
driver going back and forth). This is NOT the same mechanism as event_id --
event_id groups rows automatically from the Event column; same_driver_key
is an explicit planner override that only exists when the planner pastes
something into that column. Rows with a blank "Same Driver" cell behave
exactly as before. See allocation_engine.py for how this is enforced.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from typing import Optional

import openpyxl

_EVENT_ID_RE = re.compile(r"^\s*(\d{4,7})\s*-")
_DATETIME_RE = re.compile(
    r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})"
)
_TIME_RANGE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")

# Recognized header names -> internal field name. Matching is case-insensitive
# and ignores extra whitespace, so small export differences don't break import.
_HEADER_MAP = {
    "sr#": "sr",
    "sr #": "sr",
    "order": "order_no",
    "start date time": "date_time_raw",  # kept for the older combined-cell format
    "start date": "date_only",
    "time": "time_range_only",
    "pick up location": "pickup_location",
    "contact person": "contact_person",
    "order location": "order_location",
    "event": "event_text",
    "vehicle type": "vehicle_type_required",
    "additional info": "additional_info",
    "vehicle": "vehicle",
    "driver": "driver",
    "same driver": "same_driver_key",
    "charge code": "charge_code",
}


@dataclass
class Job:
    row_number: int
    sr: str = ""
    order_no: str = ""
    date: Optional[datetime] = None
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    pickup_location: str = ""
    contact_person: str = ""
    order_location: str = ""
    event_text: str = ""
    event_id: str = ""
    vehicle_type_required: str = ""
    additional_info: str = ""
    charge_code: str = ""
    same_driver_key: str = ""  # planner-pasted text flagging "same driver should do all rows with this value"

    # filled in by the allocation engine
    assigned_driver_id: Optional[int] = None
    assigned_driver_name: str = ""  # clean name only, no suffixes -- for export/writing, not display
    assigned_vehicle_id: Optional[int] = None
    assigned_vehicle_plate: str = ""
    assigned_supplier_unit: Optional[str] = None  # "Supplier Name #N"
    assigned_supplier_id: Optional[int] = None
    assignment_note: str = ""
    unresolved: bool = False


def _normalize_header(h):
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _clean_text(text):
    """Collapses embedded newlines/extra whitespace from wrapped Excel cells
    into single spaces, so vehicle-type matching and display stay clean."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def _parse_date_value(raw_date):
    """
    Handles the START DATE column, which openpyxl may give us as a real
    datetime/date object (normal case) or, less commonly, as text like
    "28-Feb-2026". Returns a date, or None if it can't be parsed.
    """
    if raw_date is None or raw_date == "":
        return None
    if isinstance(raw_date, datetime):
        return raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    text = str(raw_date).strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_time_range_only(raw_time):
    """
    Handles the separate TIME column, e.g. "08:00 - 15:00" or "22:00 - 02:00".
    Returns (start_time, end_time) as datetime.time objects, or (None, None).
    """
    if not raw_time:
        return None, None
    m = _TIME_RANGE_RE.search(str(raw_time))
    if not m:
        return None, None
    sh, sm, eh, em = (int(x) for x in m.groups())
    return time(sh, sm), time(eh, em)


def _combine_date_and_time(the_date, start_time, end_time):
    """
    Combines a date with a start/end time-of-day into full start/end
    datetimes, rolling the end over to the next day if it's an overnight
    job (e.g. 22:00 - 02:00).
    """
    if the_date is None or start_time is None or end_time is None:
        return None, None
    start_dt = datetime.combine(the_date, start_time)
    end_dt = datetime.combine(the_date, end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def _parse_datetime_range(raw_text):
    """
    Fallback for the older combined-cell format, e.g.
    "21-Mar-2026 02:00 - 06:00" all in one field.
    """
    if not raw_text:
        return None, None
    m = _DATETIME_RE.search(str(raw_text))
    if not m:
        return None, None
    date_str, start_str, end_str = m.groups()
    start_dt = datetime.strptime(f"{date_str} {start_str}", "%d-%b-%Y %H:%M")
    end_dt = datetime.strptime(f"{date_str} {end_str}", "%d-%b-%Y %H:%M")
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def _extract_event_id(event_text):
    if not event_text:
        return ""
    m = _EVENT_ID_RE.match(str(event_text))
    return m.group(1) if m else ""


def load_jobs_from_excel(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    header_row = rows[0]
    col_index = {}
    for idx, header in enumerate(header_row):
        key = _normalize_header(header)
        if key in _HEADER_MAP:
            col_index[_HEADER_MAP[key]] = idx

    jobs = []
    for row_number, row in enumerate(rows[1:], start=2):
        if row is None or all(v is None for v in row):
            continue

        def get(field_name):
            idx = col_index.get(field_name)
            if idx is None or idx >= len(row):
                return ""
            val = row[idx]
            return "" if val is None else str(val).strip()

        def get_raw(field_name):
            idx = col_index.get(field_name)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        sr = get("sr")
        order_no = get("order_no")
        # Real job rows always have a plain integer SR#. Footer/note rows
        # (duty-time rosters, "vehicles in workshop" lists, etc.) sometimes
        # have long text sitting in this column instead -- skip those
        # entirely rather than showing them as failed/unresolved "jobs".
        try:
            int(sr)
        except (TypeError, ValueError):
            continue

        # Preferred path: separate START DATE + TIME columns (the real export format)
        the_date = _parse_date_value(get_raw("date_only"))
        start_time, end_time = _parse_time_range_only(get("time_range_only"))
        start_dt, end_dt = _combine_date_and_time(the_date, start_time, end_time)

        # Fallback: older combined "21-Mar-2026 02:00 - 06:00" single-cell format
        if start_dt is None:
            start_dt, end_dt = _parse_datetime_range(get("date_time_raw"))

        event_text = _clean_text(get("event_text"))

        job = Job(
            row_number=row_number,
            sr=sr,
            order_no=order_no,
            date=start_dt.date() if start_dt else the_date,
            start_dt=start_dt,
            end_dt=end_dt,
            pickup_location=_clean_text(get("pickup_location")),
            contact_person=_clean_text(get("contact_person")),
            order_location=_clean_text(get("order_location")),
            event_text=event_text,
            event_id=_extract_event_id(event_text),
            vehicle_type_required=_clean_text(get("vehicle_type_required")),
            additional_info=_clean_text(get("additional_info")),
            charge_code=get("charge_code"),
            same_driver_key=_clean_text(get("same_driver_key")),
        )
        jobs.append(job)

    return jobs


def group_jobs_by_event(jobs):
    """Returns {event_id: [jobs sorted by start time]} for chain-aware reasoning."""
    groups = {}
    for job in jobs:
        key = job.event_id or f"__no_event_{job.row_number}"
        groups.setdefault(key, []).append(job)
    for key in groups:
        groups[key].sort(key=lambda j: j.start_dt or datetime.min)
    return groups

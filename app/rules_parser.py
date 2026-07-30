"""
rules_parser.py

Takes a single free-text rule line (as typed by the planner) and tries to
recognize it as one of a known set of "hard rule" patterns. If recognized,
returns a structured (rule_type, parsed_value) pair that the allocation
engine can enforce directly. If not recognized, the line is still saved
and shown exactly as typed -- it just gets passed to Claude as unstructured
context instead of being enforced as a hard constraint.

The planner never has to pick a "type" -- they just type a line, and this
module figures out what it means (if it can).
"""
import re
from datetime import datetime

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Each entry: (rule_type, regex, parse_fn)
# parse_fn takes the regex match and returns a JSON-serializable value.
_PATTERNS = []


def _register(rule_type, pattern, parse_fn):
    _PATTERNS.append((rule_type, re.compile(pattern, re.IGNORECASE), parse_fn))


# ---- Driver patterns -------------------------------------------------

def _parse_time(s):
    s = s.strip().upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return s  # fall back to raw string if we can't normalize it


_register(
    "shift_start",
    r"^shift\s*start\s*:\s*(.+)$",
    lambda m: {"time": _parse_time(m.group(1))},
)

_register(
    "max_hours",
    r"^max\s*(duty\s*)?hours\s*:\s*(\d+(\.\d+)?)",
    lambda m: {"hours": float(m.group(2))},
)

_register(
    "qualified_vehicle_types",
    r"^qualified\s*for\s*:\s*(.+)$",
    lambda m: {"types": [t.strip() for t in m.group(1).split(",") if t.strip()]},
)

_register(
    "not_qualified_vehicle_types",
    r"^not\s*qualified\s*for\s*:\s*(.+)$",
    lambda m: {"types": [t.strip() for t in m.group(1).split(",") if t.strip()]},
)

_register(
    "off_day",
    r"^off\s*day\s*:\s*(.+)$",
    lambda m: {"day": m.group(1).strip().lower()},
)

_register(
    "leave",
    r"^on\s*leave\s*:\s*(.+?)\s*to\s*(.+)$",
    lambda m: {"start": m.group(1).strip(), "end": m.group(2).strip()},
)

# ---- Supplier patterns -------------------------------------------------

_register(
    "rate",
    r"^rate\s*:\s*(.+)$",
    lambda m: {"text": m.group(1).strip()},
)

_register(
    "unit",
    r"^unit\s*:\s*(.+?)\s*\((.+)\)\s*$",
    lambda m: {"label": m.group(1).strip(), "vehicle_type": m.group(2).strip()},
)

_register(
    "max_hours_per_unit",
    r"^max\s*hours\s*per\s*unit\s*:\s*(\d+(\.\d+)?)",
    lambda m: {"hours": float(m.group(1))},
)

_register(
    "blackout_day",
    r"^cannot\s*be\s*hired\s*on\s*:\s*(.+)$",
    lambda m: {"day": m.group(1).strip().lower()},
)

_register(
    "min_notice",
    r"^minimum\s*notice\s*:\s*(\d+(\.\d+)?)\s*hour",
    lambda m: {"hours": float(m.group(1))},
)


def parse_rule_line(text):
    """
    Returns (rule_type, parsed_value_dict).
    If nothing matches, rule_type is 'custom' and parsed_value is
    {"text": <original line>} -- still stored and shown, just not
    enforced as a hard rule.
    """
    text = text.strip()
    for rule_type, pattern, parse_fn in _PATTERNS:
        m = pattern.match(text)
        if m:
            try:
                return rule_type, parse_fn(m)
            except Exception:
                # If parsing the matched pattern fails for some reason,
                # fall back to custom rather than crashing on a bad line.
                break
    return "custom", {"text": text}


def describe_rule_type(rule_type, parsed_value):
    """
    Short human-readable confirmation shown next to a rule line in the UI,
    e.g. "Recognized: Max duty hours = 8". Used purely for planner
    reassurance that the line was understood correctly.
    """
    if rule_type == "shift_start":
        return f"Recognized: shift start = {parsed_value['time']}"
    if rule_type == "max_hours":
        return f"Recognized: max duty hours = {parsed_value['hours']}"
    if rule_type == "qualified_vehicle_types":
        return f"Recognized: qualified for {', '.join(parsed_value['types'])}"
    if rule_type == "not_qualified_vehicle_types":
        return f"Recognized: NOT qualified for {', '.join(parsed_value['types'])}"
    if rule_type == "off_day":
        return f"Recognized: off day = {parsed_value['day'].title()}"
    if rule_type == "leave":
        return f"Recognized: on leave {parsed_value['start']} to {parsed_value['end']}"
    if rule_type == "rate":
        return "Recognized: rate line"
    if rule_type == "unit":
        return f"Recognized: unit '{parsed_value['label']}' ({parsed_value['vehicle_type']})"
    if rule_type == "max_hours_per_unit":
        return f"Recognized: max hours per unit = {parsed_value['hours']}"
    if rule_type == "blackout_day":
        return f"Recognized: cannot be hired on {parsed_value['day'].title()}"
    if rule_type == "min_notice":
        return f"Recognized: minimum notice = {parsed_value['hours']} hours"
    return "Context line (used by AI, not a hard rule)"

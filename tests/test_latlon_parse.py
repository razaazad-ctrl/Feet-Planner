import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from app.ui.map_tab import _parse_latlon

# Phase 32d (2026-08-16): the Lat/Lon columns were read-only in the first
# version of the map screen, so the project owner pasted Google Maps
# coordinates into the ADDRESS field for all 13 of their locations -- the
# only editable box available. Accepting that input is strictly better than
# geocoding (exact, and zero API calls), so _parse_latlon recognizes it.
# These cases pin down what must and must NOT be read as coordinates.

# Exactly what right-clicking a spot in Google Maps copies:
p = _parse_latlon("25.223732780272687, 55.28831280699118")
assert p == {"lat": 25.223732780272687, "lon": 55.28831280699118}, p
print("PASS: Google Maps clipboard format parses exactly, full precision kept")

for text in ("25.2237, 55.2883", "25.2237 55.2883", "  25.2237 ,  55.2883  ", "25.2237;55.2883"):
    assert _parse_latlon(text) is not None, text
print("PASS: comma / space / semicolon separated and padded variants all parse")

south = _parse_latlon("-33.8688, 151.2093")
assert south == {"lat": -33.8688, "lon": 151.2093}, south
print("PASS: negative (southern/western hemisphere) coordinates parse")

# Real addresses must NEVER be mistaken for coordinates, including ones
# that happen to contain two numbers.
for text in (
    "Central Production Kitchen, Al Quoz, Dubai",
    "Unit 12, 45 Sheikh Zayed Road",
    "Zabeel Ladies Club, Dubai",
    "",
    None,
    "25.2237",              # only one number
    "25.2237, 55.2883, 12", # three parts
):
    assert _parse_latlon(text) is None, f"{text!r} must NOT parse as coordinates"
print("PASS: real addresses, blanks and malformed input are rejected")

# Out-of-range values are rejected rather than silently accepted.
for text in ("999, 999", "91.0, 55.0", "25.0, 181.0", "-91, 0"):
    assert _parse_latlon(text) is None, f"{text!r} is out of range and must be rejected"
print("PASS: out-of-range latitude/longitude rejected")

print()
print("ALL LAT/LON PARSING TESTS PASSED")

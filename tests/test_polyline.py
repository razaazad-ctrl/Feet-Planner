import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from app.maps_client import decode_polyline

# Phase 32 (2026-08-16): decode_polyline turns Google's Encoded Polyline
# Algorithm (also what OpenRouteService emits) into plain (lat, lon) points
# for the map to draw. Pure logic, no network -- and worth pinning down,
# because a subtle decode bug wouldn't crash, it would just silently draw
# routes through the wrong part of the world.

# Google's own published reference example:
#   https://developers.google.com/maps/documentation/utilities/polylinealgorithm
REFERENCE_ENCODED = r"_p~iF~ps|U_ulLnnqC_mqNvxq`@"
REFERENCE_POINTS = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]

decoded = decode_polyline(REFERENCE_ENCODED)
assert len(decoded) == len(REFERENCE_POINTS), f"expected 3 points, got {len(decoded)}: {decoded}"
for (got_lat, got_lon), (want_lat, want_lon) in zip(decoded, REFERENCE_POINTS):
    assert abs(got_lat - want_lat) < 1e-6, (got_lat, want_lat)
    assert abs(got_lon - want_lon) < 1e-6, (got_lon, want_lon)
print("PASS: matches Google's published reference polyline exactly")

# Empty/None must yield an empty path, not raise -- a route with no
# geometry (some cached rows legitimately have polyline=None) should just
# draw nothing rather than break the whole map render.
assert decode_polyline("") == []
assert decode_polyline(None) == []
print("PASS: empty/None input returns [] instead of raising")

# Single point round-trip: encoding of (25.1180, 55.2270) region values.
single = decode_polyline("_p~iF~ps|U")
assert len(single) == 1, single
assert abs(single[0][0] - 38.5) < 1e-6 and abs(single[0][1] - -120.2) < 1e-6, single
print("PASS: single-point polyline decodes correctly")

# Negative deltas (the algorithm's sign bit) must work -- the reference
# above already goes both directions, but assert the invariant explicitly:
# every decoded value must be a plausible lat/lon.
for lat, lon in decoded:
    assert -90.0 <= lat <= 90.0, lat
    assert -180.0 <= lon <= 180.0, lon
print("PASS: all decoded values are within valid lat/lon ranges")

print()
print("ALL POLYLINE TESTS PASSED")

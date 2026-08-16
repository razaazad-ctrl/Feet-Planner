import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from app import db

# Phase 32 (map/travel-time screen, 2026-08-16): the geocode and travel-time
# caches exist purely to keep paid Google/OpenRouteService calls from
# repeating -- an 81-trip day re-run a few times would otherwise blow past
# any free tier. These tests pin down the cache behavior the cost argument
# depends on. Pure sqlite3, no Qt, no network -- same shape as
# tests/test_finalized_jobs_db.py.

conn = db.get_connection(":memory:")
conn.executescript(db._SCHEMA)
db._run_migrations(conn)
conn.commit()

# --- geocode cache -----------------------------------------------------
assert db.get_geocode(conn, "Palm Jumeirah") is None
db.save_geocode(conn, "Palm Jumeirah", 25.1124, 55.1390)
hit = db.get_geocode(conn, "Palm Jumeirah")
assert hit == {"lat": 25.1124, "lon": 55.1390}, hit
print("PASS: geocode cache miss -> save -> hit")

# Excel text arrives with inconsistent casing/whitespace; the cache must not
# re-charge for the same place spelled differently.
assert db.get_geocode(conn, "  palm jumeirah  ") == hit
print("PASS: geocode lookup is case- and whitespace-insensitive")

db.save_geocode(conn, "Palm Jumeirah", 25.9999, 55.9999)
assert db.get_geocode(conn, "Palm Jumeirah")["lat"] == 25.9999
print("PASS: re-saving a geocode overwrites rather than duplicating")

assert db.get_geocode(conn, "") is None and db.get_geocode(conn, None) is None
print("PASS: blank/None geocode lookups return None instead of raising")

# --- travel-time cache -------------------------------------------------
assert db.get_cached_travel_time(conn, "A", "B", 8) is None
db.save_travel_time(conn, "A", "B", 8, 35.5, 22.1, "encoded_poly")
hit = db.get_cached_travel_time(conn, "A", "B", 8)
assert hit == {"duration_minutes": 35.5, "distance_km": 22.1, "polyline": "encoded_poly"}, hit
print("PASS: travel-time cache miss -> save -> hit (duration, distance, polyline)")

# The hour bucket is what preserves traffic-awareness: the same road at
# 08:00 and 23:00 is genuinely a different duration, so they must not
# share a cache entry.
assert db.get_cached_travel_time(conn, "A", "B", 23) is None
db.save_travel_time(conn, "A", "B", 23, 12.0, 22.1, None)
assert db.get_cached_travel_time(conn, "A", "B", 8)["duration_minutes"] == 35.5
assert db.get_cached_travel_time(conn, "A", "B", 23)["duration_minutes"] == 12.0
print("PASS: different hour buckets are cached separately (traffic-awareness preserved)")

# Direction matters -- one-way systems mean B->A is not A->B.
assert db.get_cached_travel_time(conn, "B", "A", 8) is None
print("PASS: reversed origin/destination is a separate cache entry")

db.save_travel_time(conn, "A", "B", 8, 40.0, 25.0, "newer")
assert db.get_cached_travel_time(conn, "A", "B", 8)["duration_minutes"] == 40.0
print("PASS: re-saving the same (origin, destination, hour) overwrites")

# Two DISTINCT travel entries exist: (A,B,8) and (A,B,23). The (A,B,8)
# re-save above overwrote rather than added, and (B,A,8) was only probed
# for a miss, never saved.
travel, geo = db.travel_cache_stats(conn)
assert (travel, geo) == (2, 1), (travel, geo)
print(f"PASS: travel_cache_stats reports {travel} travel + {geo} geocode entries")

dropped = db.clear_travel_time_cache(conn)
assert dropped == 2, dropped
assert db.get_cached_travel_time(conn, "A", "B", 8) is None
# Clearing travel times must NOT wipe geocodes -- addresses don't move, so
# re-geocoding them would be pure waste.
assert db.get_geocode(conn, "Palm Jumeirah") is not None
print("PASS: clear_travel_time_cache drops travel entries only, geocodes survive")

# --- location coordinates ---------------------------------------------
db.add_location(conn, "CPK", "Central Production Kitchen, Al Quoz, Dubai")
loc = {r["short_code"]: r for r in db.list_locations(conn)}["CPK"]
assert loc["latitude"] is None and loc["longitude"] is None
db.set_location_coords(conn, "CPK", 25.1180, 55.2270)
loc = {r["short_code"]: r for r in db.list_locations(conn)}["CPK"]
assert loc["latitude"] == 25.1180 and loc["longitude"] == 55.2270, dict(loc)
assert loc["geocoded_at"], "geocoded_at should be stamped"
print("PASS: location coordinates save and read back")

# A planner correction must stick (a geocoder puts CPK in roughly the right
# industrial area; the planner knows the exact gate).
db.set_location_coords(conn, "CPK", 25.1200, 55.2300)
loc = {r["short_code"]: r for r in db.list_locations(conn)}["CPK"]
assert loc["latitude"] == 25.1200, dict(loc)
print("PASS: manually corrected coordinates overwrite the geocoded ones")

print()
print("ALL TRAVEL/GEOCODE CACHE TESTS PASSED")

import sys
sys.path.insert(0, "/home/claude/Feet-Planner")
from app.maps_client import _clean_place_text, haversine_km

# Phase 32e (2026-08-16). Geocoding the raw Excel location strings with no
# cleanup or region bias produced spectacularly wrong coordinates that were
# then CACHED, silently poisoning travel times and map pins:
#   "ON SITE - COCA COLA ARENA" -> Atlanta, USA   (12,199 km away)
#   "ON SITE - PALM JUMEIRAH"   -> North Carolina (11,665 km away)
#   "Dubai - MENA PORT RASHID"  -> Paraguay       (13,332 km away)
# 13 of 22 cached entries were >500km from the fleet's operating area.
# These tests pin down the two text-side defences. (The third defence --
# a hard country restriction on the API call -- can't be unit-tested
# without the network.)

# --- prefix cleanup ----------------------------------------------------
# "ON SITE - " is operational noise and actively misleads a geocoder.
assert _clean_place_text("ON SITE - COCA COLA ARENA") == "COCA COLA ARENA"
assert _clean_place_text("ONSITE - MENA PORT RASHID") == "MENA PORT RASHID"
assert _clean_place_text("on site - dec south") == "dec south"
print("PASS: 'ON SITE -' / 'ONSITE -' noise prefixes stripped")

# A city prefix is real geographic context, so it's kept -- but reordered
# into the "PLACE, CITY" form geocoders actually parse reliably.
assert _clean_place_text("Dubai - COCA COLA ARENA") == "COCA COLA ARENA, Dubai"
assert _clean_place_text("Abu Dhabi - ABU DHABI") == "ABU DHABI, Abu Dhabi"
print("PASS: 'City - Place' reordered to 'Place, City' (context kept, not discarded)")

# Plain names are left completely alone.
for text in ("CPK", "Zabeel Ladies Club", "DWTC"):
    assert _clean_place_text(text) == text, text
print("PASS: plain place names pass through unchanged")

# Never crash on empty input.
assert _clean_place_text("") == ""
assert _clean_place_text(None) == ""
print("PASS: blank/None input handled")

# --- distance sanity check --------------------------------------------
# The real operating centre of the project owner's fleet, and the real
# wrong answers that prompted this work.
DUBAI = (25.1532, 55.2606)
atlanta = haversine_km(*DUBAI, 33.770851, -84.396625)
assert atlanta > 10000, atlanta
carolina = haversine_km(*DUBAI, 35.983102, -78.538868)
assert carolina > 10000, carolina
paraguay = haversine_km(*DUBAI, -25.361076, -57.492368)
assert paraguay > 10000, paraguay
print(f"PASS: the real wrong answers measure {atlanta:,.0f} / {carolina:,.0f} / {paraguay:,.0f} km away")

# Genuine local locations must sit comfortably INSIDE the 500km guard, so
# the check never rejects a real one.
cpk = haversine_km(*DUBAI, 25.222837788644085, 55.286148296812776)
assert cpk < 50, cpk
abu_dhabi = haversine_km(*DUBAI, 24.365909, 54.582942)   # a real, legitimately distant UAE job
assert abu_dhabi < 500, abu_dhabi
print(f"PASS: real locations are well inside the guard (CPK {cpk:.0f} km, Abu Dhabi {abu_dhabi:.0f} km)")

# Sanity-check the distance function itself against a known pair.
london_paris = haversine_km(51.5074, -0.1278, 48.8566, 2.3522)
assert 330 < london_paris < 360, london_paris
print(f"PASS: haversine sanity check (London->Paris = {london_paris:.0f} km, known ~344)")

print()
print("ALL GEOCODE-QUALITY TESTS PASSED")

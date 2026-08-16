"""
maps_client.py

Travel-time and geocoding lookups, from either of two providers:

* GOOGLE (preferred): the Routes API (computeRoutes) gives a TRAFFIC-AWARE
  travel time for a SPECIFIC departure time -- not just a static distance.
  The same route can take very different amounts of time at 2am vs 6pm,
  which is exactly what decides whether a driver can realistically make it
  from one job to the next. Paid per call.
  Docs: https://developers.google.com/maps/documentation/routes

* OPENROUTESERVICE (free fallback, added 2026-08-16): 2,500 requests/day
  and 40,000/month with no credit card, one key covering both geocoding
  and routing. **Its durations are NOT traffic-aware** -- they come from
  average OpenStreetMap road speeds -- so it is a genuinely weaker
  estimate, fine for trying the feature out or for a rough picture, but
  not equivalent to Google for judging a tight rush-hour connection. The
  UI surfaces which provider produced any given number so this is never
  ambiguous. Same primary-vs-free-fallback shape as the Anthropic/Gemini
  split in ai_review.py.
  Docs: https://openrouteservice.org/dev/#/api-docs

Both providers are normalized to the same return shapes, so callers don't
branch on provider except to choose one.
"""
import requests

ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
ORS_GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"
ORS_DIRECTIONS_URL = "https://api.openrouteservice.org/v2/directions/driving-car"

PROVIDER_GOOGLE = "google"
PROVIDER_ORS = "openrouteservice"


class MapsClientError(Exception):
    pass


def get_travel_time(api_key, origin_address, destination_address, departure_dt):
    """
    Returns {"duration_minutes": float, "distance_km": float} for driving
    from origin_address to destination_address, departing at departure_dt
    (a timezone-naive or aware datetime -- treated as local time).

    Raises MapsClientError with a clear message on any failure (bad key,
    address not found, network issue, etc.) rather than letting a raw
    exception surface to the UI.
    """
    if not api_key:
        raise MapsClientError("No Google Maps API key configured. Add one in Settings.")

    body = {
        "origin": {"address": origin_address},
        "destination": {"address": destination_address},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "departureTime": _to_rfc3339(departure_dt),
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # Field mask is required by the Routes API; we only ask for what we
        # use. polyline was added 2026-08-16 for the map screen -- without it
        # a route can only be drawn as a straight line between two pins,
        # which is misleading (it implies a road that isn't there).
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
    }

    try:
        resp = requests.post(ROUTES_API_URL, json=body, headers=headers, timeout=15)
    except requests.RequestException as e:
        raise MapsClientError(f"Could not reach Google Maps: {e}")

    if resp.status_code != 200:
        raise MapsClientError(
            f"Google Maps API returned an error (status {resp.status_code}): {resp.text[:300]}"
        )

    data = resp.json()
    routes = data.get("routes")
    if not routes:
        raise MapsClientError(
            f"No route found between '{origin_address}' and '{destination_address}'."
        )

    route = routes[0]
    duration_seconds = _parse_duration_seconds(route.get("duration", "0s"))
    distance_meters = route.get("distanceMeters", 0)

    return {
        "duration_minutes": round(duration_seconds / 60.0, 1),
        "distance_km": round(distance_meters / 1000.0, 1),
        "polyline": (route.get("polyline") or {}).get("encodedPolyline"),
        "provider": PROVIDER_GOOGLE,
        "traffic_aware": True,
    }


def _to_rfc3339(dt):
    # Routes API expects an RFC3339 UTC timestamp. If the datetime has no
    # timezone info, we treat it as already local and just format it;
    # for production use you'd localize this properly to the fleet's
    # timezone before converting to UTC.
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_duration_seconds(duration_str):
    # Routes API returns durations like "1234s"
    if duration_str.endswith("s"):
        return float(duration_str[:-1])
    return float(duration_str)


# ------------------------------------------------------------- geocoding
#
# A note on why the biasing below matters, learned the hard way
# (2026-08-16): geocoding the raw Excel strings with no hints produced
# spectacularly wrong results -- "ON SITE - COCA COLA ARENA" resolved to
# ATLANTA (Coca-Cola's HQ), "ON SITE - PALM JUMEIRAH" to North Carolina,
# "Dubai - MENA PORT RASHID" to Paraguay. Three causes, all addressed here:
#   1. The "ON SITE - " / "ONSITE - " prefixes are noise -> _clean_place_text
#   2. No geographic bias, so a famous name elsewhere wins -> focus_point
#      (and country, when known)
#   3. Whatever came back was accepted unquestioned -> callers now sanity-
#      check the result against the fleet's real operating area.

import re as _re

_PLACE_NOISE_PREFIXES = (
    "on site -", "onsite -", "on-site -", "on site-", "onsite-",
)


def _clean_place_text(text):
    """Strips operational noise from a raw Excel location string so the
    geocoder sees a real place name. "ON SITE - COCA COLA ARENA" ->
    "COCA COLA ARENA". Leaves genuine city prefixes ("Dubai - X") alone --
    those are useful geographic context, not noise."""
    cleaned = (text or "").strip()
    lowered = cleaned.lower()
    for prefix in _PLACE_NOISE_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    # Collapse the "Dubai - X" form into "X, Dubai", which geocoders parse
    # far more reliably than a dash-separated pair.
    parts = [p.strip() for p in _re.split(r"\s+-\s+", cleaned, maxsplit=1)]
    if len(parts) == 2 and parts[0] and parts[1]:
        cleaned = f"{parts[1]}, {parts[0]}"
    return cleaned or (text or "").strip()


def geocode_candidates(provider, api_key, query, focus_point=None, country=None, limit=8):
    """Returns up to `limit` candidate places for a free-text search, as
    [{"lat", "lon", "label", "provider"}, ...] -- best first.

    Powers the Locations panel's "Search Place" box: for an ambiguous name
    the right answer is to show the planner the options and let them pick,
    rather than silently taking the geocoder's first guess (which is how
    Coca-Cola Arena ended up in Atlanta).

    focus_point {"lat","lon"}: bias results near here. country: ISO code
    to restrict to. Both optional; both dramatically improve relevance.
    """
    query = _clean_place_text(query)
    if not query:
        raise MapsClientError("Enter something to search for.")
    if provider == PROVIDER_ORS:
        return _geocode_candidates_ors(api_key, query, focus_point, country, limit)
    return _geocode_candidates_google(api_key, query, focus_point, country, limit)


def _geocode_candidates_google(api_key, query, focus_point, country, limit):
    if not api_key:
        raise MapsClientError("No Google Maps API key configured. Add one in Settings.")
    params = {"address": query, "key": api_key}
    if country:
        params["components"] = f"country:{country}"
    if focus_point:
        # A ~0.5 degree box around the focus point -- a viewport HINT, not a
        # hard restriction (Google treats `bounds` as a bias).
        params["bounds"] = (
            f"{focus_point['lat'] - 0.5},{focus_point['lon'] - 0.5}"
            f"|{focus_point['lat'] + 0.5},{focus_point['lon'] + 0.5}"
        )
    try:
        resp = requests.get(GOOGLE_GEOCODE_URL, params=params, timeout=15)
    except requests.RequestException as e:
        raise MapsClientError(f"Could not reach Google Maps: {e}")
    if resp.status_code != 200:
        raise MapsClientError(f"Google Geocoding returned status {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        raise MapsClientError(
            f"Google Geocoding error ({data.get('status')})."
            + (f" {data['error_message']}" if data.get("error_message") else "")
        )
    out = []
    for result in (data.get("results") or [])[:limit]:
        loc = result["geometry"]["location"]
        out.append({
            "lat": loc["lat"], "lon": loc["lng"],
            "label": result.get("formatted_address") or query,
            "provider": PROVIDER_GOOGLE,
        })
    return out


def _geocode_candidates_ors(api_key, query, focus_point, country, limit):
    if not api_key:
        raise MapsClientError("No OpenRouteService API key configured. Add one in Settings.")
    params = {"api_key": api_key, "text": query, "size": limit}
    if country:
        params["boundary.country"] = country
    if focus_point:
        params["focus.point.lat"] = focus_point["lat"]
        params["focus.point.lon"] = focus_point["lon"]
    try:
        resp = requests.get(ORS_GEOCODE_URL, params=params, timeout=15)
    except requests.RequestException as e:
        raise MapsClientError(f"Could not reach OpenRouteService: {e}")
    if resp.status_code != 200:
        raise MapsClientError(f"OpenRouteService geocoding returned status {resp.status_code}: {resp.text[:200]}")
    out = []
    for feature in (resp.json().get("features") or [])[:limit]:
        lon, lat = feature["geometry"]["coordinates"][:2]   # GeoJSON is [lon, lat]
        props = feature.get("properties") or {}
        out.append({
            "lat": lat, "lon": lon,
            "label": props.get("label") or props.get("name") or query,
            "provider": PROVIDER_ORS,
        })
    return out


def geocode_address_google(api_key, address):
    """Address text -> {"lat": float, "lon": float} via Google's Geocoding
    API. Raises MapsClientError on any failure, same discipline as
    get_travel_time above."""
    if not api_key:
        raise MapsClientError("No Google Maps API key configured. Add one in Settings.")
    if not (address or "").strip():
        raise MapsClientError("Cannot geocode an empty address.")

    try:
        resp = requests.get(
            GOOGLE_GEOCODE_URL, params={"address": address, "key": api_key}, timeout=15
        )
    except requests.RequestException as e:
        raise MapsClientError(f"Could not reach Google Maps: {e}")
    if resp.status_code != 200:
        raise MapsClientError(f"Google Geocoding returned status {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    status = data.get("status")
    if status != "OK" or not data.get("results"):
        raise MapsClientError(
            f"Google could not find '{address}' (status {status})."
            + (f" {data['error_message']}" if data.get("error_message") else "")
        )
    loc = data["results"][0]["geometry"]["location"]
    return {"lat": loc["lat"], "lon": loc["lng"], "provider": PROVIDER_GOOGLE}


def geocode_address_ors(api_key, address):
    """Address text -> {"lat": float, "lon": float} via OpenRouteService's
    geocoder. Note ORS/GeoJSON returns coordinates as [lon, lat] -- the
    reverse of the (lat, lon) order used everywhere else in this app -- so
    the swap below is deliberate, not a bug."""
    if not api_key:
        raise MapsClientError("No OpenRouteService API key configured. Add one in Settings.")
    if not (address or "").strip():
        raise MapsClientError("Cannot geocode an empty address.")

    try:
        resp = requests.get(
            ORS_GEOCODE_URL, params={"api_key": api_key, "text": address, "size": 1}, timeout=15
        )
    except requests.RequestException as e:
        raise MapsClientError(f"Could not reach OpenRouteService: {e}")
    if resp.status_code != 200:
        raise MapsClientError(f"OpenRouteService geocoding returned status {resp.status_code}: {resp.text[:200]}")

    features = resp.json().get("features") or []
    if not features:
        raise MapsClientError(f"OpenRouteService could not find '{address}'.")
    lon, lat = features[0]["geometry"]["coordinates"][:2]   # GeoJSON is [lon, lat]
    return {"lat": lat, "lon": lon, "provider": PROVIDER_ORS}


def get_travel_time_ors(api_key, origin_coords, destination_coords):
    """Travel time between two (lat, lon) pairs via OpenRouteService.

    Unlike Google's Routes API (which accepts free-text addresses), ORS
    routes strictly between COORDINATES -- so callers must geocode first.
    That's not extra cost in practice: this app caches geocodes
    permanently (db.geocode_cache / locations.latitude), so the lookup is
    normally free after the first time.

    **Not traffic-aware** -- ORS returns an average-speed estimate, so the
    returned dict says so explicitly and the UI labels it. Same
    {"duration_minutes", "distance_km", "polyline", ...} shape Google
    returns, so callers don't branch.
    """
    if not api_key:
        raise MapsClientError("No OpenRouteService API key configured. Add one in Settings.")
    if not origin_coords or not destination_coords:
        raise MapsClientError("OpenRouteService needs coordinates for both ends of the route.")

    body = {"coordinates": [
        [origin_coords["lon"], origin_coords["lat"]],            # ORS wants [lon, lat]
        [destination_coords["lon"], destination_coords["lat"]],
    ]}
    try:
        resp = requests.post(
            ORS_DIRECTIONS_URL, json=body,
            headers={"Authorization": api_key, "Content-Type": "application/json"}, timeout=20,
        )
    except requests.RequestException as e:
        raise MapsClientError(f"Could not reach OpenRouteService: {e}")
    if resp.status_code != 200:
        raise MapsClientError(f"OpenRouteService returned status {resp.status_code}: {resp.text[:300]}")

    routes = resp.json().get("routes") or []
    if not routes:
        raise MapsClientError("OpenRouteService found no route between those points.")
    route = routes[0]
    summary = route.get("summary") or {}
    return {
        "duration_minutes": round(summary.get("duration", 0) / 60.0, 1),
        "distance_km": round(summary.get("distance", 0) / 1000.0, 1),
        # ORS encodes geometry with the same algorithm Google uses
        # (precision 5), so decode_polyline() below handles both.
        "polyline": route.get("geometry"),
        "provider": PROVIDER_ORS,
        "traffic_aware": False,
    }


# ------------------------------------------------------------ dispatchers
# Thin provider switches so callers pick a provider once and otherwise
# treat the two identically.

def geocode(provider, api_key, address, focus_point=None, country=None, max_km=None):
    """Best single match for an address, with the same cleanup/biasing the
    search box uses, plus an optional sanity check.

    max_km: if given along with focus_point, a result further than this
    from the focus point is REJECTED rather than returned. This is the
    guard that would have caught "COCA COLA ARENA" resolving to Atlanta --
    an obviously-wrong answer is far worse than no answer, because a wrong
    coordinate gets cached and silently poisons every travel time and map
    pin derived from it.
    """
    candidates = geocode_candidates(provider, api_key, address, focus_point, country, limit=5)
    if not candidates:
        raise MapsClientError(f"No place found for '{address}'.")
    best = candidates[0]
    if focus_point and max_km:
        distance = haversine_km(focus_point["lat"], focus_point["lon"], best["lat"], best["lon"])
        if distance > max_km:
            raise MapsClientError(
                f"'{address}' resolved to somewhere {distance:,.0f} km away "
                f"({best['label']}) -- almost certainly the wrong place, so it was not saved. "
                f"Use Search Place to pick the right one, or paste coordinates from Google Maps."
            )
    return best


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Used only for the sanity check above --
    approximate is fine, this is deciding 'same city' vs 'different
    continent', not navigating."""
    import math
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(a))


def travel_time(provider, api_key, origin_address, destination_address, departure_dt,
                 origin_coords=None, destination_coords=None):
    """One call shape for both providers. Google routes from addresses and
    factors in traffic at departure_dt; ORS routes from coordinates and
    ignores departure_dt entirely (it has no traffic model) -- passing it
    anyway keeps one signature for both."""
    if provider == PROVIDER_ORS:
        return get_travel_time_ors(api_key, origin_coords, destination_coords)
    return get_travel_time(api_key, origin_address, destination_address, departure_dt)


# ------------------------------------------------------------- polylines

def decode_polyline(encoded):
    """Decodes Google's Encoded Polyline Algorithm (precision 5) into
    [(lat, lon), ...]. Used for BOTH providers -- ORS encodes its route
    geometry the same way. Pure Python, no dependency, and decoding here
    (rather than in the map's JavaScript) means the web page needs no
    extra library and just receives a plain list of points.

    Algorithm: https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    """
    if not encoded:
        return []
    points, index, lat, lon = [], 0, 0, 0
    length = len(encoded)
    while index < length:
        for is_longitude in (False, True):
            shift, result = 0, 0
            while index < length:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            # Least-significant bit set means the value was negative.
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_longitude:
                lon += delta
            else:
                lat += delta
        points.append((lat / 1e5, lon / 1e5))
    return points

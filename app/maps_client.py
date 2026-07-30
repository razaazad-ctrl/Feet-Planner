"""
maps_client.py

Wraps Google's Routes API (computeRoutes) to get a traffic-aware travel
time between two locations at a SPECIFIC departure time -- not just a
static distance. This matters because the same route can take very
different amounts of time depending on when it's driven (e.g. 2am vs
6pm), which is exactly the information needed to judge whether a driver
can realistically make it from one job to the next, or whether waiting
on-site for the next stage of an event makes more sense.

Docs: https://developers.google.com/maps/documentation/routes
"""
import requests

ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


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
        # Field mask is required by the Routes API; we only ask for what we use.
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
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

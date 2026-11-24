"""Google Places API (New) client — REST over the official endpoints, standard library only.

Storage rules followed (Google Maps Platform Service Specific Terms + Places policies):
- Place IDs may be stored indefinitely -> we store them.
- Latitude/longitude may be cached for at most 30 consecutive days -> only the search center
  is cached (with a timestamp), and distances derived from coordinates are refreshed within 30 days.
- All other Places content (names, addresses, hours, ratings, phone, website) is fetched live and
  is NOT written to the tracker or the report. Business details are sourced from the business's
  own website instead.
- Attribution: any screen that shows Places content without a Google map must show the Google
  Maps attribution and any third-party `attributions` returned. The HTML report only shows
  Google Maps *links* (Maps URLs, no key needed) and labels them as Google Maps.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

BASE = "https://places.googleapis.com/v1"
DOCS = {
    "text_search": "https://developers.google.com/maps/documentation/places/web-service/text-search",
    "nearby_search": "https://developers.google.com/maps/documentation/places/web-service/nearby-search",
    "place_details": "https://developers.google.com/maps/documentation/places/web-service/place-details",
    "policies": "https://developers.google.com/maps/documentation/places/web-service/policies",
    "maps_urls": "https://developers.google.com/maps/documentation/urls/get-started",
    "pricing": "https://developers.google.com/maps/billing-and-pricing/pricing",
}

# Field masks: request only what each step needs (billing tier is set by the richest field).
SEARCH_FIELDS = "places.id,places.location,places.websiteUri,nextPageToken"
FOOD_FIELDS = "places.id,places.location,places.regularOpeningHours,places.websiteUri"
DETAIL_FIELDS = "id,displayName,formattedAddress,websiteUri,googleMapsUri,location,attributions"
FOOD_TYPES = ["restaurant", "cafe", "fast_food_restaurant", "convenience_store",
              "supermarket", "gas_station", "bakery"]


class PlacesError(RuntimeError):
    pass


def _call(method: str, path: str, key: str, field_mask: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": field_mask},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # surface Google's error message, never the key
        raise PlacesError(f"Places {path} HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None


def geocode_center(key: str, query: str) -> tuple[float, float]:
    """One Text Search to turn the user's town/address into a center point."""
    data = _call("POST", "/places:searchText", key, "places.id,places.location", {"textQuery": query, "pageSize": 1})
    if not data.get("places"):
        raise PlacesError(f"No place found for {query!r}")
    loc = data["places"][0]["location"]
    return loc["latitude"], loc["longitude"]


def text_search(key: str, query: str, lat: float, lng: float, radius_m: int, max_pages: int = 3) -> list[dict]:
    """Text Search biased to a circle (Text Search accepts a circle only as a bias), then the
    caller filters by real distance. Returns live results; do not persist anything but `id`."""
    out, token = [], None
    for _ in range(max_pages):
        body = {"textQuery": query, "pageSize": 20,
                "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                            "radius": float(min(radius_m, 50000))}}}
        if token:
            body["pageToken"] = token
        data = _call("POST", "/places:searchText", key, SEARCH_FIELDS, body)
        out.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def nearby_food(key: str, lat: float, lng: float, radius_m: int) -> list[dict]:
    body = {"includedTypes": FOOD_TYPES, "maxResultCount": 20,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius_m)}}}
    return _call("POST", "/places:searchNearby", key, FOOD_FIELDS, body).get("places", [])


def place_details(key: str, place_id: str, fields: str = DETAIL_FIELDS) -> dict:
    """Live details for display/matching only — never store the result."""
    return _call("GET", f"/places/{urllib.parse.quote(place_id)}", key, fields)


def maps_url(place_id: str, label: str = "") -> str:
    """Official Maps URLs format; works without an API key."""
    q = urllib.parse.urlencode({"api": 1, "query": label or "place", "query_place_id": place_id})
    return f"https://www.google.com/maps/search/?{q}"


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))

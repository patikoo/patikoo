"""Google Places API (New) client — REST over the official endpoints, standard library only.

Storage rules followed (Google Maps Platform Service Specific Terms + Places policies):
- Place IDs may be stored indefinitely -> stored.
- Latitude/longitude may be cached at most 30 consecutive days -> only the search center is kept
  (with its resolve time); distances derived from coordinates are recomputed every run.
- All other Places content (names, addresses, hours, website, phone, ratings) is used live and
  never written to the tracker or report. Business details come from business-owned websites.
- Attribution: Places content or links shown without a Google map carry "Google Maps" attribution;
  the report only shows Maps URLs (no key needed), labelled Google Maps.
"""
from __future__ import annotations

import json
import math
import urllib.error
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
SEARCH_FIELDS = "places.id,places.location,nextPageToken"
FOOD_FIELDS = "places.id,places.location,places.websiteUri,places.displayName"
DETAIL_FIELDS = "id,displayName,formattedAddress,websiteUri,location"
FOOD_TYPES = ["convenience_store", "supermarket", "grocery_store", "gas_station", "fast_food_restaurant",
              "restaurant", "cafe", "bakery", "meal_takeaway"]
NEARBY_CAP = 20


class PlacesError(RuntimeError):
    pass


def _call(method: str, path: str, key: str, field_mask: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key, "X-Goog-FieldMask": field_mask})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # surface Google's message, never the key
        raise PlacesError(f"Places {path} HTTP {e.code}: {e.read()[:300].decode(errors='replace')}") from None


def resolve_center(key: str, query: str) -> dict:
    d = _call("POST", "/places:searchText", key, "places.id,places.location", {"textQuery": query, "pageSize": 1})
    if not d.get("places"):
        raise PlacesError(f"No place found for {query!r}")
    p = d["places"][0]
    return {"place_id": p["id"], "lat": p["location"]["latitude"], "lng": p["location"]["longitude"]}


def bbox(lat: float, lng: float, r: float) -> dict:
    dlat = r / 111320.0
    dlng = r / (111320.0 * math.cos(math.radians(lat)))
    return {"rectangle": {"low": {"latitude": lat - dlat, "longitude": lng - dlng},
                          "high": {"latitude": lat + dlat, "longitude": lng + dlng}}}


def text_search_pages(key: str, query: str, lat: float, lng: float, radius_m: int, max_pages: int = 3):
    """Text Search restricted to the circle's bounding box, following nextPageToken.
    Returns (places, pages_fetched, more_pages_left)."""
    out, token, pages = [], None, 0
    while pages < max_pages:
        body = {"textQuery": query, "pageSize": 20, "locationRestriction": bbox(lat, lng, radius_m)}
        if token:
            body["pageToken"] = token
        d = _call("POST", "/places:searchText", key, SEARCH_FIELDS, body)
        pages += 1
        out.extend(d.get("places", []))
        token = d.get("nextPageToken")
        if not token:
            break
    return out, pages, bool(token)


def nearby(key: str, types: list[str], lat: float, lng: float, radius_m: float) -> list[dict]:
    body = {"includedTypes": types, "maxResultCount": NEARBY_CAP,
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius_m)}}}
    return _call("POST", "/places:searchNearby", key, FOOD_FIELDS, body).get("places", [])


def place_details(key: str, place_id: str, fields: str = DETAIL_FIELDS) -> dict:
    """Live details for hints/matching only — never store the result."""
    return _call("GET", f"/places/{urllib.parse.quote(place_id)}", key, fields)


def nearby_complete(search, lat: float, lng: float, radius_m: int) -> tuple[list[dict], dict]:
    """Find food venues within radius; a response at the 20 cap is incomplete, so narrow:
    all types -> one type per call -> 4 overlapping quarter circles per capped type.
    `search(types, lat, lng, r)` is the live (or mock) Nearby Search call."""
    cov = {"calls": 0, "raw_results": 0, "capped_calls": 0, "still_capped": []}
    found: dict[str, dict] = {}

    def run(types, la, ln, r):
        res = search(types, la, ln, r)
        cov["calls"] += 1
        cov["raw_results"] += len(res)
        capped = len(res) >= NEARBY_CAP
        cov["capped_calls"] += capped
        for p in res:
            found.setdefault(p["id"], p)
        return capped

    if run(FOOD_TYPES, lat, lng, radius_m):
        for t in FOOD_TYPES:
            if run([t], lat, lng, radius_m):
                off = radius_m / 2
                for dy, dx in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                    la = lat + dy * off / 111320.0
                    ln = lng + dx * off / (111320.0 * math.cos(math.radians(lat)))
                    if run([t], la, ln, radius_m * 0.72):
                        cov["still_capped"].append(t)
    inside = []
    for p in found.values():
        loc = p.get("location")
        if loc and distance_m(lat, lng, loc["latitude"], loc["longitude"]) <= radius_m:
            inside.append(p)
    cov["unique_in_radius"] = len(inside)
    cov["complete"] = not cov["still_capped"]
    return inside, cov


def maps_url(place_id: str, label: str = "") -> str:
    q = urllib.parse.urlencode({"api": 1, "query": label or "place", "query_place_id": place_id})
    return f"https://www.google.com/maps/search/?{q}"


def maps_search_url(text: str) -> str:
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": 1, "query": text})


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))

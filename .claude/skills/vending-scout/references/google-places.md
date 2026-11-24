# Google Maps / Places API (New) notes

Verified 2026-11-24. `developers.google.com` was blocked in the build session; these notes come from Google's published
terms and pricing pages via search, plus the live endpoint (answered `403 PERMISSION_DENIED` without a key). Re-read the live
pages before changing anything here.

## Calls (`scripts/places.py`)

| Step | Call | Field mask |
|---|---|---|
| Center | `POST https://places.googleapis.com/v1/places:searchText` (pageSize 1) | `places.id,places.location` |
| Discovery | Text Search, `locationRestriction.rectangle` = bounding box of the circle, `pageSize` 20, follow `nextPageToken` (≤3 pages); dedup by place_id; keep straight-line distance ≤ radius | `places.id,places.location,nextPageToken` |
| Live hint | `GET /v1/places/{place_id}` | `id,displayName,formattedAddress,websiteUri,location` |
| Food | `POST /v1/places:searchNearby`, `includedTypes`, `locationRestriction.circle` 800 m, `maxResultCount` 20 | `places.id,places.location,places.websiteUri,places.displayName` |

20 results = capped/incomplete. Narrowing: all types → one type per call → four quarter circles (radius 0.72 r, centers
offset r/2) per still-capped type. Still capped → `food_searches.complete = 0` and dead hours stay unknown.

## Storage and attribution

- Place IDs may be stored → kept in `sites.place_id`, `food.place_id`.
- Latitude/longitude may be cached up to 30 consecutive days → only the search center (`config.json → center.resolved_at`);
  distances are recomputed each run.
- Other Places content (names, addresses, hours, website, phone) is used live only. Names/addresses come from employer
  websites; food hours only from business-owned sources.
- Places content or links shown without a Google map carry Google Maps attribution; the report shows Maps URLs only.

## Sources

Text Search https://developers.google.com/maps/documentation/places/web-service/text-search ·
Nearby Search https://developers.google.com/maps/documentation/places/web-service/nearby-search ·
Place Details https://developers.google.com/maps/documentation/places/web-service/place-details ·
Policies https://developers.google.com/maps/documentation/places/web-service/policies ·
Service Specific Terms https://cloud.google.com/maps-platform/terms/maps-service-terms ·
Pricing https://developers.google.com/maps/billing-and-pricing/pricing ·
Maps URLs https://developers.google.com/maps/documentation/urls/get-started

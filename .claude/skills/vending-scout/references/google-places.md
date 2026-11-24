# Google Maps / Places API (New) notes

Verified 2026-11-24. `developers.google.com` was not reachable from the build session, so these notes come from Google's
published terms and pricing pages via search, plus the live endpoint (it answered `403 PERMISSION_DENIED` without a key,
which confirms the URL and header scheme). Re-read the live pages before changing anything here.

## Endpoints used (`scripts/places.py`)

| Step | Call | Field mask |
|---|---|---|
| Center point | `POST https://places.googleapis.com/v1/places:searchText` | `places.id,places.location` |
| Candidate sites | same, with `locationBias.circle` (radius ≤ 50,000 m); results filtered by distance in code | `places.id,places.location,places.websiteUri,nextPageToken` |
| Food near a site | `POST /v1/places:searchNearby`, `includedTypes`, `locationRestriction.circle` | `places.id,places.location,places.regularOpeningHours,places.websiteUri` |
| Live details | `GET /v1/places/{place_id}` | `id,displayName,formattedAddress,websiteUri,googleMapsUri,location,attributions` |
| Integration check | Text Search | `places.id` (IDs-only SKU) |

Headers: `X-Goog-Api-Key`, `X-Goog-FieldMask`. Billing tier is set by the richest field in the mask.

## Storage and attribution

- Place IDs may be stored (cached) and are the only Places content kept in the tracker.
- Latitude/longitude may be cached for up to 30 consecutive days, then deleted/refreshed (config `center_fetched_at`, `food.checked_at`).
- Other Places content (names, addresses, hours, website, phone, ratings) is used live and not written to the DB or report.
  Business name/address in `sites` come from the business's own website.
- Show Google Maps attribution where Places content or links appear without a Google map, and pass on any `attributions`.
- Map links use Maps URLs (`https://www.google.com/maps/search/?api=1&query=...&query_place_id=...`), which need no key.

## Sources

- Text Search: https://developers.google.com/maps/documentation/places/web-service/text-search
- Nearby Search: https://developers.google.com/maps/documentation/places/web-service/nearby-search
- Place Details: https://developers.google.com/maps/documentation/places/web-service/place-details
- Policies and attribution: https://developers.google.com/maps/documentation/places/web-service/policies
- Service Specific Terms: https://cloud.google.com/maps-platform/terms/maps-service-terms
- Pricing: https://developers.google.com/maps/billing-and-pricing/pricing
- Maps URLs: https://developers.google.com/maps/documentation/urls/get-started

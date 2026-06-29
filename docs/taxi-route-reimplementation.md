# Taxi Route Reimplementation Handoff

Date researched: 2026-06-29

This document captures the old Nuxt backend taxi-route feature so it can be
rebuilt in the Python FastAPI backend without needing access to the Nuxt repo.

## Current Status

The feature still exists in the Nuxt app, not in this Python backend.

Old Nuxt sources:

- `OpenSquawk/server/api/service/tools/taxiroute.get.ts`
- `OpenSquawk/server/api/service/tools/airportGeocode.ts`
- `OpenSquawk/server/api/service/tools/airport-geocode.get.ts`
- `OpenSquawk/app/pages/api-docs.vue`

The Python backend currently has only radio decision flows that mention taxi and
runways. It has no OSM, Overpass, airport feature geocoder, or taxi-route route.

Nuxt git history for the feature:

- `c7980ac` - add taxi route endpoint
- `49fce39` - document call example for taxiroute
- `2b5a762` - fix taxiroute nearest taxiway
- `e76588a` - first try with graph
- `15aa894` - get route
- `2e40361` - use runway and gate names but fails to generate route
- `713eb32` - taxiroutes from and to runway and or gates
- `b1cb22b` - fix double taxiroutes between nodes, sanatise route for using less different named taxi routes
- `0ffbfa7` - preserve raw taxi route names
- `6db100b` - adjust taxi route collapse rules
- `e22d531` - refine taxiway collapse and radius default
- `c8e35a1` - add airport geocode endpoint
- `9d43c69` - infer airport geocode types from query text
- `cc7e492` - remove explicit type hints from airport geocode lookup
- `3dee67b` - add bidirectional airport geocode and name-aware taxi routing
- `6e04501` - add todo for runway endpoint selection
- `f88fced` - typescript

No deleted OSM/taxi-route files were found in the Python backend history.

## Public API To Recreate

### GET `/api/service/tools/taxiroute`

Purpose: compute a taxi route between two points or named aerodrome features
using OpenStreetMap taxiway data.

Query parameters:

- `airport`: ICAO code. Required when either endpoint is given by feature name.
- `origin_lat`: decimal latitude.
- `origin_lng` or `origin_lon`: decimal longitude.
- `origin_name`: feature name/designator, e.g. `Gate A5`, `Stand V155`, `RWY 25C`.
- `dest_lat`: decimal latitude.
- `dest_lng` or `dest_lon`: decimal longitude.
- `dest_name`: feature name/designator.
- `radius`: Overpass taxiway search radius in meters. Default: `5000`.

At least one origin representation and one destination representation are
required. Each endpoint may be coordinates, name, or both.

Example:

```bash
curl "https://opensquawk.de/api/service/tools/taxiroute?airport=EDDF&origin_name=Gate%20A5&dest_name=RWY%2025C&radius=2500"
```

Successful response shape:

```json
{
  "airport": "EDDF",
  "origin": {
    "lat": 50.0506,
    "lon": 8.5708,
    "query": { "name": "Gate A5", "lat": null, "lon": null },
    "feature": {
      "type": "gate",
      "name": "A5",
      "lat": 50.05061,
      "lon": 8.57079,
      "matched_alias": "A5",
      "primary_alias": "A5",
      "map_url": "https://www.openstreetmap.org/node/1234567890?mlat=50.050610&mlon=8.570790#map=19/50.050610/8.570790",
      "osm": { "type": "node", "id": 1234567890, "tags": {} },
      "source": "name",
      "distance_m": null
    }
  },
  "dest": {
    "lat": 50.0473,
    "lon": 8.561,
    "query": { "name": "RWY 25C", "lat": null, "lon": null },
    "feature": {
      "type": "runway",
      "name": "25C",
      "lat": 50.04726,
      "lon": 8.561,
      "matched_alias": "25C",
      "primary_alias": "25C",
      "map_url": "https://www.openstreetmap.org/way/987654321?mlat=50.047260&mlon=8.561000#map=17/50.047260/8.561000",
      "osm": { "type": "way", "id": 987654321, "tags": {} },
      "source": "name",
      "distance_m": null
    }
  },
  "start_attach": { "node_id": 111, "lat": 50.0507, "lon": 8.5709, "distance_m": 8.3 },
  "end_attach": { "node_id": 222, "lat": 50.0472, "lon": 8.5611, "distance_m": 5.7 },
  "route": {
    "node_ids": [1234567890, 1234567990, 1234568021],
    "total_distance_m": 1580.3
  },
  "names": ["L7", "L", "N"],
  "names_collapsed": ["L7", "N"]
}
```

Old soft-error response shapes, all returned with HTTP 200 in Nuxt:

- `{ "error": "missing_origin", "details": "provide origin coordinates or name", "airport": "EDDF" }`
- `{ "error": "missing_destination", "details": "provide destination coordinates or name", "airport": "EDDF" }`
- `{ "error": "missing_airport", "details": "airport is required when using feature names" }`
- `{ "error": "overpass_error", "airport": "EDDF", "details": "..." }`
- `{ "error": "origin_not_found", "airport": "EDDF", "origin": { "name": "..." } }`
- `{ "error": "dest_not_found", "airport": "EDDF", "dest": { "name": "..." } }`
- `{ "error": "missing_coordinates", "airport": "EDDF", "origin": {...}, "dest": {...} }`
- `{ "error": "no_nodes_in_area", "airport": "EDDF", "origin": {...}, "dest": {...} }`

For the Python backend, prefer FastAPI `HTTPException` with real status codes,
but preserve the same error codes in the response body for client compatibility.

### GET `/api/service/tools/airport-geocode`

Purpose: resolve named aerodrome features to coordinates, or coordinates to the
nearest named aerodrome feature, for one airport.

Query parameters:

- `airport`: ICAO code. Required.
- `origin_name`, `origin_lat`, `origin_lng` or `origin_lon`.
- `dest_name`, `dest_lat`, `dest_lng` or `dest_lon`.

Example:

```bash
curl "https://opensquawk.de/api/service/tools/airport-geocode?airport=EDDF&origin_name=Stand%20V155&dest_lat=50.0474&dest_lng=8.5612"
```

Response shape:

```json
{
  "airport": "EDDF",
  "feature_count": 284,
  "origin": {
    "query": { "name": "Stand V155", "lat": null, "lon": null },
    "result": {
      "type": "stand",
      "name": "V155",
      "lat": 50.046321,
      "lon": 8.576842,
      "matched_alias": "V155",
      "primary_alias": "V155",
      "map_url": "https://www.openstreetmap.org/node/1234567890?mlat=50.046321&mlon=8.576842#map=19/50.046321/8.576842",
      "osm": { "type": "node", "id": 1234567890, "tags": {} },
      "source": "name",
      "distance_m": null
    }
  },
  "dest": {
    "query": { "name": null, "lat": 50.0474, "lon": 8.5612 },
    "result": {
      "type": "runway",
      "name": "25C",
      "lat": 50.04726,
      "lon": 8.5611,
      "matched_alias": "25C",
      "primary_alias": "25C",
      "map_url": "https://www.openstreetmap.org/way/987654321?mlat=50.047260&mlon=8.561100#map=17/50.047260/8.561100",
      "osm": { "type": "way", "id": 987654321, "tags": {} },
      "source": "coordinate",
      "distance_m": 9.2
    }
  }
}
```

## Overpass Queries

Use endpoint:

```text
https://overpass-api.de/api/interpreter
```

POST body format used by Nuxt:

```text
data=<urlencoded query>
```

Content-Type:

```text
application/x-www-form-urlencoded
```

### Airport Feature Query

This query finds the OSM aerodrome area by ICAO and fetches relevant nodes/ways
inside it. Ways are returned with `center` and tags, not their full geometry.

```overpass
[out:json][timeout:60];
(
  area["aeroway"="aerodrome"]["ref:icao"="{AIRPORT}"];
  area["aeroway"="aerodrome"]["icao"="{AIRPORT}"];
  area["aeroway"="aerodrome"]["ref"="{AIRPORT}"];
)->.airport;
(
  node(area.airport)["aeroway"="parking_position"];
  way(area.airport)["aeroway"="parking_position"];
  node(area.airport)["aeroway"="gate"];
  way(area.airport)["aeroway"="gate"];
  node(area.airport)["aeroway"="runway"];
  node(area.airport)["aeroway"="taxiway"];
  node(area.airport)["aeroway"="holding_position"];
  way(area.airport)["aeroway"="runway"];
  way(area.airport)["aeroway"="taxiway"];
  way(area.airport)["aeroway"="holding_position"];
);
out center tags;
```

Only features with at least one alias are retained.

### Taxiway Graph Query

After both endpoint coordinates are known, fetch taxiway ways around each
endpoint, plus all child nodes for those ways.

```overpass
[out:json][timeout:90];
(
  way["aeroway"="taxiway"](around:{RADIUS},{ORIGIN_LAT},{ORIGIN_LON});
  way["aeroway"="taxiway"](around:{RADIUS},{DEST_LAT},{DEST_LON});
);
(._;>;);
out body;
```

The old service only includes OSM `aeroway=taxiway` ways in the routing graph.
It does not include `aeroway=runway`, service roads, aprons, gates, or stand
lead-in lines unless they are tagged as taxiways.

## Data Model

Recommended Python/Pydantic equivalents:

```python
from typing import Literal
from pydantic import BaseModel

FeatureType = Literal[
    "runway",
    "gate",
    "parking_position",
    "taxiway",
    "holding_position",
    "stand",
    "unknown",
]

class AirportFeature(BaseModel):
    osm_type: Literal["node", "way"]
    osm_id: int
    type: FeatureType
    lat: float
    lon: float
    tags: dict[str, str]
    aliases: list[str]
    primary_alias: str
    normalized_aliases: dict[str, str]

class GeocodeQuery(BaseModel):
    name: str | None = None
    lat: float | None = None
    lon: float | None = None

class GeocodeMatch(BaseModel):
    feature: AirportFeature
    matched_alias: str | None
    distance_meters: float | None = None
    source: Literal["name", "coordinate"]
```

Internal graph types can stay simple:

```python
Node = dict[str, float | int]  # id, lat, lon
Edge = tuple[int, int, float, int, str | None]  # u, v, distance_m, way_id, name
```

## Airport Feature Geocoder Behavior

### Feature Types

Derive the feature type from `tags["aeroway"]`:

- `parking_position` -> feature type `parking_position`, but output type `stand`
- `gate` -> `gate`
- `runway` -> `runway`
- `taxiway` -> `taxiway`
- `holding_position` -> `holding_position`
- anything else -> `unknown`

### Feature Coordinates

- OSM nodes use `lat` and `lon` directly.
- OSM ways use the Overpass `center.lat` and `center.lon`.
- If a node/way has no usable coordinate, skip it.

Important known limitation: using the center of a runway way is often wrong for
routing, because a runway is a long geometry. See "Runway endpoint selection" in
Known Gaps below.

### Alias Building

For each feature, collect aliases from tags in this order:

1. `ref`
2. `designation`, if different from `ref`
3. `name`, if different from `ref`
4. `ref:icao`, if different from `ref`
5. For runways only: `ref:runway` or `ref:icao:runway`
6. For parking positions only: `ref:stand`

For runway aliases, split combined candidates on `/`, `;`, and `,` and add the
individual runway ends as aliases too. For example `07L/25R` should produce
aliases for `07L`, `25R`, and `07L/25R`.

The first non-empty alias is the `primary_alias`. Skip any feature with no
aliases.

### Normalization

Normalize text by uppercasing and removing every non-alphanumeric character:

```python
def normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())
```

For each alias or query, generate variants:

- the base normalized string
- if it starts with `RUNWAY ` or `RWY `, add the normalized suffix
- if it starts with `GATE ` or `STAND `, add the normalized suffix
- if it contains multiple whitespace-separated tokens, add the normalized last token

Examples:

- `RWY 25C` -> `RWY25C`, `25C`
- `Runway 07L` -> `RUNWAY07L`, `07L`
- `Gate A5` -> `GATEA5`, `A5`
- `Stand V155` -> `STANDV155`, `V155`

### Query Analysis

Before matching names, strip common feature words from the user query:

- `runway`, `rwy`
- `gate`
- `stand`, `parking`, `standposition`
- `taxiway`, `taxi`
- `holding`, `holdshort`, `holdingpoint`

Collapse whitespace after stripping. Use the sanitized query if not empty,
otherwise use the original trimmed query.

Set `runway_bias` if any of these are true:

- query starts with a digit
- query contains `runway` or `rwy`
- query contains a runway-like token matching `\d{1,2}[LRC]?`
- query contains paired runway ends matching `\d{2}\s*/\s*\d{2}`

### Name Matching Score

For each query variant and each feature alias variant:

- exact match -> score 100
- either string contains the other -> score 70
- otherwise no match

Then adjust:

- if `runway_bias` and feature type is `runway`, add 20
- if `runway_bias` and feature type is not `runway`, subtract 10
- if feature type is `runway` and the query variant starts with a digit, add 10
- if feature type is `gate` or `stand`, add 2

Return the highest-scoring feature. Include the original alias that matched.

### Coordinate Matching

For coordinate queries, return the closest airport feature by haversine
distance. The old code did not enforce a maximum distance threshold.

Haversine:

```python
EARTH_RADIUS_M = 6371000

def haversine(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    d_lat = math.radians(b_lat - a_lat)
    d_lon = math.radians(b_lon - a_lon)
    lat1 = math.radians(a_lat)
    lat2 = math.radians(b_lat)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))
```

### Resolve Order

`resolve_feature(features, query)` behavior:

1. If `query.name` exists, try name matching first.
2. If name matching succeeds, return source `name`.
3. Otherwise, if `query.lat` and `query.lon` exist, return closest feature with
   source `coordinate`.
4. Otherwise return `None`.

The taxi-route endpoint uses this to turn missing endpoint coordinates into
feature center coordinates. If both name and coordinates are provided, the old
route keeps the provided coordinates but still tries to resolve the name so the
response can include feature metadata.

## Taxi Route Algorithm

1. Parse query parameters.
2. Require origin and destination. Each may be coordinates or name.
3. If either endpoint needs name resolution, require `airport` and fetch airport
   features with the airport feature Overpass query.
4. Resolve missing endpoint coordinates using feature names.
5. Fetch taxiway ways around both endpoint coordinates with the taxiway graph
   Overpass query.
6. Build a node map from OSM node elements: `id -> {id, lat, lon}`.
7. Keep OSM way elements separately.
8. For every taxiway way, connect each adjacent node pair as bidirectional
   weighted edges.
9. Edge weight is haversine distance in meters between adjacent OSM nodes.
10. Edge name is `way.tags.name` if present, otherwise `way.tags.ref`, otherwise
    `None`.
11. Build adjacency list: `node_id -> [(neighbor_id, distance_m), ...]`.
12. Build edge metadata lookup: `"u->v" -> {name, way_id}`.
13. Snap origin coordinate to the nearest graph node.
14. Snap destination coordinate to the nearest graph node.
15. Run Dijkstra from snapped origin node to snapped destination node.
16. If no path is found, return `route: null`, empty `names`, and empty
    `names_collapsed`, but still include endpoint metadata and attachments.
17. If a path is found, return all path node IDs and total distance.
18. Derive taxiway name sequences along the path.

### Dijkstra Details

The old TypeScript used a simple sorted array as the priority queue:

- initialize every node distance to infinity
- set source distance to 0
- repeatedly pop the lowest-distance unvisited node
- relax outgoing edges
- store `prev[v] = u` when improving distance
- reconstruct the path by walking backward from destination to source

For Python, use `heapq` instead of sorting the whole queue on every insertion.

### Taxiway Name Extraction

Given the node ID path:

1. For each consecutive pair `(u, v)`, find edge metadata for `u->v` or `v->u`.
2. Read `name`; skip empty/unnamed edges.
3. Append it to `names` only if it is different from the immediately previous
   appended name.

Then produce `names_collapsed`:

1. Iterate over `names`.
2. Skip exact duplicates.
3. Compare the alphabetic prefix of the previous collapsed name and current
   name after removing whitespace and uppercasing.
4. If the prefixes are equal, the previous name contains digits, and the current
   name contains digits, skip the current name.
5. Otherwise append it.

This turns routes like `["L7", "L", "N"]` into `["L7", "N"]` in the documented
example. More generally it tries to avoid noisy consecutive variants of the same
taxiway family, while preserving the raw `names` list.

Equivalent helper:

```python
def alpha_prefix(value: str) -> str | None:
    compact = re.sub(r"\s+", "", value)
    match = re.match(r"^([A-Za-z]+)", compact)
    return match.group(1).upper() if match else None

def collapse_names(names: list[str]) -> list[str]:
    collapsed: list[str] = []
    for name in names:
        last = collapsed[-1] if collapsed else None
        if last == name:
            continue

        last_prefix = alpha_prefix(last) if last else None
        current_prefix = alpha_prefix(name)
        last_has_digits = bool(last and re.search(r"\d", last))
        current_has_digits = bool(re.search(r"\d", name))

        if (
            last_prefix
            and current_prefix
            and last_prefix == current_prefix
            and last_has_digits
            and current_has_digits
        ):
            continue

        collapsed.append(name)
    return collapsed
```

## Known Gaps To Fix During Reimplementation

### Runway Endpoint Selection

The old code has a German TODO in `taxiroute.get.ts`:

When an endpoint is provided by runway name, first resolve it as a feature and
detect that the matched feature is a runway. The caller should be able to choose
whether to use the start or end node of the runway way. The current code uses
the center of the OSM runway way, which is poor for routing because the runway
geometry is a long line. Default should be runway start.

Suggested Python API:

- `origin_runway_point=start|end|center`, default `start`
- `dest_runway_point=start|end|center`, default `start`

Implementation detail:

- The existing airport feature query returns only `center` for ways, so it is
  insufficient for runway endpoint selection.
- If a matched named feature is a runway way and the requested point is `start`
  or `end`, fetch the full way geometry or include child nodes in the airport
  feature query.
- Use first node for `start`, last node for `end`. If runway direction matters
  later, derive it from the runway designator and geometry bearing. For now,
  reproduce the TODO semantics: caller chooses start or end explicitly.

### Graph Coverage

The old route graph only includes `aeroway=taxiway`. That can fail when OSM maps
stands, apron lead-ins, runway turnoffs, or holding positions as separate feature
types. Consider optionally adding:

- `aeroway=parking_position`
- `aeroway=holding_position`
- `aeroway=apron` edges if represented as ways
- selected runway connector segments, with a high penalty if needed

Keep default behavior compatible first, then add broadened graph coverage behind
a query flag such as `include_connectors=true`.

### Radius And Airport Scope

The taxiway graph query searches by radius around origin and destination, not by
airport area. On very large airports, too-small radius can miss middle taxiways.
On airports close to each other, large radius can include unrelated taxiways.

Safer future behavior:

- If `airport` is provided, query taxiways inside the airport area.
- Optionally add around-filters for performance.
- Keep `radius` as a fallback or limiter.

### No Path Diagnostics

The old response returns `route: null` without explaining graph size or component
separation. Add diagnostics in Python, at least internally:

- number of OSM nodes
- number of taxiway ways
- number of graph edges
- snapped endpoint distances
- whether source and destination are in different connected components

### Caching And Overpass Safety

The old code calls Overpass on every request. Reimplementation should cache:

- airport feature results by ICAO
- taxiway graph results by airport or by rounded endpoint/radius bucket

Recommended cache: simple in-memory TTL first. Do not make every radio session
block on Overpass if this is later called from flow generation.

Also set an HTTP client timeout. Python dependencies currently include FastAPI,
PyYAML, Pydantic, and Jellyfish. Add `httpx` as a runtime dependency or use the
standard library, but `httpx.AsyncClient` is cleaner for FastAPI.

## Suggested Python Backend Layout

Add:

- `app/airport_geocode.py` - Overpass feature fetch, alias normalization, matching.
- `app/taxi_routing.py` - graph construction, nearest-node snap, Dijkstra, name collapse.
- `app/routes/tool_routes.py` or `app/routes/airport_tool_routes.py` - FastAPI routes.
- `tests/test_airport_geocode_unit.py` - pure unit tests with fixture OSM elements.
- `tests/test_taxi_routing_unit.py` - pure graph tests without network.
- `tests/test_tool_routes.py` - route contract tests with mocked Overpass client.

Register the new router in `main.py`:

```python
from app.routes.tool_routes import router as tool_router

app.include_router(tool_router)
```

Suggested route prefix:

```python
router = APIRouter(prefix="/api/service/tools", tags=["tools"])
```

This preserves the old public paths:

- `/api/service/tools/airport-geocode`
- `/api/service/tools/taxiroute`

## Minimal Test Plan

Pure unit tests:

- alias normalization: `RWY 25C`, `Runway 25C`, `25C`, `Gate A5`, `Stand V155`
- runway split aliases: `07L/25R` matches both `07L` and `25R`
- runway bias: query `25C` prefers runway over a stand/gate with similar alias
- coordinate resolve returns closest feature and distance
- graph construction creates bidirectional edges with haversine weights
- nearest-node snap returns node ID and distance
- Dijkstra returns expected path and total distance on a tiny fixture graph
- no-path returns `None`
- raw names remove only consecutive duplicates
- collapsed names remove noisy same-prefix digit variants

Route tests with mocked Overpass responses:

- coordinate-to-coordinate route succeeds without airport
- name-to-name route requires airport
- missing origin/destination errors
- origin or destination name not found errors
- no taxiway nodes error
- no path response includes endpoint metadata and attachments
- successful response matches old keys: `airport`, `origin`, `dest`,
  `start_attach`, `end_attach`, `route`, `names`, `names_collapsed`

Optional integration test:

- Mark as slow/network and skip by default.
- Query a known small airport with stable OSM data.
- Assert only broad invariants, not exact node IDs.

## Implementation Notes For The Next AI

Start with pure functions and tests, then add FastAPI routes. Do not couple this
to the radio decision engine at first. The old service was a standalone public
tool endpoint.

The high-value compatibility behavior is:

- accepts coordinates or names for each endpoint
- name resolution uses OSM airport features and fuzzy-ish alias matching
- route is calculated over OSM taxiway way nodes
- endpoint coordinates are snapped to nearest taxiway graph node
- shortest path is by physical distance in meters
- response exposes both raw taxiway names and collapsed taxiway names

The high-value improvement is runway endpoint selection, because the old Nuxt
code explicitly identified the runway-center behavior as wrong.

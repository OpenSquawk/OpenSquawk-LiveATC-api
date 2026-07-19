"""OpenStreetMap aerodrome feature lookup and matching.

This module is intentionally independent from the radio flow engine. It turns
OSM/Overpass aerodrome features into named points that taxi routing can use.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Literal

FeatureType = Literal[
    "runway",
    "gate",
    "parking_position",
    "taxiway",
    "holding_position",
    "stand",
    "unknown",
]
OsmType = Literal["node", "way"]
RunwayPoint = Literal["start", "end", "center"]

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
EARTH_RADIUS_M = 6_371_000


class OverpassError(RuntimeError):
    """Raised when Overpass cannot return usable OSM JSON."""


class FeatureNotFoundError(LookupError):
    """Raised when a named or coordinate feature cannot be resolved."""


@dataclass(frozen=True)
class AirportFeature:
    osm_type: OsmType
    osm_id: int
    type: FeatureType
    lat: float
    lon: float
    tags: dict[str, str]
    aliases: list[str]
    primary_alias: str
    normalized_aliases: dict[str, str]
    node_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class GeocodeQuery:
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    runway_point: RunwayPoint = "start"


@dataclass(frozen=True)
class GeocodeMatch:
    feature: AirportFeature
    matched_alias: str | None
    source: Literal["name", "coordinate"]
    distance_meters: float | None = None


class OverpassClient:
    """Tiny synchronous Overpass client with an in-memory TTL cache."""

    def __init__(
        self,
        endpoint: str = OVERPASS_ENDPOINT,
        timeout_seconds: float = 20.0,
        cache_ttl_seconds: float = 900.0,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def fetch_json(self, query: str) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._cache.get(query)
        if cached and now - cached[0] <= self.cache_ttl_seconds:
            return cached[1]

        body = urllib.parse.urlencode({"data": query}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                # overpass-api.de answers 406 to the Python default UA;
                # without this header every live route computation fails.
                "User-Agent": "OpenSquawk-LiveATC/1.0 (https://opensquawk.de)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except Exception as exc:  # pragma: no cover - network failure shape varies
            raise OverpassError(f"Overpass request failed: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OverpassError("Overpass returned invalid JSON") from exc

        if not isinstance(parsed, dict):
            raise OverpassError("Overpass returned an unexpected JSON shape")

        self._cache[query] = (now, parsed)
        return parsed


def haversine_distance(
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    """Distance in meters between two lat/lon coordinates."""

    a_lat, a_lon = a
    b_lat, b_lon = b
    d_lat = math.radians(b_lat - a_lat)
    d_lon = math.radians(b_lon - a_lon)
    lat1 = math.radians(a_lat)
    lat2 = math.radians(b_lat)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def normalize_with_prefixes(value: str) -> set[str]:
    variants: set[str] = set()
    trimmed = value.strip().upper()
    if not trimmed:
        return variants

    base = normalize(trimmed)
    if base:
        variants.add(base)

    without_runway = re.sub(r"^(RUNWAY|RWY)\s+", "", trimmed)
    if without_runway != trimmed:
        normalized = normalize(without_runway)
        if normalized:
            variants.add(normalized)

    without_gate = re.sub(r"^(GATE|STAND)\s+", "", trimmed)
    if without_gate != trimmed:
        normalized = normalize(without_gate)
        if normalized:
            variants.add(normalized)

    tokens = trimmed.split()
    if len(tokens) > 1:
        last_token = normalize(tokens[-1])
        if last_token:
            variants.add(last_token)

    return variants


def _derive_feature_type(tags: dict[str, str]) -> FeatureType:
    aeroway = tags.get("aeroway")
    if aeroway == "parking_position":
        return "parking_position"
    if aeroway == "gate":
        return "gate"
    if aeroway == "runway":
        return "runway"
    if aeroway == "taxiway":
        return "taxiway"
    if aeroway == "holding_position":
        return "holding_position"
    return "unknown"


def _build_aliases(tags: dict[str, str], feature_type: FeatureType) -> tuple[list[str], str | None]:
    aliases: list[str] = []
    seen: set[str] = set()

    def add_alias(value: str | None) -> None:
        if value is None:
            return
        trimmed = value.strip()
        if not trimmed or trimmed in seen:
            return
        seen.add(trimmed)
        aliases.append(trimmed)

    candidates: list[str] = []
    ref = tags.get("ref")
    name = tags.get("name")
    designation = tags.get("designation")
    icao = tags.get("ref:icao")

    if ref:
        candidates.append(ref)
    if designation and designation != ref:
        candidates.append(designation)
    if name and name != ref:
        candidates.append(name)
    if icao and icao != ref:
        candidates.append(icao)

    if feature_type == "runway":
        runway = tags.get("ref:runway") or tags.get("ref:icao:runway")
        if runway:
            candidates.append(runway)

    for candidate in candidates:
        if feature_type == "runway":
            parts = re.split(r"[/;,]", candidate)
            if len(parts) > 1:
                for part in parts:
                    add_alias(part)
        add_alias(candidate)

    if feature_type == "parking_position":
        add_alias(tags.get("ref:stand"))

    return aliases, aliases[0] if aliases else None


def create_feature(element: dict[str, Any]) -> AirportFeature | None:
    osm_type = element.get("type")
    if osm_type not in ("node", "way"):
        return None

    tags = {
        str(key): str(value)
        for key, value in (element.get("tags") or {}).items()
        if value is not None
    }
    feature_type = _derive_feature_type(tags)

    if osm_type == "node":
        lat = element.get("lat")
        lon = element.get("lon")
    else:
        center = element.get("center") or {}
        lat = center.get("lat")
        lon = center.get("lon")

    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None

    aliases, primary_alias = _build_aliases(tags, feature_type)
    if not aliases or primary_alias is None:
        return None

    normalized_aliases: dict[str, str] = {}
    for alias in aliases:
        for variant in normalize_with_prefixes(alias):
            normalized_aliases.setdefault(variant, alias)

    if not normalized_aliases:
        return None

    node_ids = tuple(
        node_id
        for node_id in element.get("nodes", [])
        if isinstance(node_id, int)
    )

    output_type: FeatureType = "stand" if feature_type == "parking_position" else feature_type
    return AirportFeature(
        osm_type=osm_type,
        osm_id=int(element["id"]),
        type=output_type,
        lat=float(lat),
        lon=float(lon),
        tags=tags,
        aliases=aliases,
        primary_alias=primary_alias,
        normalized_aliases=normalized_aliases,
        node_ids=node_ids,
    )


def _analyze_query(query: str) -> tuple[str, str, bool]:
    trimmed = query.strip()
    if not trimmed:
        return "", "", False

    sanitized = trimmed
    for pattern in (
        r"\b(runway|rwy)\b",
        r"\b(gate)\b",
        r"\b(stand|parking|standposition)\b",
        r"\b(taxiway|taxi)\b",
        r"\b(holding|holdshort|holdingpoint)\b",
    ):
        sanitized = re.sub(pattern, " ", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()

    runway_bias = (
        bool(re.match(r"^\s*\d", trimmed))
        or bool(re.search(r"\b(runway|rwy)\b", query, flags=re.IGNORECASE))
        or bool(re.search(r"\d{1,2}[LRC]?\b", trimmed, flags=re.IGNORECASE))
        or bool(re.search(r"\d{2}\s*/\s*\d{2}", trimmed))
    )
    return trimmed, sanitized, runway_bias


def match_feature_by_name(features: list[AirportFeature], query: str) -> GeocodeMatch | None:
    if not query:
        return None

    trimmed, sanitized, runway_bias = _analyze_query(query)
    base_query = sanitized or trimmed
    if not base_query:
        return None

    variants = normalize_with_prefixes(base_query)
    if not variants:
        return None

    best: tuple[int, AirportFeature, str] | None = None
    for feature in features:
        for alias_variant, original_alias in feature.normalized_aliases.items():
            for variant in variants:
                if alias_variant == variant:
                    score = 100
                elif alias_variant in variant or variant in alias_variant:
                    score = 70
                else:
                    continue

                if runway_bias:
                    score += 20 if feature.type == "runway" else -10
                if feature.type == "runway" and re.match(r"^\d", variant):
                    score += 10
                if feature.type in ("gate", "stand"):
                    score += 2

                if best is None or score > best[0]:
                    best = (score, feature, original_alias)

    if best is None:
        return None

    _, feature, original_alias = best
    return GeocodeMatch(feature=feature, matched_alias=original_alias, source="name")


def match_feature_by_coordinate(
    features: list[AirportFeature],
    lat: float,
    lon: float,
) -> GeocodeMatch | None:
    best: tuple[float, AirportFeature] | None = None
    for feature in features:
        distance_m = haversine_distance((lat, lon), (feature.lat, feature.lon))
        if best is None or distance_m < best[0]:
            best = (distance_m, feature)

    if best is None:
        return None

    distance_m, feature = best
    return GeocodeMatch(
        feature=feature,
        matched_alias=feature.primary_alias,
        distance_meters=distance_m,
        source="coordinate",
    )


def resolve_feature(features: list[AirportFeature], query: GeocodeQuery) -> GeocodeMatch | None:
    if query.name:
        match = match_feature_by_name(features, query.name)
        if match is not None:
            return match

    if query.lat is not None and query.lon is not None:
        return match_feature_by_coordinate(features, query.lat, query.lon)

    return None


def build_map_url(feature: AirportFeature) -> str:
    zoom = 17 if feature.type == "runway" else 19
    lat = f"{feature.lat:.6f}"
    lon = f"{feature.lon:.6f}"
    return (
        f"https://www.openstreetmap.org/{feature.osm_type}/{feature.osm_id}"
        f"?mlat={lat}&mlon={lon}#map={zoom}/{lat}/{lon}"
    )


def geocode_payload(match: GeocodeMatch) -> dict[str, Any]:
    feature = match.feature
    return {
        "type": feature.type,
        "name": match.matched_alias or feature.primary_alias,
        "lat": feature.lat,
        "lon": feature.lon,
        "matched_alias": match.matched_alias,
        "primary_alias": feature.primary_alias,
        "map_url": build_map_url(feature),
        "osm": {
            "type": feature.osm_type,
            "id": feature.osm_id,
            "tags": feature.tags,
        },
        "source": match.source,
        "distance_m": match.distance_meters,
    }


def airport_features_query(airport: str) -> str:
    code = airport.strip().upper()
    return f"""
[out:json][timeout:60];
(
  area["aeroway"="aerodrome"]["ref:icao"="{code}"];
  area["aeroway"="aerodrome"]["icao"="{code}"];
  area["aeroway"="aerodrome"]["ref"="{code}"];
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
out body center;
"""


def way_nodes_query(way_id: int) -> str:
    return f"""
[out:json][timeout:30];
(
  way({way_id});
  >;
);
out body;
"""


def parse_airport_features(osm: dict[str, Any]) -> list[AirportFeature]:
    elements = osm.get("elements")
    if not isinstance(elements, list):
        return []

    features: list[AirportFeature] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        feature = create_feature(element)
        if feature is not None:
            features.append(feature)
    return features


def fetch_airport_features(airport: str, client: OverpassClient | None = None) -> list[AirportFeature]:
    overpass = client or OverpassClient()
    return parse_airport_features(overpass.fetch_json(airport_features_query(airport)))


def fetch_way_endpoints(
    way_id: int,
    client: OverpassClient | None = None,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Both geometric endpoints of a way, in node order: ``(first, last)``."""

    overpass = client or OverpassClient()
    osm = overpass.fetch_json(way_nodes_query(way_id))
    elements = osm.get("elements")
    if not isinstance(elements, list):
        return None

    way_nodes: list[int] = []
    nodes: dict[int, tuple[float, float]] = {}
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("type") == "way" and element.get("id") == way_id:
            way_nodes = [
                node_id
                for node_id in element.get("nodes", [])
                if isinstance(node_id, int)
            ]
        elif element.get("type") == "node":
            node_id = element.get("id")
            lat = element.get("lat")
            lon = element.get("lon")
            if isinstance(node_id, int) and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                nodes[node_id] = (float(lat), float(lon))

    if not way_nodes:
        return None

    first = nodes.get(way_nodes[0])
    last = nodes.get(way_nodes[-1])
    if first is None or last is None:
        return None
    return first, last


def fetch_way_endpoint(
    way_id: int,
    runway_point: RunwayPoint,
    client: OverpassClient | None = None,
) -> tuple[float, float] | None:
    if runway_point == "center":
        return None
    endpoints = fetch_way_endpoints(way_id, client=client)
    if endpoints is None:
        return None
    return endpoints[0] if runway_point == "start" else endpoints[1]


_RUNWAY_DESIGNATOR_RE = re.compile(r"^(\d{2})[LRC]?$")


def _designator_heading_degrees(alias: str) -> float | None:
    """Magnetic heading a runway-end designator implies ("25R" -> 250)."""
    match = _RUNWAY_DESIGNATOR_RE.match(alias.strip().upper().replace(" ", ""))
    if match is None:
        return None
    return int(match.group(1)) * 10.0


def _bearing_degrees(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial great-circle bearing from ``a`` to ``b`` in [0, 360)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d_lon = lon2 - lon1
    x = math.sin(d_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _angle_difference(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def apply_runway_point(
    match: GeocodeMatch,
    runway_point: RunwayPoint,
    client: OverpassClient | None = None,
) -> GeocodeMatch:
    """Move a named runway-way match to the requested runway point.

    ``start`` means the **threshold of the matched designator** — the holding
    point a departure lines up at — and ``end`` the far end a landing rolls
    out toward. The OSM way's node order is arbitrary, so the ends are
    disambiguated by comparing the way's bearing with the designator heading
    ("25R" implies ~250°). Matches without a single designator (full refs like
    "07L/25R") keep the way's geometric order.
    """

    feature = match.feature
    if feature.type != "runway" or feature.osm_type != "way" or runway_point == "center":
        return match

    endpoints = fetch_way_endpoints(feature.osm_id, client=client)
    if endpoints is None:
        return match

    threshold, far_end = endpoints
    heading = _designator_heading_degrees(match.matched_alias or "")
    if heading is not None:
        # The threshold is the end you take off *from*: looking from it toward
        # the other end must roughly match the designator heading.
        if _angle_difference(_bearing_degrees(threshold, far_end), heading) > 90.0:
            threshold, far_end = far_end, threshold

    lat, lon = threshold if runway_point == "start" else far_end
    moved = replace(feature, lat=lat, lon=lon)
    return replace(match, feature=moved)


def _resolved_endpoint_payload(
    query: GeocodeQuery | None,
    features: list[AirportFeature],
    client: OverpassClient,
) -> dict[str, Any]:
    """Resolve one geocode endpoint into its `{query, result}` payload.

    Runway start/end selection is only applied to name matches; a reverse
    (coordinate) lookup should report the feature it actually snapped to rather
    than jumping to a runway threshold.
    """

    if query is None:
        return {
            "query": {"name": None, "lat": None, "lon": None},
            "result": None,
        }

    match = resolve_feature(features, query)
    if match is not None and match.source == "name":
        match = apply_runway_point(match, query.runway_point, client=client)

    return {
        "query": {"name": query.name, "lat": query.lat, "lon": query.lon},
        "result": geocode_payload(match) if match is not None else None,
    }


def geocode_airport(
    airport: str,
    origin: GeocodeQuery | None,
    dest: GeocodeQuery | None,
    client: OverpassClient | None = None,
) -> dict[str, Any]:
    """Resolve origin/dest queries against one airport's OSM features.

    Mirrors the old Nuxt `airport-geocode` response shape:
    `{airport, feature_count, origin, dest}`.
    """

    overpass = client or OverpassClient()
    features = fetch_airport_features(airport, client=overpass)
    return {
        "airport": airport.strip().upper(),
        "feature_count": len(features),
        "origin": _resolved_endpoint_payload(origin, features, overpass),
        "dest": _resolved_endpoint_payload(dest, features, overpass),
    }

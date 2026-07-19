"""Taxiway graph construction and route calculation from OSM/Overpass data."""

from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass
from typing import Any

from app.airport_geocode import (
    AirportFeature,
    GeocodeMatch,
    GeocodeQuery,
    OverpassClient,
    apply_runway_point,
    fetch_airport_features,
    geocode_payload,
    haversine_distance,
    resolve_feature,
)


class TaxiRouteError(ValueError):
    """Domain error with a stable code suitable for API responses."""

    def __init__(
        self,
        code: str,
        details: str,
        *,
        status_code: int = 400,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(details)
        self.code = code
        self.details = details
        self.status_code = status_code
        self.payload = payload or {}

    def to_payload(self) -> dict[str, Any]:
        return {"error": self.code, "details": self.details, **self.payload}


@dataclass(frozen=True)
class GraphNode:
    id: int
    lat: float
    lon: float


@dataclass(frozen=True)
class GraphEdge:
    u: int
    v: int
    distance_m: float
    way_id: int
    name: str | None


@dataclass(frozen=True)
class Attachment:
    node_id: int
    lat: float
    lon: float
    distance_m: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "lat": self.lat,
            "lon": self.lon,
            "distance_m": self.distance_m,
        }


@dataclass
class TaxiwayGraph:
    nodes: dict[int, GraphNode]
    adjacency: dict[int, list[tuple[int, float]]]
    edge_meta: dict[tuple[int, int], tuple[str | None, int]]


# Surface types that may bridge gaps where OSM maps stands, apron lead-ins, or
# runway turnoffs as separate ways instead of taxiways. Opt-in via
# include_connectors; weighted higher than taxiways so real taxiways win.
_CONNECTOR_AEROWAYS = ("parking_position", "holding_position", "apron")
_CONNECTOR_PENALTY = 5.0


def _graph_aeroway_types(include_connectors: bool) -> list[str]:
    # Runways are fetched so the route can be tested for crossings; they are NOT
    # added to the routing graph (see parse_taxiway_graph).
    types = ["taxiway", "runway"]
    if include_connectors:
        types.extend(_CONNECTOR_AEROWAYS)
    return types


def taxiway_graph_query(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    radius_m: float,
    include_connectors: bool = False,
) -> str:
    """Radius-scoped graph query around both endpoints (no airport area)."""
    selectors = "\n".join(
        f'  way["aeroway"="{t}"](around:{radius_m},{lat},{lon});'
        for t in _graph_aeroway_types(include_connectors)
        for lat, lon in ((origin_lat, origin_lon), (dest_lat, dest_lon))
    )
    return f"""
[out:json][timeout:90];
(
{selectors}
);
(._;>;);
out body;
"""


def taxiway_graph_area_query(airport: str, include_connectors: bool = False) -> str:
    """Airport-area-scoped graph query: all taxiways inside the aerodrome.

    Preferred when an ICAO is known — it cannot miss middle taxiways on large
    fields the way a radius can, and the query is identical per airport so the
    Overpass TTL cache keys on the airport.
    """
    code = airport.strip().upper()
    selectors = "\n".join(
        f'  way(area.airport)["aeroway"="{t}"];'
        for t in _graph_aeroway_types(include_connectors)
    )
    return f"""
[out:json][timeout:90];
(
  area["aeroway"="aerodrome"]["ref:icao"="{code}"];
  area["aeroway"="aerodrome"]["icao"="{code}"];
  area["aeroway"="aerodrome"]["ref"="{code}"];
)->.airport;
(
{selectors}
);
(._;>;);
out body;
"""


def _collect_nodes(elements: list[Any]) -> dict[int, GraphNode]:
    nodes: dict[int, GraphNode] = {}
    for element in elements:
        if not isinstance(element, dict) or element.get("type") != "node":
            continue
        node_id = element.get("id")
        lat = element.get("lat")
        lon = element.get("lon")
        if isinstance(node_id, int) and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            nodes[node_id] = GraphNode(id=node_id, lat=float(lat), lon=float(lon))
    return nodes


def parse_taxiway_graph(osm: dict[str, Any], include_connectors: bool = False) -> TaxiwayGraph:
    elements = osm.get("elements")
    if not isinstance(elements, list):
        return TaxiwayGraph(nodes={}, adjacency={}, edge_meta={})

    # All node coordinates (incl. runway children), but only routable surfaces
    # end up in the graph so snapping never attaches to a runway node.
    all_nodes = _collect_nodes(elements)
    allowed = {"taxiway"}
    if include_connectors:
        allowed.update(_CONNECTOR_AEROWAYS)
    ways = [
        el
        for el in elements
        if isinstance(el, dict)
        and el.get("type") == "way"
        and (el.get("tags") or {}).get("aeroway") in allowed
    ]

    nodes: dict[int, GraphNode] = {}
    adjacency: dict[int, list[tuple[int, float]]] = {}
    edge_meta: dict[tuple[int, int], tuple[str | None, int]] = {}

    def add_edge(u: int, v: int, weight: float, way_id: int, name: str | None) -> None:
        adjacency.setdefault(u, []).append((v, weight))
        edge_meta[(u, v)] = (name, way_id)

    for way in ways:
        way_id = way.get("id")
        if not isinstance(way_id, int):
            continue
        tags = way.get("tags") or {}
        name = None
        if isinstance(tags, dict):
            raw_name = tags.get("name") or tags.get("ref")
            if raw_name is not None:
                name = str(raw_name)
        # Connector surfaces cost more so the router only uses them to bridge
        # gaps the taxiway network cannot.
        penalty = 1.0 if tags.get("aeroway") == "taxiway" else _CONNECTOR_PENALTY

        way_nodes = [
            node_id
            for node_id in way.get("nodes", [])
            if isinstance(node_id, int)
        ]
        for idx in range(len(way_nodes) - 1):
            a = all_nodes.get(way_nodes[idx])
            b = all_nodes.get(way_nodes[idx + 1])
            if a is None or b is None:
                continue
            nodes[a.id] = a
            nodes[b.id] = b
            weight = haversine_distance((a.lat, a.lon), (b.lat, b.lon)) * penalty
            add_edge(a.id, b.id, weight, way_id, name)
            add_edge(b.id, a.id, weight, way_id, name)

    return TaxiwayGraph(nodes=nodes, adjacency=adjacency, edge_meta=edge_meta)


def _runway_designators(tags: dict[str, Any]) -> list[str]:
    """All spoken designators for a runway way, e.g. "07L/25R" -> ["07L", "25R"]."""
    ref = tags.get("ref") or tags.get("ref:runway") or tags.get("ref:icao") or ""
    return [part.strip().upper() for part in re.split(r"[/;,]", str(ref)) if part.strip()]


def parse_runways(osm: dict[str, Any]) -> list[tuple[list[str], list[tuple[float, float]]]]:
    """Extract runway centerlines as ``(designators, [(lat, lon), ...])``."""
    elements = osm.get("elements")
    if not isinstance(elements, list):
        return []

    node_coords = {nid: (n.lat, n.lon) for nid, n in _collect_nodes(elements).items()}
    runways: list[tuple[list[str], list[tuple[float, float]]]] = []
    for el in elements:
        if not isinstance(el, dict) or el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if tags.get("aeroway") != "runway":
            continue
        designators = _runway_designators(tags)
        if not designators:
            continue
        polyline = [
            node_coords[nid]
            for nid in el.get("nodes", [])
            if isinstance(nid, int) and nid in node_coords
        ]
        if len(polyline) >= 2:
            runways.append((designators, polyline))
    return runways


def _orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    """Signed area sign of triangle abc (lon as x, lat as y)."""
    return (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])


def _segments_cross(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    """True when segment p1-p2 properly intersects segment p3-p4 (planar)."""
    d1 = _orientation(p3, p4, p1)
    d2 = _orientation(p3, p4, p2)
    d3 = _orientation(p1, p2, p3)
    d4 = _orientation(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _intersection_point(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> tuple[float, float] | None:
    """Intersection point (lat, lon) of segments p1-p2 and p3-p4, or None."""
    x1, y1, x2, y2 = p1[1], p1[0], p2[1], p2[0]
    x3, y3, x4, y4 = p3[1], p3[0], p4[1], p4[0]
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if denom == 0:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    return (y1 + t * (y2 - y1), x1 + t * (x2 - x1))


def _bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Initial compass bearing in degrees from a to b."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    d_lon = math.radians(b[1] - a[1])
    y = math.sin(d_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def _designator_heading(designator: str) -> int | None:
    match = re.match(r"^(\d{1,2})", designator)
    return int(match.group(1)) * 10 if match else None


def _announced_designator(
    designators: list[str],
    polyline: list[tuple[float, float]],
    crossing: tuple[float, float],
) -> str:
    """Pick which runway end to name, by the threshold nearer to the crossing.

    A runway way runs poly[0] -> poly[-1]; the designator whose heading matches
    that bearing has its threshold at poly[0] (you depart that threshold flying
    along the way). Name whichever threshold the aircraft crosses closer to.
    """
    if len(designators) == 1:
        return designators[0]

    bearing = _bearing(polyline[0], polyline[-1])
    start = min(
        designators,
        key=lambda d: _angle_diff(_designator_heading(d) or 0, bearing),
    )
    end = next((d for d in designators if d != start), start)
    near_start = haversine_distance(crossing, polyline[0]) <= haversine_distance(crossing, polyline[-1])
    return start if near_start else end


def detect_crossings(
    path_coords: list[tuple[float, float]],
    runways: list[tuple[list[str], list[tuple[float, float]]]],
    exclude: set[str],
) -> list[str]:
    """Runway designators the path crosses, in path order, excluding endpoints.

    Each crossing is announced by the end nearer to where the path cuts the
    runway (see ``_announced_designator``).
    """
    ordered: list[str] = []
    seen: set[str] = set()
    for i in range(len(path_coords) - 1):
        a, b = path_coords[i], path_coords[i + 1]
        for designators, polyline in runways:
            if exclude.intersection(designators):
                continue
            for j in range(len(polyline) - 1):
                if _segments_cross(a, b, polyline[j], polyline[j + 1]):
                    point = _intersection_point(a, b, polyline[j], polyline[j + 1])
                    announced = (
                        _announced_designator(designators, polyline, point)
                        if point is not None
                        else designators[0]
                    )
                    if announced not in seen:
                        ordered.append(announced)
                        seen.add(announced)
                    break
    return ordered


def nearest_node(
    graph: TaxiwayGraph,
    lat: float,
    lon: float,
    allowed: set[int] | None = None,
) -> Attachment | None:
    best: tuple[float, GraphNode] | None = None
    for node in graph.nodes.values():
        if allowed is not None and node.id not in allowed:
            continue
        distance_m = haversine_distance((lat, lon), (node.lat, node.lon))
        if best is None or distance_m < best[0]:
            best = (distance_m, node)

    if best is None:
        return None

    distance_m, node = best
    return Attachment(
        node_id=node.id,
        lat=node.lat,
        lon=node.lon,
        distance_m=distance_m,
    )


def _connected_components(graph: TaxiwayGraph) -> list[set[int]]:
    """Connected components of the taxiway graph, by node id."""
    seen: set[int] = set()
    components: list[set[int]] = []
    for start in graph.nodes:
        if start in seen:
            continue
        component = {start}
        stack = [start]
        while stack:
            u = stack.pop()
            for v, _ in graph.adjacency.get(u, []):
                if v not in component:
                    component.add(v)
                    stack.append(v)
        seen |= component
        components.append(component)
    return components


# An endpoint further than this from a component is not served by it — a stand
# or holding point sits within a few hundred metres of its taxiway network.
MAX_ATTACH_DISTANCE_M = 2000.0


def attach_endpoints(
    graph: TaxiwayGraph,
    origin: tuple[float, float],
    dest: tuple[float, float],
    max_attach_m: float = MAX_ATTACH_DISTANCE_M,
) -> tuple[Attachment | None, Attachment | None]:
    """Attach both endpoints to the graph so a path can exist between them.

    OSM airport data regularly contains disconnected taxiway fragments (an
    unconnected stub next to a runway end, a separate GA apron). Attaching each
    endpoint to its globally nearest node then fails with "different
    components" even though the main network serves both. Instead, pick the
    component that minimizes the combined attach distance of BOTH endpoints —
    but only among components that plausibly serve both (attach distance under
    ``max_attach_m``). When no component qualifies, fall back to the globally
    nearest nodes so the caller's diagnostics still describe the split.
    """
    components = _connected_components(graph)
    if not components:
        return None, None

    best: tuple[float, Attachment, Attachment] | None = None
    for component in components:
        start = nearest_node(graph, origin[0], origin[1], allowed=component)
        end = nearest_node(graph, dest[0], dest[1], allowed=component)
        if start is None or end is None:
            continue
        if start.distance_m > max_attach_m or end.distance_m > max_attach_m:
            continue
        cost = start.distance_m + end.distance_m
        if best is None or cost < best[0]:
            best = (cost, start, end)

    if best is not None:
        return best[1], best[2]
    return (
        nearest_node(graph, origin[0], origin[1]),
        nearest_node(graph, dest[0], dest[1]),
    )


def shortest_path(graph: TaxiwayGraph, src: int, dst: int) -> tuple[list[int], float] | None:
    distances = {node_id: float("inf") for node_id in graph.nodes}
    previous: dict[int, int] = {}
    distances[src] = 0.0
    queue: list[tuple[float, int]] = [(0.0, src)]
    visited: set[int] = set()

    while queue:
        current_distance, u = heapq.heappop(queue)
        if u in visited:
            continue
        visited.add(u)
        if u == dst:
            break

        for v, weight in graph.adjacency.get(u, []):
            if v in visited:
                continue
            candidate = current_distance + weight
            if candidate < distances.get(v, float("inf")):
                distances[v] = candidate
                previous[v] = u
                heapq.heappush(queue, (candidate, v))

    if src != dst and dst not in previous:
        return None

    path = [dst]
    u = dst
    while u != src:
        parent = previous.get(u)
        if parent is None:
            return None
        u = parent
        path.append(u)
    path.reverse()
    return path, distances[dst]


def path_names(graph: TaxiwayGraph, path: list[int]) -> list[str]:
    names: list[str] = []
    for idx in range(len(path) - 1):
        u = path[idx]
        v = path[idx + 1]
        meta = graph.edge_meta.get((u, v)) or graph.edge_meta.get((v, u))
        if meta is None:
            continue
        name = (meta[0] or "").strip()
        if not name:
            continue
        if not names or names[-1] != name:
            names.append(name)
    return names


def _alpha_prefix(value: str) -> str | None:
    compact = re.sub(r"\s+", "", value)
    match = re.match(r"^([A-Za-z]+)", compact)
    return match.group(1).upper() if match else None


def collapse_taxiway_names(names: list[str]) -> list[str]:
    collapsed: list[str] = []
    for name in names:
        last = collapsed[-1] if collapsed else None
        if last == name:
            continue

        last_prefix = _alpha_prefix(last) if last else None
        current_prefix = _alpha_prefix(name)
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


def _query_payload(query: GeocodeQuery, coords_provided: bool, lat: float, lon: float) -> dict[str, Any]:
    return {
        "name": query.name.strip() if query.name else None,
        "lat": lat if coords_provided else None,
        "lon": lon if coords_provided else None,
    }


def _endpoint_payload(
    lat: float,
    lon: float,
    query: GeocodeQuery,
    coords_provided: bool,
    match: GeocodeMatch | None,
) -> dict[str, Any]:
    return {
        "lat": lat,
        "lon": lon,
        "query": _query_payload(query, coords_provided, lat, lon),
        "feature": geocode_payload(match) if match is not None else None,
    }


def _resolve_endpoint(
    *,
    label: str,
    query: GeocodeQuery,
    coords_provided: bool,
    features: list[AirportFeature],
    client: OverpassClient,
) -> tuple[float, float, GeocodeMatch | None]:
    match: GeocodeMatch | None = None
    if not coords_provided:
        if not query.name:
            raise TaxiRouteError(
                f"missing_{label}",
                f"provide {label} coordinates or name",
            )
        match = resolve_feature(features, query)
        if match is None:
            raise TaxiRouteError(
                f"{label}_not_found",
                f"{label} feature not found",
                status_code=404,
                payload={label: {"name": query.name}},
            )
        match = apply_runway_point(match, query.runway_point, client=client)
        return match.feature.lat, match.feature.lon, match

    assert query.lat is not None and query.lon is not None
    lat = query.lat
    lon = query.lon
    if query.name and features:
        match = resolve_feature(features, query)
        if match is not None:
            match = apply_runway_point(match, query.runway_point, client=client)
    return lat, lon, match


def _reachable_nodes(graph: TaxiwayGraph, src: int) -> set[int]:
    """All node ids reachable from ``src`` (connected component), via DFS."""
    seen = {src}
    stack = [src]
    while stack:
        u = stack.pop()
        for v, _ in graph.adjacency.get(u, []):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


def _build_diagnostics(
    graph: TaxiwayGraph,
    start_attach: Attachment | None,
    end_attach: Attachment | None,
    *,
    same_component: bool,
) -> dict[str, Any]:
    way_ids = {way_id for _, way_id in graph.edge_meta.values()}
    return {
        "node_count": len(graph.nodes),
        "way_count": len(way_ids),
        "edge_count": sum(len(edges) for edges in graph.adjacency.values()),
        "start_attach_distance_m": start_attach.distance_m if start_attach else None,
        "end_attach_distance_m": end_attach.distance_m if end_attach else None,
        "same_component": same_component,
    }


def _fetch_graph(
    overpass: OverpassClient,
    airport_code: str,
    o_lat: float,
    o_lon: float,
    d_lat: float,
    d_lon: float,
    radius_m: float,
    include_connectors: bool,
) -> tuple[dict[str, Any], TaxiwayGraph]:
    """Prefer an airport-area graph when an ICAO is known; fall back to radius.

    The area query cannot miss middle taxiways on large fields. If the aerodrome
    area cannot be resolved (graph empty), fall back to the radius query.
    """
    if airport_code:
        osm = overpass.fetch_json(taxiway_graph_area_query(airport_code, include_connectors))
        graph = parse_taxiway_graph(osm, include_connectors=include_connectors)
        if graph.nodes:
            return osm, graph
    osm = overpass.fetch_json(
        taxiway_graph_query(o_lat, o_lon, d_lat, d_lon, radius_m, include_connectors)
    )
    return osm, parse_taxiway_graph(osm, include_connectors=include_connectors)


def calculate_taxi_route(
    *,
    origin: GeocodeQuery,
    dest: GeocodeQuery,
    airport: str | None = None,
    radius_m: float = 5000,
    include_connectors: bool = False,
    client: OverpassClient | None = None,
) -> dict[str, Any]:
    """Calculate a shortest taxiway route and return the compatibility payload.

    This is the main entry point for future API routes or flow service actions.
    It performs live Overpass reads unless the caller injects a fake client.

    When ``include_connectors`` is set, parking/holding/apron ways are added to
    the graph (weighted higher than taxiways) so routing can bridge gaps where
    OSM maps those surfaces separately.
    """

    overpass = client or OverpassClient()
    airport_code = airport.strip().upper() if airport else ""
    origin_name = origin.name.strip() if origin.name else ""
    dest_name = dest.name.strip() if dest.name else ""

    origin_coords_provided = origin.lat is not None and origin.lon is not None
    dest_coords_provided = dest.lat is not None and dest.lon is not None

    if not origin_coords_provided and not origin_name:
        raise TaxiRouteError(
            "missing_origin",
            "provide origin coordinates or name",
            payload={"airport": airport_code or None},
        )
    if not dest_coords_provided and not dest_name:
        raise TaxiRouteError(
            "missing_destination",
            "provide destination coordinates or name",
            payload={"airport": airport_code or None},
        )

    if airport_code and not re.fullmatch(r"[A-Z0-9]{3,8}", airport_code):
        raise TaxiRouteError(
            "invalid_airport",
            "airport must be a short ICAO-style code",
            payload={"airport": airport_code},
        )

    requires_geocode = (
        (not origin_coords_provided and bool(origin_name))
        or (not dest_coords_provided and bool(dest_name))
        or (bool(origin_name) and bool(airport_code))
        or (bool(dest_name) and bool(airport_code))
    )
    if requires_geocode and not airport_code:
        raise TaxiRouteError(
            "missing_airport",
            "airport is required when using feature names",
        )

    features = fetch_airport_features(airport_code, client=overpass) if requires_geocode else []

    o_lat, o_lon, origin_match = _resolve_endpoint(
        label="origin",
        query=origin,
        coords_provided=origin_coords_provided,
        features=features,
        client=overpass,
    )
    d_lat, d_lon, dest_match = _resolve_endpoint(
        label="dest",
        query=dest,
        coords_provided=dest_coords_provided,
        features=features,
        client=overpass,
    )

    graph_osm, graph = _fetch_graph(
        overpass, airport_code, o_lat, o_lon, d_lat, d_lon, radius_m, include_connectors
    )
    start_attach, end_attach = attach_endpoints(graph, (o_lat, o_lon), (d_lat, d_lon))

    origin_payload = _endpoint_payload(o_lat, o_lon, origin, origin_coords_provided, origin_match)
    dest_payload = _endpoint_payload(d_lat, d_lon, dest, dest_coords_provided, dest_match)

    if start_attach is None or end_attach is None:
        raise TaxiRouteError(
            "no_nodes_in_area",
            "no taxiway nodes found in search area",
            status_code=404,
            payload={
                "airport": airport_code or None,
                "origin": origin_payload,
                "dest": dest_payload,
            },
        )

    shortest = shortest_path(graph, start_attach.node_id, end_attach.node_id)
    if shortest is None:
        # Explain WHY there is no route: a disconnected graph (endpoints in
        # different components) reads very differently from a too-sparse one.
        same_component = end_attach.node_id in _reachable_nodes(graph, start_attach.node_id)
        return {
            "airport": airport_code or None,
            "origin": origin_payload,
            "dest": dest_payload,
            "start_attach": start_attach.to_payload(),
            "end_attach": end_attach.to_payload(),
            "route": None,
            "names": [],
            "names_collapsed": [],
            "crossings": [],
            "diagnostics": _build_diagnostics(
                graph, start_attach, end_attach, same_component=same_component
            ),
        }

    path, _ = shortest
    names = path_names(graph, path)

    # Real geometric length, independent of connector edge penalties.
    path_coords = [(graph.nodes[nid].lat, graph.nodes[nid].lon) for nid in path if nid in graph.nodes]
    total_distance_m = sum(
        haversine_distance(path_coords[i], path_coords[i + 1])
        for i in range(len(path_coords) - 1)
    )

    # Runways the route crosses, excluding the endpoint runway itself (that is
    # the holding point, announced separately as the departure/arrival runway).
    exclude = _endpoint_runway_designators(origin_match) | _endpoint_runway_designators(dest_match)
    crossings = detect_crossings(path_coords, parse_runways(graph_osm), exclude)

    return {
        "airport": airport_code or None,
        "origin": origin_payload,
        "dest": dest_payload,
        "start_attach": start_attach.to_payload(),
        "end_attach": end_attach.to_payload(),
        "route": {
            "node_ids": path,
            "total_distance_m": total_distance_m,
        },
        "names": names,
        "names_collapsed": collapse_taxiway_names(names),
        "crossings": crossings,
        "diagnostics": _build_diagnostics(graph, start_attach, end_attach, same_component=True),
    }


def _endpoint_runway_designators(match: GeocodeMatch | None) -> set[str]:
    """Normalized runway aliases for an endpoint, to exclude from crossings."""
    if match is None or match.feature.type != "runway":
        return set()
    return {alias.strip().upper() for alias in match.feature.aliases if alias.strip()}

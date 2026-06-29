"""Taxiway graph construction and route calculation from OSM/Overpass data."""

from __future__ import annotations

import heapq
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


def taxiway_graph_query(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    radius_m: float,
) -> str:
    return f"""
[out:json][timeout:90];
(
  way["aeroway"="taxiway"](around:{radius_m},{origin_lat},{origin_lon});
  way["aeroway"="taxiway"](around:{radius_m},{dest_lat},{dest_lon});
);
(._;>;);
out body;
"""


def parse_taxiway_graph(osm: dict[str, Any]) -> TaxiwayGraph:
    elements = osm.get("elements")
    if not isinstance(elements, list):
        return TaxiwayGraph(nodes={}, adjacency={}, edge_meta={})

    nodes: dict[int, GraphNode] = {}
    ways: list[dict[str, Any]] = []

    for element in elements:
        if not isinstance(element, dict):
            continue
        element_type = element.get("type")
        if element_type == "node":
            node_id = element.get("id")
            lat = element.get("lat")
            lon = element.get("lon")
            if isinstance(node_id, int) and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                nodes[node_id] = GraphNode(id=node_id, lat=float(lat), lon=float(lon))
        elif element_type == "way":
            ways.append(element)

    adjacency: dict[int, list[tuple[int, float]]] = {}
    edge_meta: dict[tuple[int, int], tuple[str | None, int]] = {}

    def add_edge(edge: GraphEdge) -> None:
        adjacency.setdefault(edge.u, []).append((edge.v, edge.distance_m))
        edge_meta[(edge.u, edge.v)] = (edge.name, edge.way_id)

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

        way_nodes = [
            node_id
            for node_id in way.get("nodes", [])
            if isinstance(node_id, int)
        ]
        for idx in range(len(way_nodes) - 1):
            a = nodes.get(way_nodes[idx])
            b = nodes.get(way_nodes[idx + 1])
            if a is None or b is None:
                continue
            distance_m = haversine_distance((a.lat, a.lon), (b.lat, b.lon))
            add_edge(GraphEdge(a.id, b.id, distance_m, way_id, name))
            add_edge(GraphEdge(b.id, a.id, distance_m, way_id, name))

    return TaxiwayGraph(nodes=nodes, adjacency=adjacency, edge_meta=edge_meta)


def nearest_node(graph: TaxiwayGraph, lat: float, lon: float) -> Attachment | None:
    best: tuple[float, GraphNode] | None = None
    for node in graph.nodes.values():
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


def calculate_taxi_route(
    *,
    origin: GeocodeQuery,
    dest: GeocodeQuery,
    airport: str | None = None,
    radius_m: float = 5000,
    client: OverpassClient | None = None,
) -> dict[str, Any]:
    """Calculate a shortest taxiway route and return the compatibility payload.

    This is the main entry point for future API routes or flow service actions.
    It performs live Overpass reads unless the caller injects a fake client.
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

    graph_osm = overpass.fetch_json(taxiway_graph_query(o_lat, o_lon, d_lat, d_lon, radius_m))
    graph = parse_taxiway_graph(graph_osm)
    start_attach = nearest_node(graph, o_lat, o_lon)
    end_attach = nearest_node(graph, d_lat, d_lon)

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
        return {
            "airport": airport_code or None,
            "origin": origin_payload,
            "dest": dest_payload,
            "start_attach": start_attach.to_payload(),
            "end_attach": end_attach.to_payload(),
            "route": None,
            "names": [],
            "names_collapsed": [],
            "diagnostics": {
                "node_count": len(graph.nodes),
                "edge_count": sum(len(edges) for edges in graph.adjacency.values()),
            },
        }

    path, total_distance_m = shortest
    names = path_names(graph, path)
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
        "diagnostics": {
            "node_count": len(graph.nodes),
            "edge_count": sum(len(edges) for edges in graph.adjacency.values()),
        },
    }

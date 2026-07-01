import pytest

from app.airport_geocode import GeocodeQuery
from app.taxi_routing import (
    TaxiRouteError,
    calculate_taxi_route,
    collapse_taxiway_names,
    detect_crossings,
    nearest_node,
    parse_taxiway_graph,
    path_names,
    shortest_path,
    taxiway_graph_area_query,
    taxiway_graph_query,
)


GRAPH_OSM = {
    "elements": [
        {"type": "node", "id": 1, "lat": 50.0, "lon": 8.0},
        {"type": "node", "id": 2, "lat": 50.0, "lon": 8.001},
        {"type": "node", "id": 3, "lat": 50.0, "lon": 8.002},
        {"type": "node", "id": 4, "lat": 50.001, "lon": 8.002},
        {"type": "way", "id": 100, "nodes": [1, 2], "tags": {"aeroway": "taxiway", "ref": "L7"}},
        {"type": "way", "id": 101, "nodes": [2, 3], "tags": {"aeroway": "taxiway", "ref": "L8"}},
        {"type": "way", "id": 102, "nodes": [3, 4], "tags": {"aeroway": "taxiway", "ref": "N"}},
    ]
}


class FakeRouteClient:
    def fetch_json(self, query: str):
        # Graph query (radius or airport-area) — recurses child nodes via (._;>;).
        if "(._;>;)" in query:
            return GRAPH_OSM
        if "way(600)" in query:  # runway endpoint geometry
            return {
                "elements": [
                    {"type": "way", "id": 600, "nodes": [601, 602]},
                    {"type": "node", "id": 601, "lat": 50.001, "lon": 8.002},
                    {"type": "node", "id": 602, "lat": 50.5, "lon": 8.5},
                ]
            }
        if 'area["aeroway"="aerodrome"]' in query:  # airport features
            return {
                "elements": [
                    {
                        "type": "node",
                        "id": 500,
                        "lat": 50.0,
                        "lon": 8.0,
                        "tags": {"aeroway": "gate", "ref": "A5"},
                    },
                    {
                        "type": "way",
                        "id": 600,
                        "center": {"lat": 50.9, "lon": 8.9},
                        "nodes": [601, 602],
                        "tags": {"aeroway": "runway", "ref": "25C"},
                    },
                ]
            }
        raise AssertionError(f"unexpected query: {query}")


def test_parse_graph_creates_bidirectional_edges_and_shortest_path():
    graph = parse_taxiway_graph(GRAPH_OSM)

    assert set(graph.nodes) == {1, 2, 3, 4}
    assert graph.adjacency[1][0][0] == 2
    assert any(to == 1 for to, _ in graph.adjacency[2])

    start = nearest_node(graph, 50.0, 8.00001)
    end = nearest_node(graph, 50.001, 8.002)

    assert start is not None
    assert end is not None

    result = shortest_path(graph, start.node_id, end.node_id)

    assert result is not None
    path, total_m = result
    assert path == [1, 2, 3, 4]
    assert total_m > 200
    assert path_names(graph, path) == ["L7", "L8", "N"]


def test_parse_graph_excludes_runway_nodes_and_edges():
    osm = {
        "elements": GRAPH_OSM["elements"]
        + [
            {"type": "node", "id": 90, "lat": 49.999, "lon": 8.0015},
            {"type": "node", "id": 91, "lat": 50.001, "lon": 8.0015},
            {"type": "way", "id": 900, "nodes": [90, 91], "tags": {"aeroway": "runway", "ref": "09/27"}},
        ]
    }
    graph = parse_taxiway_graph(osm)
    # Runway nodes must not enter the routing graph (snapping would break).
    assert set(graph.nodes) == {1, 2, 3, 4}
    assert 90 not in graph.adjacency and 91 not in graph.adjacency


def test_detect_crossings_finds_runway_intersecting_path_and_excludes_endpoint():
    path = [(50.0, 8.0), (50.0, 8.001), (50.0, 8.002), (50.001, 8.002)]
    runways = [
        # North-south runway crossing the 2->3 segment; crossing (50.0) is nearer
        # the south (36) threshold at 49.9995 than the north (18) at 50.003.
        (["18", "36"], [(49.9995, 8.0015), (50.003, 8.0015)]),
        # Destination runway crossing 1->2 → excluded by its designator.
        (["25L", "07R"], [(49.999, 8.0005), (50.001, 8.0005)]),
    ]
    assert detect_crossings(path, runways, exclude={"25L"}) == ["36"]


def test_detect_crossings_announces_nearer_runway_end():
    runway = (["18", "36"], [(49.999, 8.001), (50.005, 8.001)])  # south=36, north=18
    near_south = [(50.0005, 8.0), (50.0005, 8.002)]  # crosses near the 36 end
    near_north = [(50.0045, 8.0), (50.0045, 8.002)]  # crosses near the 18 end
    assert detect_crossings(near_south, [runway], exclude=set()) == ["36"]
    assert detect_crossings(near_north, [runway], exclude=set()) == ["18"]


def test_detect_crossings_returns_empty_when_no_intersection():
    path = [(50.0, 8.0), (50.0, 8.001)]
    runways = [(["09"], [(51.0, 9.0), (51.0, 9.001)])]
    assert detect_crossings(path, runways, exclude=set()) == []


def test_collapse_taxiway_names_removes_noisy_same_prefix_digit_variants():
    assert collapse_taxiway_names(["L7", "L8", "N"]) == ["L7", "N"]
    assert collapse_taxiway_names(["L7", "L", "N"]) == ["L7", "L", "N"]
    assert collapse_taxiway_names(["A", "A", "B"]) == ["A", "B"]


def test_calculate_taxi_route_resolves_names_and_uses_runway_start_by_default():
    result = calculate_taxi_route(
        airport="EDDF",
        origin=GeocodeQuery(name="Gate A5"),
        dest=GeocodeQuery(name="RWY 25C"),
        radius_m=2500,
        client=FakeRouteClient(),
    )

    assert result["airport"] == "EDDF"
    assert result["origin"]["feature"]["type"] == "gate"
    assert result["dest"]["feature"]["type"] == "runway"
    assert result["dest"]["lat"] == 50.001
    assert result["dest"]["lon"] == 8.002
    assert result["route"]["node_ids"] == [1, 2, 3, 4]
    assert result["names"] == ["L7", "L8", "N"]
    assert result["names_collapsed"] == ["L7", "N"]
    assert result["diagnostics"]["node_count"] == 4


def test_calculate_taxi_route_with_coordinates_does_not_require_airport():
    result = calculate_taxi_route(
        origin=GeocodeQuery(lat=50.0, lon=8.0),
        dest=GeocodeQuery(lat=50.001, lon=8.002),
        radius_m=2500,
        client=FakeRouteClient(),
    )

    assert result["airport"] is None
    assert result["origin"]["feature"] is None
    assert result["route"]["node_ids"] == [1, 2, 3, 4]


def test_name_query_without_airport_is_rejected_before_network():
    with pytest.raises(TaxiRouteError) as exc:
        calculate_taxi_route(
            origin=GeocodeQuery(name="Gate A5"),
            dest=GeocodeQuery(lat=50.001, lon=8.002),
            client=FakeRouteClient(),
        )

    assert exc.value.code == "missing_airport"
    assert exc.value.status_code == 400


# ── Known-gap: graph coverage (include_connectors) ──────────────────────────

# Two taxiway stubs 1-2 and 3-4 with a gap; only a parking_position way bridges
# nodes 2 and 3.
CONNECTOR_OSM = {
    "elements": [
        {"type": "node", "id": 1, "lat": 50.0, "lon": 8.0},
        {"type": "node", "id": 2, "lat": 50.0, "lon": 8.001},
        {"type": "node", "id": 3, "lat": 50.0, "lon": 8.002},
        {"type": "node", "id": 4, "lat": 50.0, "lon": 8.003},
        {"type": "way", "id": 100, "nodes": [1, 2], "tags": {"aeroway": "taxiway", "ref": "A"}},
        {"type": "way", "id": 101, "nodes": [3, 4], "tags": {"aeroway": "taxiway", "ref": "B"}},
        {"type": "way", "id": 200, "nodes": [2, 3], "tags": {"aeroway": "parking_position", "ref": "V1"}},
    ]
}


def test_connectors_off_leaves_gap_unbridged():
    graph = parse_taxiway_graph(CONNECTOR_OSM)
    # The parking_position way is ignored, so 1 and 4 are disconnected.
    assert shortest_path(graph, 1, 4) is None


def test_connectors_on_bridges_gap_via_parking_position():
    graph = parse_taxiway_graph(CONNECTOR_OSM, include_connectors=True)
    result = shortest_path(graph, 1, 4)
    assert result is not None
    assert result[0] == [1, 2, 3, 4]


def test_radius_query_includes_connectors_only_when_requested():
    assert "parking_position" not in taxiway_graph_query(50.0, 8.0, 50.0, 8.0, 1000)
    with_conn = taxiway_graph_query(50.0, 8.0, 50.0, 8.0, 1000, include_connectors=True)
    assert '["aeroway"="parking_position"]' in with_conn
    assert '["aeroway"="holding_position"]' in with_conn


# ── Known-gap: airport-area scope ───────────────────────────────────────────

def test_area_query_scopes_to_airport_not_radius():
    query = taxiway_graph_area_query("EDDF")
    assert 'area["aeroway"="aerodrome"]["ref:icao"="EDDF"]' in query
    assert 'way(area.airport)["aeroway"="taxiway"]' in query
    assert "around:" not in query


# ── Known-gap: no-path diagnostics ──────────────────────────────────────────

# Two taxiway components far apart, so snapped endpoints land in different ones.
DISCONNECTED_OSM = {
    "elements": [
        {"type": "node", "id": 1, "lat": 50.0, "lon": 8.0},
        {"type": "node", "id": 2, "lat": 50.0, "lon": 8.001},
        {"type": "node", "id": 3, "lat": 50.10, "lon": 8.10},
        {"type": "node", "id": 4, "lat": 50.10, "lon": 8.101},
        {"type": "way", "id": 100, "nodes": [1, 2], "tags": {"aeroway": "taxiway", "ref": "A"}},
        {"type": "way", "id": 101, "nodes": [3, 4], "tags": {"aeroway": "taxiway", "ref": "B"}},
    ]
}


class DisconnectedClient:
    def fetch_json(self, query: str):
        assert "(._;>;)" in query  # coords-only route uses the radius graph query
        return DISCONNECTED_OSM


def test_no_path_diagnostics_report_disconnected_components():
    result = calculate_taxi_route(
        origin=GeocodeQuery(lat=50.0, lon=8.0),
        dest=GeocodeQuery(lat=50.10, lon=8.10),
        client=DisconnectedClient(),
    )
    assert result["route"] is None
    diag = result["diagnostics"]
    assert diag["same_component"] is False
    assert diag["node_count"] == 4
    assert diag["way_count"] == 2
    assert diag["start_attach_distance_m"] is not None
    assert diag["end_attach_distance_m"] is not None


def test_diagnostics_report_same_component_on_success():
    result = calculate_taxi_route(
        origin=GeocodeQuery(lat=50.0, lon=8.0),
        dest=GeocodeQuery(lat=50.001, lon=8.002),
        radius_m=2500,
        client=FakeRouteClient(),
    )
    assert result["route"] is not None
    assert result["diagnostics"]["same_component"] is True
    assert result["diagnostics"]["way_count"] >= 1

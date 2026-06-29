import pytest

from app.airport_geocode import GeocodeQuery
from app.taxi_routing import (
    TaxiRouteError,
    calculate_taxi_route,
    collapse_taxiway_names,
    nearest_node,
    parse_taxiway_graph,
    path_names,
    shortest_path,
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
        if 'area["aeroway"="aerodrome"]' in query:
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
        if "way(600)" in query:
            return {
                "elements": [
                    {"type": "way", "id": 600, "nodes": [601, 602]},
                    {"type": "node", "id": 601, "lat": 50.001, "lon": 8.002},
                    {"type": "node", "id": 602, "lat": 50.5, "lon": 8.5},
                ]
            }
        if 'way["aeroway"="taxiway"]' in query:
            return GRAPH_OSM
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

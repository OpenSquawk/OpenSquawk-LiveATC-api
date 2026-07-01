"""Contract tests for /api/service/tools/* with a mocked Overpass client."""

import pytest
from fastapi.testclient import TestClient

from app.routes.tool_routes import get_overpass_client
from main import app

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

AIRPORT_FEATURES = {
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


class FakeOverpassClient:
    """Routes Overpass queries to canned OSM fixtures by query content."""

    def fetch_json(self, query: str):
        # Graph query (radius or airport-area) — recurses child nodes via (._;>;).
        if "(._;>;)" in query:
            return GRAPH_OSM
        if "way(600)" in query:
            return {
                "elements": [
                    {"type": "way", "id": 600, "nodes": [601, 602]},
                    {"type": "node", "id": 601, "lat": 50.001, "lon": 8.002},
                    {"type": "node", "id": 602, "lat": 50.5, "lon": 8.5},
                ]
            }
        if 'area["aeroway"="aerodrome"]' in query:
            return AIRPORT_FEATURES
        raise AssertionError(f"unexpected query: {query}")


@pytest.fixture
def client():
    app.dependency_overrides[get_overpass_client] = FakeOverpassClient
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_overpass_client, None)


def test_taxiroute_by_coordinates_does_not_require_airport(client):
    resp = client.get(
        "/api/service/tools/taxiroute",
        params={
            "origin_lat": 50.0,
            "origin_lng": 8.0,
            "dest_lat": 50.001,
            "dest_lng": 8.002,
            "radius": 2500,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {
        "airport",
        "origin",
        "dest",
        "start_attach",
        "end_attach",
        "route",
        "names",
        "names_collapsed",
    }
    assert body["airport"] is None
    assert body["route"]["node_ids"] == [1, 2, 3, 4]
    assert body["names_collapsed"] == ["L7", "N"]


def test_taxiroute_by_name_resolves_and_uses_runway_start(client):
    resp = client.get(
        "/api/service/tools/taxiroute",
        params={"airport": "EDDF", "origin_name": "Gate A5", "dest_name": "RWY 25C", "radius": 2500},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin"]["feature"]["type"] == "gate"
    assert body["dest"]["feature"]["type"] == "runway"
    # Runway resolved to its start node (50.001, 8.002), not the way center.
    assert body["dest"]["lat"] == 50.001
    assert body["dest"]["lon"] == 8.002
    assert body["route"]["node_ids"] == [1, 2, 3, 4]


def test_taxiroute_lon_alias_is_accepted(client):
    resp = client.get(
        "/api/service/tools/taxiroute",
        params={
            "origin_lat": 50.0,
            "origin_lon": 8.0,
            "dest_lat": 50.001,
            "dest_lon": 8.002,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["route"]["node_ids"] == [1, 2, 3, 4]


def test_taxiroute_missing_origin_returns_400_with_error_code(client):
    resp = client.get(
        "/api/service/tools/taxiroute",
        params={"dest_lat": 50.001, "dest_lng": 8.002},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_origin"


def test_taxiroute_name_without_airport_returns_400(client):
    resp = client.get(
        "/api/service/tools/taxiroute",
        params={"origin_name": "Gate A5", "dest_lat": 50.001, "dest_lng": 8.002},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_airport"


def test_taxiroute_origin_name_not_found_returns_404(client):
    resp = client.get(
        "/api/service/tools/taxiroute",
        params={"airport": "EDDF", "origin_name": "Gate ZZZ", "dest_name": "RWY 25C"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "origin_not_found"
    assert body["origin"]["name"] == "Gate ZZZ"


def test_airport_geocode_resolves_name_and_coordinate(client):
    resp = client.get(
        "/api/service/tools/airport-geocode",
        params={
            "airport": "EDDF",
            "origin_name": "Gate A5",
            "dest_lat": 50.0011,
            "dest_lng": 8.0021,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["airport"] == "EDDF"
    assert body["feature_count"] == 2
    assert body["origin"]["result"]["type"] == "gate"
    assert body["origin"]["result"]["source"] == "name"
    assert body["dest"]["result"]["source"] == "coordinate"
    assert body["dest"]["result"]["distance_m"] is not None


def test_airport_geocode_requires_airport(client):
    resp = client.get(
        "/api/service/tools/airport-geocode",
        params={"airport": "   ", "origin_name": "Gate A5"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_airport"

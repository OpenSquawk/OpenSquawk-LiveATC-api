"""Tests for best-effort taxi route computation at session creation."""

from app.taxi_route_service import (
    maybe_compute_taxi_route,
    phoneticize_route,
    resolve_taxi_route_names,
)


def test_phoneticize_route_expands_designators_only():
    assert phoneticize_route("L7, N") == "Lima 7, November"
    assert phoneticize_route("A, B") == "Alpha, Bravo"
    assert phoneticize_route("WA") == "Whiskey Alpha"
    # A real spelled-out taxiway name is left untouched, not re-expanded.
    assert phoneticize_route("Outer, M2") == "Outer, Mike 2"

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
                    {"type": "node", "id": 500, "lat": 50.0, "lon": 8.0, "tags": {"aeroway": "parking_position", "ref": "A12"}},
                    {
                        "type": "way",
                        "id": 600,
                        "center": {"lat": 50.9, "lon": 8.9},
                        "nodes": [601, 602],
                        "tags": {"aeroway": "runway", "ref": "25L"},
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


class EmptyClient:
    def fetch_json(self, query: str):
        return {"elements": []}


def test_resolve_returns_collapsed_route_string():
    route = resolve_taxi_route_names(
        icao="EDDF",
        origin_name="A12",
        dest_name="25L",
        client=FakeRouteClient(),
    )
    assert route == "Lima 7, November"


def test_resolve_returns_none_when_feature_not_found():
    # Empty airport features -> origin/dest cannot be resolved -> fallback.
    route = resolve_taxi_route_names(
        icao="EDDF",
        origin_name="A12",
        dest_name="25L",
        client=EmptyClient(),
    )
    assert route is None


def test_maybe_compute_uses_flow_spec_and_merged_variables():
    route = maybe_compute_taxi_route(
        flow_slug="taxi-v1",
        icao="EDDF",
        variables={"stand": "A12", "runway": "25L", "taxi_route": "Alpha, Bravo"},
        client=FakeRouteClient(),
    )
    assert route == "Lima 7, November"


def test_maybe_compute_skips_unknown_flow():
    assert (
        maybe_compute_taxi_route(
            flow_slug="clearance-v1",
            icao="EDDF",
            variables={"stand": "A12", "runway": "25L"},
            client=FakeRouteClient(),
        )
        is None
    )


def test_maybe_compute_skips_without_icao():
    assert (
        maybe_compute_taxi_route(
            flow_slug="taxi-v1",
            icao=None,
            variables={"stand": "A12", "runway": "25L"},
            client=FakeRouteClient(),
        )
        is None
    )

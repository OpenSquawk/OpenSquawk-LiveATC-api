"""Tests for best-effort taxi route computation at session creation."""

from app.taxi_route_service import (
    maybe_compute_taxi_route,
    phoneticize_route,
    resolve_taxi_clearance,
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
    GRAPH = GRAPH_OSM

    def fetch_json(self, query: str):
        # Graph query (radius or airport-area) — recurses child nodes via (._;>;).
        if "(._;>;)" in query:
            return self.GRAPH
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
        raise AssertionError(f"unexpected query: {query}")


class EmptyClient:
    def fetch_json(self, query: str):
        return {"elements": []}


# Same taxiway path as GRAPH_OSM, plus a north-south runway "18/36" across
# lon 8.0015 that crosses the 2->3 taxiway segment. The crossing (lat 50.0) is
# nearer the south (36) threshold at 49.9995 than the north (18) at 50.003.
GRAPH_OSM_WITH_CROSSING = {
    "elements": GRAPH_OSM["elements"]
    + [
        {"type": "node", "id": 701, "lat": 49.9995, "lon": 8.0015},
        {"type": "node", "id": 702, "lat": 50.003, "lon": 8.0015},
        {"type": "way", "id": 700, "nodes": [701, 702], "tags": {"aeroway": "runway", "ref": "18/36"}},
    ]
}


class CrossingRouteClient(FakeRouteClient):
    GRAPH = GRAPH_OSM_WITH_CROSSING


def test_resolve_detects_runway_crossing_and_builds_hold_clause():
    clearance = resolve_taxi_clearance(
        icao="EDDF",
        origin_name="A12",
        dest_name="25L",
        client=CrossingRouteClient(),
    )
    assert clearance["taxi_route"] == "Lima 7, November"
    assert clearance["crossing_runways"] == ["36"]
    assert clearance["taxi_hold_clause"] == ", hold short runway 36"


def test_resolve_returns_clearance_variables():
    clearance = resolve_taxi_clearance(
        icao="EDDF",
        origin_name="A12",
        dest_name="25L",
        client=FakeRouteClient(),
    )
    assert clearance == {
        "taxi_route": "Lima 7, November",
        "crossing_runways": [],
        "taxi_hold_clause": "",
    }


def test_resolve_returns_none_when_feature_not_found():
    # Empty airport features -> origin/dest cannot be resolved -> fallback.
    clearance = resolve_taxi_clearance(
        icao="EDDF",
        origin_name="A12",
        dest_name="25L",
        client=EmptyClient(),
    )
    assert clearance is None


def test_maybe_compute_uses_flow_spec_and_merged_variables():
    clearance = maybe_compute_taxi_route(
        flow_slug="taxi-v1",
        icao="EDDF",
        variables={"stand": "A12", "runway": "25L", "taxi_route": "Alpha, Bravo"},
        client=FakeRouteClient(),
    )
    assert clearance["taxi_route"] == "Lima 7, November"
    assert clearance["crossing_runways"] == []


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

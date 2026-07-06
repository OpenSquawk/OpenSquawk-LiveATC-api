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


# ── Chain resolution: which taxi flow a session will reach ───────────────────

def _load_flow(slug):
    from app.config import FLOWS_DIR
    from app.flow_loader import get_flow, load_all_flows

    load_all_flows(FLOWS_DIR)
    return get_flow(slug)


def test_taxi_flow_in_chain_finds_taxi_v1_from_clearance():
    from app.routes.session_routes import _taxi_flow_in_chain

    # clearance-v1 -> taxi-v1 -> tower-v1: the taxi flow is taxi-v1.
    assert _taxi_flow_in_chain(_load_flow("clearance-v1")) == "taxi-v1"


def test_taxi_flow_in_chain_none_for_non_taxi_chain():
    from app.routes.session_routes import _taxi_flow_in_chain

    # tower-v1 does not chain into a taxi flow.
    assert _taxi_flow_in_chain(_load_flow("tower-v1")) is None


# ── Real-stand selection: random OSM stand / bridge reverse-geocode ──────────

def test_unresolvable_stand_falls_back_to_random_real_stand():
    # "Z99" is not an OSM stand; the fixture airport has exactly one stand (A12),
    # so the "random" pick is deterministic. The spoken stand must be updated to
    # match the routed one.
    clearance = maybe_compute_taxi_route(
        flow_slug="taxi-v1",
        icao="EDDF",
        variables={"stand": "Z99", "runway": "25L"},
        client=FakeRouteClient(),
    )
    assert clearance is not None
    assert clearance["stand"] == "A12"
    assert clearance["taxi_route"] == "Lima 7, November"


def test_bridge_position_routes_from_coordinates_and_names_nearest_stand():
    # Aircraft parked at the A12 stand node -> route starts from raw coords,
    # nearest stand within snap range names the spoken stand.
    clearance = maybe_compute_taxi_route(
        flow_slug="taxi-v1",
        icao="EDDF",
        variables={"stand": "Z99", "runway": "25L"},
        aircraft_lat=50.0,
        aircraft_lon=8.0,
        client=FakeRouteClient(),
    )
    assert clearance is not None
    assert clearance["stand"] == "A12"
    assert clearance["taxi_route"] == "Lima 7, November"


def test_null_island_bridge_position_is_ignored():
    # 0/0 is the bridge's "no data" default — must not be used as an origin.
    clearance = maybe_compute_taxi_route(
        flow_slug="taxi-v1",
        icao="EDDF",
        variables={"stand": "A12", "runway": "25L"},
        aircraft_lat=0.0,
        aircraft_lon=0.0,
        client=FakeRouteClient(),
    )
    assert clearance is not None
    # Named-stand path used; no stand override needed since A12 resolves.
    assert "stand" not in clearance
    assert clearance["taxi_route"] == "Lima 7, November"


def test_arrival_taxi_in_picks_random_real_stand_for_unresolvable_dest():
    # taxi-in-v1: dest is the parking stand; YAML default "the apron" is not an
    # OSM feature -> random real stand, and parking_stand is updated.
    clearance = maybe_compute_taxi_route(
        flow_slug="taxi-in-v1",
        icao="EDDF",
        variables={"runway": "25L", "parking_stand": "the apron"},
        # Arrival: aircraft position must be ignored (it's not at the stand).
        aircraft_lat=50.0,
        aircraft_lon=8.0,
        client=FakeRouteClient(),
    )
    assert clearance is not None
    assert clearance["parking_stand"] == "A12"

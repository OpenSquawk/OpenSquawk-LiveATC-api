from app.airport_geocode import (
    GeocodeMatch,
    GeocodeQuery,
    apply_runway_point,
    create_feature,
    match_feature_by_coordinate,
    match_feature_by_name,
    normalize_with_prefixes,
)


class FakeOverpassClient:
    def fetch_json(self, query: str):
        assert "way(200)" in query
        return {
            "elements": [
                {"type": "way", "id": 200, "nodes": [10, 11]},
                {"type": "node", "id": 10, "lat": 50.0, "lon": 8.0},
                {"type": "node", "id": 11, "lat": 50.1, "lon": 8.1},
            ]
        }


def test_normalize_with_prefixes_adds_practical_variants():
    assert normalize_with_prefixes("RWY 25C") == {"RWY25C", "25C"}
    assert normalize_with_prefixes("Gate A5") == {"GATEA5", "A5"}
    assert normalize_with_prefixes("Stand V155") == {"STANDV155", "V155"}


def test_runway_aliases_are_split_and_preferred_by_runway_bias():
    runway = create_feature(
        {
            "type": "way",
            "id": 200,
            "center": {"lat": 50.05, "lon": 8.05},
            "nodes": [10, 11],
            "tags": {"aeroway": "runway", "ref": "07L/25R"},
        }
    )
    stand = create_feature(
        {
            "type": "node",
            "id": 300,
            "lat": 50.06,
            "lon": 8.06,
            "tags": {"aeroway": "parking_position", "ref": "25R"},
        }
    )

    assert runway is not None
    assert stand is not None
    assert "07L" in runway.aliases
    assert "25R" in runway.aliases

    match = match_feature_by_name([stand, runway], "RWY 25R")

    assert match is not None
    assert match.feature.type == "runway"
    assert match.matched_alias == "25R"


def test_coordinate_match_returns_closest_feature_with_distance():
    gate = create_feature(
        {
            "type": "node",
            "id": 1,
            "lat": 50.0,
            "lon": 8.0,
            "tags": {"aeroway": "gate", "ref": "A1"},
        }
    )
    stand = create_feature(
        {
            "type": "node",
            "id": 2,
            "lat": 51.0,
            "lon": 9.0,
            "tags": {"aeroway": "parking_position", "ref": "B1"},
        }
    )

    assert gate is not None
    assert stand is not None

    match = match_feature_by_coordinate([gate, stand], 50.0001, 8.0001)

    assert match is not None
    assert match.feature.osm_id == 1
    assert match.distance_meters is not None
    assert match.distance_meters < 20


def test_apply_runway_point_moves_named_runway_way_to_requested_endpoint():
    runway = create_feature(
        {
            "type": "way",
            "id": 200,
            "center": {"lat": 50.05, "lon": 8.05},
            "nodes": [10, 11],
            "tags": {"aeroway": "runway", "ref": "07L/25R"},
        }
    )

    assert runway is not None

    match = GeocodeMatch(feature=runway, matched_alias="07L", source="name")
    start = apply_runway_point(match, "start", client=FakeOverpassClient())
    end = apply_runway_point(match, "end", client=FakeOverpassClient())
    center = apply_runway_point(match, "center", client=FakeOverpassClient())

    assert (start.feature.lat, start.feature.lon) == (50.0, 8.0)
    assert (end.feature.lat, end.feature.lon) == (50.1, 8.1)
    assert (center.feature.lat, center.feature.lon) == (50.05, 8.05)


def test_resolve_query_dataclass_accepts_runway_point_default():
    query = GeocodeQuery(name="RWY 25R")

    assert query.runway_point == "start"

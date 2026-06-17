"""Tests for the airport reference data layer (names + frequencies)."""

import pytest

from app.airport_data import (
    invent_frequency,
    is_icao_airport,
    resolve_airport,
    resolve_for_session,
    spoken_place_aliases,
)


class TestResolveAirport:
    def test_known_airport_english_and_german(self):
        info = resolve_airport("EDDM")
        assert info is not None
        assert info.city_en == "Munich"
        assert info.city_de == "München"

    def test_real_frequency_used_when_published(self):
        info = resolve_airport("EDDM")
        # EDDM publishes a real tower frequency; it must not be invented.
        assert info.invented["tower"] is False
        assert info.frequencies["tower"].startswith("118.")

    def test_missing_position_is_invented_in_band(self):
        info = resolve_airport("EDDM")
        # EDDM publishes no DEP frequency → invented, but still a valid airband value.
        assert info.invented["departure"] is True
        mhz = float(info.frequencies["departure"])
        assert 118.0 <= mhz <= 137.0

    def test_unknown_code_returns_none(self):
        assert resolve_airport("ZZZZ") is None

    def test_german_name_absent_is_none_not_error(self):
        # An airport not in the curated overlay resolves with city_de=None.
        info = resolve_airport("KJFK")
        assert info is not None
        assert info.city_de is None


class TestInventFrequency:
    def test_deterministic(self):
        assert invent_frequency("EDDM", "departure") == invent_frequency("EDDM", "departure")

    def test_avoids_guard_frequency(self):
        # Never returns the emergency guard 121.500
        for icao in ("EDDM", "EDDF", "KJFK", "LSZH"):
            for pos in ("ground", "tower", "departure", "director", "center"):
                assert invent_frequency(icao, pos) != "121.500"


class TestAliases:
    def test_icao_expands_to_both_languages(self):
        aliases = [a.lower() for a in spoken_place_aliases("EDDM")]
        assert "munich" in aliases
        assert "münchen" in aliases

    def test_english_city_expands_to_german(self):
        aliases = [a.lower() for a in spoken_place_aliases("Munich")]
        assert "münchen" in aliases

    def test_is_icao_airport(self):
        assert is_icao_airport("EDDM") is True
        assert is_icao_airport("DLH39A") is False
        assert is_icao_airport("ZZZZ") is False


class TestResolveForSession:
    def test_station_supplies_frequencies(self):
        res = resolve_for_session("EDDM", None)
        assert res.variables["ground_freq"].startswith("121.")
        assert "tower_freq" in res.variables
        # Every logical position becomes a *_freq variable (real or invented).
        assert "director_freq" in res.variables
        assert "departure_freq" in res.variables

    def test_destination_supplies_spoken_city(self):
        res = resolve_for_session(None, "EDDM")
        assert res.variables["destination"] == "Munich"

    def test_unknown_codes_yield_no_overrides(self):
        res = resolve_for_session("ZZZZ", "ZZZZ")
        assert res.variables == {}
        assert res.station is None and res.destination is None


class TestSessionRouteResolution:
    @pytest.fixture(autouse=True)
    def _load(self):
        from pathlib import Path
        from app import session_store
        from app.flow_loader import load_all_flows
        load_all_flows(Path(__file__).parent.parent / "flows")
        session_store._sessions.clear()

    def test_create_session_resolves_names_and_freqs(self):
        from app.models import CreateSessionRequest
        from app.routes.session_routes import create_radio_session
        resp = create_radio_session(CreateSessionRequest(
            flow_slug="clearance-v1", airport_icao="EDDM",
            destination_icao="EDDM", no_chain=True,
        ))
        assert resp.variables["ground_freq"].startswith("121.")
        assert resp.variables["destination"] == "Munich"
        assert resp.station_airport is not None
        assert resp.station_airport.city_de == "München"
        # Departure has no published EDDM freq → reported as invented.
        assert "departure" in resp.station_airport.invented_positions

    def test_explicit_variables_override_resolution(self):
        from app.models import CreateSessionRequest
        from app.routes.session_routes import create_radio_session
        resp = create_radio_session(CreateSessionRequest(
            flow_slug="clearance-v1", destination_icao="EDDM",
            variables={"destination": "Testfield"}, no_chain=True,
        ))
        assert resp.variables["destination"] == "Testfield"

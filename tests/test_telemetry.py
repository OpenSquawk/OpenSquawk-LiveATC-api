"""Tests for the telemetry-driven decision path (process_telemetry).

A synthetic two-state flow is registered directly in the flow cache:

  WAIT_AIRBORNE (pilot) ──telemetry on_ground == False──► ATC_CONTACT_DEPARTURE (atc, end)

This exercises only the telemetry machinery, independent of any YAML flow, so
the behaviour is pinned regardless of how the shipped flows evolve.
"""

import time

import pytest

from app import flow_loader, session_store
from app.decision_engine import _eval_telemetry, process_telemetry, process_timeout
from app.models import (
    DecisionFlow,
    DecisionState,
    TelemetryCondition,
    Transition,
)
from app.session_store import create_session, get_session


def _build_flow(*, for_ms: int = 0, once: bool = True) -> DecisionFlow:
    flow = DecisionFlow(
        slug="telem-test",
        schema_version="1.0",
        name="Telemetry test flow",
        start_state="WAIT_AIRBORNE",
        end_states=["ATC_CONTACT_DEPARTURE"],
        variables={},
        flags={},
        states={
            "WAIT_AIRBORNE": DecisionState(
                role="pilot",
                name="Waiting to get airborne",
                # Non-telemetry sibling so the flow still works without a bridge.
                ok_next=[Transition(to="ATC_CONTACT_DEPARTURE", trigger="airborne|passing")],
                auto_transitions=[
                    Transition(
                        to="ATC_CONTACT_DEPARTURE",
                        telemetry=TelemetryCondition(
                            parameter="on_ground", operator="eq", value=False,
                            for_ms=for_ms, once=once,
                        ),
                        label="Airborne — contact Departure",
                    ),
                ],
            ),
            "ATC_CONTACT_DEPARTURE": DecisionState(
                role="atc",
                name="Contact departure",
                say_template="{{callsign}}, contact Departure, good day",
            ),
        },
    )
    return flow


@pytest.fixture
def telem_session():
    flow = _build_flow()
    flow_loader._flow_cache[flow.slug] = flow
    session_store._sessions.clear()
    session = create_session(flow, variable_overrides={"callsign": "DLH39A"})
    yield session
    flow_loader._flow_cache.pop(flow.slug, None)


class TestEvalTelemetry:
    def test_missing_parameter_is_false(self):
        cond = TelemetryCondition(parameter="altitude_ft", operator="gt", value=1000)
        assert _eval_telemetry(cond, {}) is False

    def test_numeric_comparisons(self):
        cond = TelemetryCondition(parameter="altitude_ft", operator="gt", value=1000)
        assert _eval_telemetry(cond, {"altitude_ft": 1500}) is True
        assert _eval_telemetry(cond, {"altitude_ft": 800}) is False

    def test_boolean_coercion(self):
        cond = TelemetryCondition(parameter="on_ground", operator="eq", value=False)
        assert _eval_telemetry(cond, {"on_ground": False}) is True
        assert _eval_telemetry(cond, {"on_ground": 0}) is True   # 0 → False
        assert _eval_telemetry(cond, {"on_ground": True}) is False
        assert _eval_telemetry(cond, {"on_ground": 1}) is False  # 1 → True


class TestProcessTelemetry:
    def test_idle_tick_does_not_advance(self, telem_session):
        resp = process_telemetry(telem_session.session_id, {"on_ground": True, "altitude_ft": 0})
        assert resp.telemetry_fired is False
        assert resp.next_state_id == "WAIT_AIRBORNE"
        assert resp.controller_say_rendered is None

    def test_threshold_met_fires_transition(self, telem_session):
        resp = process_telemetry(telem_session.session_id, {"on_ground": False, "altitude_ft": 500})
        assert resp.telemetry_fired is True
        assert resp.next_state_id == "ATC_CONTACT_DEPARTURE"
        assert "Departure" in (resp.controller_say_rendered or "")
        # Callsign was rendered raw (phonetic expansion happens on the frontend).
        assert "DLH39A" in (resp.controller_say_rendered or "")
        assert resp.session_complete is True

    def test_once_guard_prevents_refire(self, telem_session):
        process_telemetry(telem_session.session_id, {"on_ground": False})
        # Manually rewind to the pilot state to prove the once-guard, not the
        # end-state, is what stops a refire.
        session = get_session(telem_session.session_id)
        session.current_state = "WAIT_AIRBORNE"
        session_store.save_session(session)
        resp = process_telemetry(telem_session.session_id, {"on_ground": False})
        assert resp.telemetry_fired is False
        assert resp.next_state_id == "WAIT_AIRBORNE"

    def test_sparse_updates_merge(self, telem_session):
        # First tick reports only altitude; on_ground stays unknown → no fire.
        r1 = process_telemetry(telem_session.session_id, {"altitude_ft": 500})
        assert r1.telemetry_fired is False
        # Second tick reports on_ground; altitude is remembered.
        r2 = process_telemetry(telem_session.session_id, {"on_ground": False})
        assert r2.telemetry_fired is True
        session = get_session(telem_session.session_id)
        assert session.telemetry["altitude_ft"] == 500


class TestHysteresis:
    def test_for_ms_requires_sustained_condition(self):
        flow = _build_flow(for_ms=5000)
        flow_loader._flow_cache[flow.slug] = flow
        session_store._sessions.clear()
        session = create_session(flow, variable_overrides={"callsign": "DLH39A"})
        try:
            # First true tick only arms the timer (keys are state::<edge index>::target).
            r1 = process_telemetry(session.session_id, {"on_ground": False})
            assert r1.telemetry_fired is False
            s = get_session(session.session_id)
            assert "WAIT_AIRBORNE::0::ATC_CONTACT_DEPARTURE" in s.telemetry_pending
            # A false tick disarms it.
            process_telemetry(session.session_id, {"on_ground": True})
            s = get_session(session.session_id)
            assert "WAIT_AIRBORNE::0::ATC_CONTACT_DEPARTURE" not in s.telemetry_pending
        finally:
            flow_loader._flow_cache.pop(flow.slug, None)


class TestTelemetryIsolation:
    """Telemetry edges must never fire via silence-timeout."""

    def test_timeout_ignores_telemetry_edge(self, telem_session):
        # WAIT_AIRBORNE has no auto_advance_on_silence and its only auto_transition
        # is telemetry-gated, so a silence timeout must NOT advance on it.
        with pytest.raises(ValueError):
            process_timeout(telem_session.session_id)
        session = get_session(telem_session.session_id)
        assert session.current_state == "WAIT_AIRBORNE"


# ---------------------------------------------------------------------------
# Integration: the real tower-v1 airborne handoff (PoC flow)
# ---------------------------------------------------------------------------

FLOWS_DIR = __import__("pathlib").Path(__file__).parent.parent / "flows"


class TestTowerAirborneHandoff:
    """tower-v1: PILOT_AWAIT_AIRBORNE must exit via telemetry, phrase, or silence."""

    @pytest.fixture(autouse=True)
    def load_flows(self):
        from app.flow_loader import load_all_flows
        load_all_flows(FLOWS_DIR)
        session_store._sessions.clear()

    @pytest.fixture
    def rolling_session(self):
        """A tower-v1 session advanced to PILOT_AWAIT_AIRBORNE (takeoff roll)."""
        from app.decision_engine import process_transmission
        from app.flow_loader import get_flow
        from app.models import DecisionRequest

        flow = get_flow("tower")
        session = create_session(flow, no_chain=True)
        for utterance in (
            "DLH39A holding short runway 25L ready for departure",
            "line up and wait runway 25L DLH39A",
            "cleared for takeoff runway 25L DLH39A",
        ):
            resp = process_transmission(session.session_id, DecisionRequest(pilot_utterance=utterance))
        assert resp.next_state_id == "PILOT_AWAIT_AIRBORNE"
        return session

    def _age_pending(self, session_id: str, ms: float) -> None:
        """Backdate the hysteresis arm time so for_ms elapses without sleeping."""
        session = get_session(session_id)
        for key in list(session.telemetry_pending):
            session.telemetry_pending[key] -= ms
        session_store.save_session(session)

    def test_on_ground_tick_does_not_fire(self, rolling_session):
        resp = process_telemetry(rolling_session.session_id, {"on_ground": True, "gs_kts": 120})
        assert resp.telemetry_fired is False
        assert resp.next_state_id == "PILOT_AWAIT_AIRBORNE"

    def test_airborne_fires_after_hysteresis(self, rolling_session):
        # First airborne tick arms the 2s debounce, does not fire.
        r1 = process_telemetry(rolling_session.session_id, {"on_ground": False})
        assert r1.telemetry_fired is False
        # Backdate the arm time past for_ms; the next tick fires the handoff.
        self._age_pending(rolling_session.session_id, 3000)
        r2 = process_telemetry(rolling_session.session_id, {"on_ground": False})
        assert r2.telemetry_fired is True
        assert r2.next_state_id == "PILOT_HANDOFF_READBACK"
        assert "contact departure" in (r2.controller_say_rendered or "").lower()

    def test_pilot_phrase_fallback(self, rolling_session):
        from app.decision_engine import process_transmission
        from app.models import DecisionRequest
        resp = process_transmission(
            rolling_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A airborne"),
        )
        assert resp.next_state_id == "PILOT_HANDOFF_READBACK"
        assert "contact departure" in (resp.controller_say_rendered or "").lower()

    def test_silence_timeout_fallback(self, rolling_session):
        resp = process_timeout(rolling_session.session_id)
        assert resp.next_state_id == "PILOT_HANDOFF_READBACK"
        assert "contact departure" in (resp.controller_say_rendered or "").lower()

    def test_other_call_gets_roger_and_keeps_waiting(self, rolling_session):
        from app.decision_engine import process_transmission
        from app.models import DecisionRequest
        resp = process_transmission(
            rolling_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A rolling"),
        )
        # bad_next → roger → auto back to the wait state.
        assert resp.next_state_id == "PILOT_AWAIT_AIRBORNE"
        assert "roger" in (resp.controller_say_rendered or "").lower()


# ---------------------------------------------------------------------------
# Variable-resolved condition values ("{{climb_altitude}}", FL parsing, offset)
# ---------------------------------------------------------------------------

class TestValueResolution:
    def test_parse_level(self):
        from app.decision_engine import _parse_level
        assert _parse_level("FL150") == 15000
        assert _parse_level("fl 80") == 8000
        assert _parse_level("5000") == 5000
        assert _parse_level(5000) == 5000.0
        assert _parse_level("5,000") == 5000
        assert _parse_level("garbage") is None
        assert _parse_level(True) is None

    def test_template_value_resolves_against_variables(self):
        cond = TelemetryCondition(parameter="altitude_ft", operator="gte", value="{{climb_altitude}}")
        variables = {"climb_altitude": "FL150"}
        assert _eval_telemetry(cond, {"altitude_ft": 15200}, variables) is True
        assert _eval_telemetry(cond, {"altitude_ft": 14000}, variables) is False

    def test_offset_shifts_the_threshold(self):
        cond = TelemetryCondition(
            parameter="altitude_ft", operator="gte", value="{{climb_altitude}}", offset=-1000,
        )
        variables = {"climb_altitude": "5000"}
        assert _eval_telemetry(cond, {"altitude_ft": 4200}, variables) is True
        assert _eval_telemetry(cond, {"altitude_ft": 3900}, variables) is False

    def test_unresolvable_template_never_fires(self):
        cond = TelemetryCondition(parameter="altitude_ft", operator="gte", value="{{nope}}")
        assert _eval_telemetry(cond, {"altitude_ft": 99999}, {}) is False

    def test_static_string_level_parses(self):
        cond = TelemetryCondition(parameter="altitude_ft", operator="lte", value="FL80", offset=1500)
        assert _eval_telemetry(cond, {"altitude_ft": 9400}, {}) is True
        assert _eval_telemetry(cond, {"altitude_ft": 9600}, {}) is False


# ---------------------------------------------------------------------------
# Distance derivation from lat/lon ticks
# ---------------------------------------------------------------------------

class TestDerivedDistances:
    def test_distances_derived_from_position(self, telem_session):
        session = get_session(telem_session.session_id)
        session.airport_icao = "EDDM"
        session.destination_icao = "EDDF"
        session_store.save_session(session)
        # Exactly one degree of latitude north of EDDF (50.02671, 8.55835) = 60 nm.
        process_telemetry(telem_session.session_id, {"lat": 51.02671, "lon": 8.55835})
        session = get_session(telem_session.session_id)
        assert session.telemetry["distance_to_dest_nm"] == pytest.approx(60.0, abs=0.5)
        assert session.telemetry["distance_to_dep_nm"] > 100  # far from Munich

    def test_null_island_position_ignored(self, telem_session):
        session = get_session(telem_session.session_id)
        session.destination_icao = "EDDF"
        session_store.save_session(session)
        process_telemetry(telem_session.session_id, {"lat": 0.0, "lon": 0.0})
        session = get_session(telem_session.session_id)
        assert "distance_to_dest_nm" not in session.telemetry

    def test_unknown_airport_keeps_distances_absent(self, telem_session):
        session = get_session(telem_session.session_id)
        session.airport_icao = "QQQQ"  # not in the dataset (XXXX actually is!)
        session.destination_icao = None
        session_store.save_session(session)
        process_telemetry(telem_session.session_id, {"lat": 50.0, "lon": 8.0})
        session = get_session(telem_session.session_id)
        assert "distance_to_dep_nm" not in session.telemetry
        assert "distance_to_dest_nm" not in session.telemetry


# ---------------------------------------------------------------------------
# Integration: departure-v1 climb — approach the level, bust callout, handoff
# ---------------------------------------------------------------------------

class TestDepartureClimbHandoff:
    @pytest.fixture(autouse=True)
    def load_flows(self):
        from app.flow_loader import load_all_flows
        load_all_flows(FLOWS_DIR)
        session_store._sessions.clear()

    @pytest.fixture
    def climbing_session(self):
        """A departure-v1 session advanced to PILOT_AWAIT_CLIMB."""
        from app.decision_engine import process_transmission
        from app.flow_loader import get_flow
        from app.models import DecisionRequest

        session = create_session(get_flow("departure-v1"), no_chain=True)
        for utterance in (
            "DLH39A, passing 1500, climbing 5000",
            "Climb FL150, direct BIBAX, DLH39A",
        ):
            resp = process_transmission(session.session_id, DecisionRequest(pilot_utterance=utterance))
        assert resp.next_state_id == "PILOT_AWAIT_CLIMB"
        return session

    def _age_pending(self, session_id: str, ms: float) -> None:
        session = get_session(session_id)
        for key in list(session.telemetry_pending):
            session.telemetry_pending[key] -= ms
        session_store.save_session(session)

    def test_approaching_cleared_level_hands_off_to_center(self, climbing_session):
        # climb_altitude FL150 → threshold 14000 ft (offset -1000), held 3 s.
        r1 = process_telemetry(climbing_session.session_id, {"altitude_ft": 14200})
        assert r1.telemetry_fired is False  # arming
        self._age_pending(climbing_session.session_id, 4000)
        r2 = process_telemetry(climbing_session.session_id, {"altitude_ft": 14300})
        assert r2.telemetry_fired is True
        assert r2.next_state_id == "PILOT_CENTER_FREQ_READBACK"
        assert "contact center" in (r2.controller_say_rendered or "").lower()

    def test_low_altitude_does_not_fire(self, climbing_session):
        r = process_telemetry(climbing_session.session_id, {"altitude_ft": 8000})
        assert r.telemetry_fired is False
        assert r.next_state_id == "PILOT_AWAIT_CLIMB"

    def test_level_bust_calls_out_once_and_returns_to_wait(self, climbing_session):
        sid = climbing_session.session_id
        # 15400 ft > FL150 + 300. Note this also satisfies the handoff edge, but
        # the bust callout requires 8 s vs the handoff's 3 s — age past the
        # handoff first and mark it fired so only the bust edge remains.
        # In a real climb the handoff fires first (lower threshold reached
        # earlier); to test the bust edge in isolation, pre-mark the handoff.
        session = get_session(sid)
        session.fired_telemetry.append("PILOT_AWAIT_CLIMB::0::ATC_CENTER_HANDOFF")
        session_store.save_session(session)
        r1 = process_telemetry(sid, {"altitude_ft": 15400})
        assert r1.telemetry_fired is False  # arming the bust edge
        self._age_pending(sid, 9000)
        r2 = process_telemetry(sid, {"altitude_ft": 15400})
        assert r2.telemetry_fired is True
        assert r2.next_state_id == "PILOT_AWAIT_CLIMB"  # callout, then back to waiting
        assert "check altitude" in (r2.controller_say_rendered or "").lower()

    def test_phrase_fallback_still_works(self, climbing_session):
        from app.decision_engine import process_transmission
        from app.models import DecisionRequest
        resp = process_transmission(
            climbing_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A reaching flight level 150"),
        )
        assert resp.next_state_id == "PILOT_CENTER_FREQ_READBACK"
        assert "contact center" in (resp.controller_say_rendered or "").lower()

    def test_silence_fallback_still_works(self, climbing_session):
        resp = process_timeout(climbing_session.session_id)
        assert resp.next_state_id == "PILOT_CENTER_FREQ_READBACK"


# ---------------------------------------------------------------------------
# Integration: ifr-tower-landing-v1 — vacated detected from groundspeed
# ---------------------------------------------------------------------------

class TestVacatedByGroundspeed:
    @pytest.fixture(autouse=True)
    def load_flows(self):
        from app.flow_loader import load_all_flows
        load_all_flows(FLOWS_DIR)
        session_store._sessions.clear()

    @pytest.fixture
    def landed_session(self):
        """An ifr-tower-landing session advanced to PILOT_RUNWAY_VACATED."""
        from app.decision_engine import process_transmission
        from app.flow_loader import get_flow
        from app.models import DecisionRequest

        session = create_session(get_flow("ifr-tower-landing-v1"), no_chain=True)
        for utterance in (
            "DLH6RK, established ILS runway 26L",
            "cleared to land runway 26L, DLH6RK",
        ):
            resp = process_transmission(session.session_id, DecisionRequest(pilot_utterance=utterance))
        assert resp.next_state_id == "PILOT_RUNWAY_VACATED"
        return session

    def _age_pending(self, session_id: str, ms: float) -> None:
        session = get_session(session_id)
        for key in list(session.telemetry_pending):
            session.telemetry_pending[key] -= ms
        session_store.save_session(session)

    def test_taxi_speed_triggers_the_handoff(self, landed_session):
        r1 = process_telemetry(landed_session.session_id, {"gs_kts": 18})
        assert r1.telemetry_fired is False  # arming (8 s hold)
        self._age_pending(landed_session.session_id, 9000)
        r2 = process_telemetry(landed_session.session_id, {"gs_kts": 15})
        assert r2.telemetry_fired is True
        assert r2.next_state_id == "PILOT_GROUND_FREQ_READBACK"
        assert "contact ground" in (r2.controller_say_rendered or "").lower()

    def test_approach_speed_does_not_fire(self, landed_session):
        r = process_telemetry(landed_session.session_id, {"gs_kts": 135})
        assert r.telemetry_fired is False
        assert r.next_state_id == "PILOT_RUNWAY_VACATED"

"""Integration tests for the full decision engine.

The clearance flow (clearance-v1.yaml) models ICAO IFR clearance delivery:
  INITIAL_CALL → ATC_ISSUES_CLEARANCE (auto) → PILOT_READBACK
               → ATC_READBACK_CORRECT (auto) → CLEARANCE_COMPLETE
               ↓ next_flow chain
  REQUEST_STARTUP (taxi-v1)

Trigger for INITIAL_CALL ok_next: "information|request.*clear|IFR|clearance|stand"
Readback required at PILOT_READBACK: squawk (2341) and initial_altitude (5000).

Note: clearance-v1 has next_flow=taxi-v1, so completing the clearance automatically
chains to taxi-v1 at REQUEST_STARTUP.  The "end state raises ValueError" behaviour
is tested via a flow without next_flow (using the taxi-v1 session separately).
"""

import pytest

from app import session_store
from app.decision_engine import process_timeout, process_transmission
from app.flow_loader import load_all_flows
from app.models import DecisionRequest, LoopDetectedError
from app.session_store import create_session, get_session

FLOWS_DIR = __import__("pathlib").Path(__file__).parent.parent / "flows"

# Utterances that match the actual YAML triggers
GOOD_INITIAL_CALL = "request clearance"          # matches "clearance" in trigger
GOOD_READBACK = "cleared Munich BIBAX1N departure climb 5000 squawk 2341 DLH39A"
BAD_READBACK = "acknowledged"                    # missing squawk and initial_altitude


@pytest.fixture(autouse=True)
def load_flows():
    load_all_flows(FLOWS_DIR)
    session_store._sessions.clear()


@pytest.fixture
def clearance_session():
    from app.flow_loader import get_flow
    flow = get_flow("clearance")
    return create_session(flow)


class TestDecisionEngine:
    def test_correct_request_advances_to_pilot_readback(self, clearance_session):
        req = DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL)
        resp = process_transmission(clearance_session.session_id, req)
        # Auto-advances past ATC_ISSUES_CLEARANCE → PILOT_READBACK
        assert resp.next_state_id == "PILOT_READBACK"
        assert resp.fallback_used is False

    def test_wrong_utterance_returns_to_initial_call(self, clearance_session):
        req = DecisionRequest(pilot_utterance="blah blah nonsense")
        resp = process_transmission(clearance_session.session_id, req)
        assert resp.next_state_id == "INITIAL_CALL"

    def test_trace_contains_state_enter(self, clearance_session):
        req = DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL)
        resp = process_transmission(clearance_session.session_id, req)
        assert "state_enter" in [t.type for t in resp.trace]

    def test_trace_contains_regex_match(self, clearance_session):
        req = DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL)
        resp = process_transmission(clearance_session.session_id, req)
        assert "regex_match" in [t.type for t in resp.trace]

    def test_correct_readback_chains_to_taxi_flow(self, clearance_session):
        """Completing the clearance readback chains to taxi-v1 via next_flow."""
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_READBACK),
        )
        # next_flow=taxi-v1 → engine chains past CLEARANCE_COMPLETE to REQUEST_STARTUP
        assert resp.next_state_id == "REQUEST_STARTUP"
        assert resp.active_flow == "taxi-v1"
        assert resp.fallback_used is False
        # ATC speech is still from clearance (the last controller message)
        assert resp.controller_say_rendered is not None
        assert "ground" in (resp.controller_say_rendered or "").lower()
        # Expected pilot template is now the taxi startup request
        assert resp.expected_pilot_template is not None
        assert "startup" in (resp.expected_pilot_template or "").lower()

    def test_incorrect_readback_loops_back(self, clearance_session):
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=BAD_READBACK),
        )
        assert resp.next_state_id == "PILOT_READBACK"
        assert resp.fallback_used is True
        assert "readback_missing" in (resp.fallback_reason or "")

    def test_incorrect_readback_returns_atc_correction(self, clearance_session):
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=BAD_READBACK),
        )
        # ATC_READBACK_INCORRECT say_template must be captured and rendered
        assert resp.controller_say_template is not None
        assert resp.controller_say_rendered is not None
        # Rendered output must contain the correct squawk and altitude values
        assert "2341" in (resp.controller_say_rendered or "")
        assert "5000" in (resp.controller_say_rendered or "")

    def test_session_persisted_after_transmission(self, clearance_session):
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        session = get_session(clearance_session.session_id)
        assert session is not None
        assert len(session.decision_history) == 1

    def test_unknown_session_raises_key_error(self):
        with pytest.raises(KeyError):
            process_transmission("nonexistent-session-id", DecisionRequest(pilot_utterance="test"))

    def test_auto_advanced_states_populated(self, clearance_session):
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        # ATC_ISSUES_CLEARANCE → PILOT_READBACK auto-advance should be recorded
        assert len(resp.auto_advanced_states) > 0

    def test_readback_correct_flag_set_after_full_readback(self, clearance_session):
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_READBACK),
        )
        # set_flag(readback_correct=true) fires on ATC_READBACK_CORRECT → CLEARANCE_COMPLETE
        assert resp.flags.get("readback_correct") is True

    def test_response_has_rendered_template(self, clearance_session):
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        assert hasattr(resp, "controller_say_rendered")

    def test_end_state_raises_value_error(self):
        """Transmitting after a terminal flow (no next_flow) raises ValueError."""
        from app.flow_loader import get_flow
        from app.session_store import save_session
        # departure-v1 has no next_flow — DEPARTURE_COMPLETE is truly terminal.
        dep_flow = get_flow("departure")
        session = create_session(dep_flow)
        session.current_state = "DEPARTURE_COMPLETE"
        save_session(session)
        with pytest.raises(ValueError, match="complete"):
            process_transmission(
                session.session_id,
                DecisionRequest(pilot_utterance="anything"),
            )

    def test_clearance_variables_carry_over_to_taxi(self, clearance_session):
        """Variables set during clearance (callsign, stand) are available in taxi-v1."""
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_READBACK),
        )
        # After chaining to taxi-v1, callsign and stand must still be correct
        assert resp.variables.get("callsign") == "DLH39A"
        assert resp.variables.get("stand") == "A12"
        # Taxi-specific variables are now initialised too
        assert "qnh" in resp.variables
        assert "runway" in resp.variables


class TestGreeting:
    """Optional courtesy-call handshake at initial-contact states."""

    @pytest.fixture
    def taxi_session(self):
        from app.flow_loader import get_flow
        return create_session(get_flow("taxi"))

    def test_greeting_only_stays_and_prompts(self, taxi_session):
        resp = process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="Munich Ground, DLH39A, good day"),
        )
        # Stays at the initial state and replies "pass your message"
        assert resp.next_state_id == "REQUEST_STARTUP"
        assert "pass your message" in (resp.controller_say_rendered or "").lower()
        assert "greeting" in [t.type for t in resp.trace]

    def test_greeting_with_request_advances(self, taxi_session):
        # A greeting that ALSO carries the request must not be intercepted.
        resp = process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="Good day, DLH39A, request startup"),
        )
        assert resp.next_state_id == "PILOT_STARTUP_READBACK"

    def test_greeting_not_intercepted_when_not_allowed(self):
        from app.flow_loader import get_flow
        # PILOT_READBACK in clearance has no allow_greeting → greeting is just a
        # normal (non-matching) utterance, handled by readback/bad_next.
        session = create_session(get_flow("clearance"))
        process_transmission(session.session_id, DecisionRequest(pilot_utterance="request clearance"))
        resp = process_transmission(session.session_id, DecisionRequest(pilot_utterance="hello"))
        assert resp.next_state_id == "PILOT_READBACK"  # looped back, not "pass your message"
        assert "greeting" not in [t.type for t in resp.trace]


class TestCombinedStartupPushback:
    @pytest.fixture
    def taxi_session(self):
        from app.flow_loader import get_flow
        return create_session(get_flow("taxi"))

    def test_combined_request_routes_to_combined_clearance(self, taxi_session):
        resp = process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A, stand A12, request startup and pushback"),
        )
        assert resp.next_state_id == "PILOT_STARTUP_PUSHBACK_READBACK"
        say = (resp.controller_say_rendered or "").lower()
        assert "startup and pushback approved" in say

    def test_combined_readback_reaches_request_taxi(self, taxi_session):
        process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A, request startup and pushback"),
        )
        resp = process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="startup and pushback approved, QNH 1013, face west, DLH39A"),
        )
        assert resp.next_state_id == "REQUEST_TAXI"

    def test_startup_only_still_works(self, taxi_session):
        resp = process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A, stand A12, request startup"),
        )
        assert resp.next_state_id == "PILOT_STARTUP_READBACK"


class TestReadbackSilenceTimeout:
    @pytest.fixture
    def taxi_session(self):
        from app.flow_loader import get_flow
        return create_session(get_flow("taxi"))

    def test_silence_on_readback_reprompts_and_stays(self, taxi_session):
        # Advance to a readback state
        process_transmission(
            taxi_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A, request startup"),
        )
        resp = process_timeout(taxi_session.session_id)
        # Re-requests the readback and stays put
        assert resp.next_state_id == "PILOT_STARTUP_READBACK"
        assert "readback_silence" in [t.type for t in resp.trace]

    def test_silence_on_non_readback_pilot_state_raises(self, taxi_session):
        # REQUEST_STARTUP has no readback and no silence auto-transition
        with pytest.raises(ValueError):
            process_timeout(taxi_session.session_id)


class TestTaxiInParkingStandNoCollision:
    def test_destination_override_does_not_leak_into_parking(self):
        from app.flow_loader import get_flow
        # Simulate a chained arrival where the inbound destination airport (EDDM)
        # is already in the session.  Taxi-in must say the stand, not "EDDM".
        session = create_session(
            get_flow("taxi-in"), variable_overrides={"destination": "EDDM"}
        )
        resp = process_transmission(
            session.session_id,
            DecisionRequest(pilot_utterance="G-ABCD, vacated runway 25L, for the apron"),
        )
        say = (resp.controller_say_rendered or "")
        assert "EDDM" not in say
        assert "the apron" in say


class TestFuzzySidReadback:
    """Word-pronounced SID names mangled by STT still match the readback."""

    def test_tobak2e_matches_tobacco_too_echo(self):
        from app.readback_evaluator import evaluate_readback_simple
        ok, missing, rep = evaluate_readback_simple(
            "Cleared Tobacco too Echo, climb initially 5000, squawk 0000",
            ["sid"], {"sid": "TOBAK2E"},
        )
        assert ok and missing == []
        assert rep[0]["matched_via"].startswith("fuzzy_sid")

    def test_marun7f_matches_maroon_seven_foxtrot(self):
        from app.readback_evaluator import _fuzzy_ident_match
        assert _fuzzy_ident_match("MARUN7F", "maroon seven foxtrot") is not None

    def test_revision_digit_spoken_as_to(self):
        from app.readback_evaluator import _fuzzy_ident_match
        # "two" is routinely transcribed as the homophone "to".
        for u in ("CLEARED TOBACK TO ECHO", "Clear TODAC to ECHO", "Tobac to Echo"):
            assert _fuzzy_ident_match("TOBAK2E", u) is not None, u

    def test_to_inside_word_is_not_a_digit(self):
        from app.readback_evaluator import _fuzzy_ident_match
        # The "to" inside "tobacco" must not count as the revision digit.
        assert _fuzzy_ident_match("TOBAK2E", "tobacco echo") is None

    def test_wrong_digit_or_letter_does_not_match(self):
        from app.readback_evaluator import _fuzzy_ident_match
        # Right stem, wrong revision digit / final letter → no match.
        assert _fuzzy_ident_match("TOBAK2E", "tobacco three echo") is None
        assert _fuzzy_ident_match("TOBAK2E", "tobacco two sierra") is None

    def test_unrelated_word_does_not_match(self):
        from app.readback_evaluator import _fuzzy_ident_match
        assert _fuzzy_ident_match("TOBAK2E", "runway two echo terminal") is None


class TestIcaoDigitVariants:
    """Numeric readbacks spelled with ICAO radio digits (wun, tree, fife, niner)."""

    def test_qnh_with_icao_digit_variants(self):
        from app.readback_evaluator import evaluate_readback_simple
        ok, missing, rep = evaluate_readback_simple(
            "QNH wun zero wun tree, face west, Lufthansa six Romeo Kilo",
            ["qnh", "pushback_direction"],
            {"qnh": "1013", "pushback_direction": "west"},
        )
        assert ok and missing == []
        assert rep[0]["matched_via"] == "digit_phonetic"

    def test_squawk_with_icao_digit_variants(self):
        from app.readback_evaluator import evaluate_readback_simple
        ok, _, _ = evaluate_readback_simple(
            "squawk too tree four wun", ["squawk"], {"squawk": "2341"})
        assert ok

    def test_wrong_digit_still_rejected(self):
        from app.readback_evaluator import evaluate_readback_simple
        ok, missing, _ = evaluate_readback_simple(
            "QNH wun zero wun four", ["qnh"], {"qnh": "1013"})
        assert not ok and missing == ["qnh"]


class TestReadbackReport:
    """Per-field readback diagnostics surfaced on the response."""

    @pytest.fixture
    def clearance_session(self):
        from app.flow_loader import get_flow
        return create_session(get_flow("clearance"))

    def test_report_present_on_readback_state(self, clearance_session):
        process_transmission(clearance_session.session_id, DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL))
        resp = process_transmission(clearance_session.session_id, DecisionRequest(pilot_utterance=GOOD_READBACK))
        fields = {r.field: r for r in resp.readback_report}
        assert set(fields) == {"sid", "squawk", "initial_altitude"}
        assert all(r.matched for r in resp.readback_report)
        # The matched form is reported so the log can show what was recognised.
        assert fields["squawk"].matched_via is not None

    def test_report_marks_missing_fields(self, clearance_session):
        process_transmission(clearance_session.session_id, DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL))
        resp = process_transmission(clearance_session.session_id, DecisionRequest(pilot_utterance="acknowledged"))
        by_field = {r.field: r for r in resp.readback_report}
        assert by_field["squawk"].matched is False
        assert by_field["squawk"].matched_via is None
        # Accepted forms are exposed so the user can see what would have matched.
        assert "2341" in by_field["squawk"].accepted_forms

    def test_no_report_on_non_readback_state(self, clearance_session):
        resp = process_transmission(clearance_session.session_id, DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL))
        assert resp.readback_report == []


class TestFlowInterrupt:
    """Tests for the MAYDAY / PAN-PAN flow interrupt mechanism."""

    @pytest.fixture
    def clearance_session(self):
        from app.flow_loader import get_flow
        flow = get_flow("clearance")
        return create_session(flow)

    def test_mayday_from_initial_call_enters_emergency_flow(self, clearance_session):
        """MAYDAY at INITIAL_CALL suspends clearance and enters emergency-v1."""
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="MAYDAY MAYDAY MAYDAY"),
        )
        # Engine pushes clearance flow onto stack and enters emergency flow at MAYDAY_DECLARED.
        # ATC auto-advances to PILOT_STATES_EMERGENCY immediately.
        assert resp.next_state_id == "PILOT_STATES_EMERGENCY"
        trace_types = [t.type for t in resp.trace]
        assert "flow_interrupt" in trace_types

    def test_mayday_flow_stack_has_clearance_flow(self, clearance_session):
        """After MAYDAY interrupt the flow stack holds the clearance flow."""
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="MAYDAY MAYDAY MAYDAY"),
        )
        session = get_session(clearance_session.session_id)
        assert len(session.flow_stack) == 1
        assert session.flow_stack[0] == "clearance-v1"

    def test_mayday_emergency_progresses_through_states(self, clearance_session):
        """Can walk through the full emergency flow after a MAYDAY interrupt."""
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="MAYDAY MAYDAY MAYDAY"),
        )
        # Now at PILOT_STATES_EMERGENCY — pilot states the emergency
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="MAYDAY MAYDAY MAYDAY, DLH39A, engine fire, 150 souls, 2 hours fuel, returning to stand"),
        )
        # Advances through ATC_EMERGENCY_RESPONSE (auto) → PILOT_INTENTIONS
        assert resp.next_state_id == "PILOT_INTENTIONS"

    def test_mayday_during_readback_bypasses_readback_check(self, clearance_session):
        """MAYDAY mid-readback must not be blocked by the readback evaluator."""
        # Advance to PILOT_READBACK
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        # Saying MAYDAY without squawk/altitude should still trigger the emergency,
        # not be rejected with "Negative, say again squawk …"
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="MAYDAY MAYDAY MAYDAY"),
        )
        assert resp.next_state_id == "PILOT_STATES_EMERGENCY"
        assert resp.fallback_used is False
        trace_types = [t.type for t in resp.trace]
        assert "flow_interrupt" in trace_types
        assert "emergency_override" in trace_types

    def test_mayday_resume_restores_clearance_flow(self, clearance_session):
        """After EMERGENCY_COMPLETE the engine pops back to the clearance flow."""
        # 1. Trigger MAYDAY
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="MAYDAY MAYDAY MAYDAY"),
        )
        # 2. State emergency details (→ PILOT_INTENTIONS)
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="engine fire, 150 souls, fuel 2 hours, returning to stand"),
        )
        # 3. State intentions — reaches EMERGENCY_COMPLETE, engine pops flow
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance="DLH39A, returning to stand"),
        )
        session = get_session(clearance_session.session_id)
        # Stack should be empty again; active flow back to clearance
        assert len(session.flow_stack) == 0
        assert session.active_flow == "clearance-v1"
        # Resumed at INITIAL_CALL (where the interrupt happened)
        assert resp.next_state_id == "INITIAL_CALL"

"""Integration tests for the full decision engine.

The clearance flow (clearance-v1.yaml) models ICAO IFR clearance delivery:
  INITIAL_CALL → ATC_ISSUES_CLEARANCE (auto) → PILOT_READBACK
               → ATC_READBACK_CORRECT (auto) → CLEARANCE_COMPLETE

Trigger for INITIAL_CALL ok_next: "information|request.*clear|IFR|clearance|stand"
Readback required at PILOT_READBACK: squawk (2341) and initial_altitude (5000).
"""

import pytest

from app import session_store
from app.decision_engine import process_transmission
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

    def test_correct_readback_advances_to_clearance_complete(self, clearance_session):
        process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_INITIAL_CALL),
        )
        resp = process_transmission(
            clearance_session.session_id,
            DecisionRequest(pilot_utterance=GOOD_READBACK),
        )
        assert resp.next_state_id == "CLEARANCE_COMPLETE"
        assert resp.fallback_used is False

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

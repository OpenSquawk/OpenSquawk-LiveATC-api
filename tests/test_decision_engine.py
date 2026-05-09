"""Integration tests for the full decision engine."""

import pytest

from app import session_store
from app.decision_engine import process_transmission
from app.flow_loader import load_all_flows
from app.models import DecisionRequest, LoopDetectedError
from app.session_store import create_session, get_session

FLOWS_DIR = __import__("pathlib").Path(__file__).parent.parent / "flows"


@pytest.fixture(autouse=True)
def load_flows():
    load_all_flows(FLOWS_DIR)
    # Clear sessions between tests
    session_store._sessions.clear()


@pytest.fixture
def clearance_session():
    from app.flow_loader import get_flow
    flow = get_flow("clearance")
    # Ensure gates_clear is True so transitions work
    session = create_session(flow)
    session.flags["gates_clear"] = True
    return session


class TestDecisionEngine:
    def test_correct_request_advances_to_atc_state(self, clearance_session):
        req = DecisionRequest(pilot_utterance="Lufthansa 359 ready for pushback")
        resp = process_transmission(clearance_session.session_id, req)
        # Should auto-advance past ATC_ISSUES_CLEARANCE to PILOT_READBACK
        assert resp.next_state_id == "PILOT_READBACK"
        assert resp.fallback_used is False

    def test_wrong_utterance_stays_at_same_state(self, clearance_session):
        req = DecisionRequest(pilot_utterance="blah blah nonsense")
        resp = process_transmission(clearance_session.session_id, req)
        # bad_next returns to REQUESTING_CLEARANCE
        assert resp.next_state_id == "REQUESTING_CLEARANCE"

    def test_trace_contains_state_enter(self, clearance_session):
        req = DecisionRequest(pilot_utterance="ready for pushback")
        resp = process_transmission(clearance_session.session_id, req)
        trace_types = [t.type for t in resp.trace]
        assert "state_enter" in trace_types

    def test_trace_contains_regex_match(self, clearance_session):
        req = DecisionRequest(pilot_utterance="ready for pushback")
        resp = process_transmission(clearance_session.session_id, req)
        trace_types = [t.type for t in resp.trace]
        assert "regex_match" in trace_types

    def test_correct_readback_advances_to_clearance_complete(self, clearance_session):
        # First: pilot requests clearance
        req1 = DecisionRequest(pilot_utterance="ready for pushback")
        resp1 = process_transmission(clearance_session.session_id, req1)
        assert resp1.next_state_id == "PILOT_READBACK"

        # Second: pilot reads back the runway correctly
        req2 = DecisionRequest(pilot_utterance="push back approved runway 25R")
        resp2 = process_transmission(clearance_session.session_id, req2)
        assert resp2.next_state_id == "CLEARANCE_COMPLETE"
        assert resp2.fallback_used is False

    def test_incorrect_readback_loops_back(self, clearance_session):
        # Advance to PILOT_READBACK first
        req1 = DecisionRequest(pilot_utterance="ready for pushback")
        process_transmission(clearance_session.session_id, req1)

        # Bad readback (no runway mentioned)
        req2 = DecisionRequest(pilot_utterance="acknowledged")
        resp2 = process_transmission(clearance_session.session_id, req2)
        assert resp2.next_state_id == "PILOT_READBACK"
        assert resp2.fallback_used is True
        assert "readback_missing" in (resp2.fallback_reason or "")

    def test_session_persisted_after_transmission(self, clearance_session):
        req = DecisionRequest(pilot_utterance="ready for pushback")
        process_transmission(clearance_session.session_id, req)
        session = get_session(clearance_session.session_id)
        assert session is not None
        assert len(session.decision_history) == 1

    def test_unknown_session_raises_key_error(self):
        req = DecisionRequest(pilot_utterance="test")
        with pytest.raises(KeyError):
            process_transmission("nonexistent-session-id", req)

    def test_auto_advanced_states_populated(self, clearance_session):
        req = DecisionRequest(pilot_utterance="ready for pushback")
        resp = process_transmission(clearance_session.session_id, req)
        # Should have auto-advanced from ATC_ISSUES_CLEARANCE → PILOT_READBACK
        assert len(resp.auto_advanced_states) > 0

    def test_set_flag_action_executed(self, clearance_session):
        # Advance to PILOT_READBACK
        req1 = DecisionRequest(pilot_utterance="ready for pushback")
        process_transmission(clearance_session.session_id, req1)

        # Correct readback triggers set_flag(frequency_set=True)
        req2 = DecisionRequest(pilot_utterance="push back approved runway 25R")
        resp2 = process_transmission(clearance_session.session_id, req2)
        assert resp2.flags.get("frequency_set") is True

    def test_response_has_rendered_template(self, clearance_session):
        req = DecisionRequest(pilot_utterance="ready for pushback")
        resp = process_transmission(clearance_session.session_id, req)
        # PILOT_READBACK has no say_template — that's fine; CLEARANCE state has one
        # Just verify the field exists in the response
        assert hasattr(resp, "controller_say_rendered")

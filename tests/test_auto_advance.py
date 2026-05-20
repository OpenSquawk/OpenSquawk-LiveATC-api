"""Tests for auto-advance logic and loop detection."""

import pytest

from app.auto_advance import advance_through_non_pilot
from app.models import DecisionFlow, LoopDetectedError
from tests.conftest import make_simple_flow, make_state, make_transition


VARS: dict = {}
FLAGS: dict = {}


class TestAutoAdvance:
    def test_pilot_state_stops_immediately(self):
        states = {
            "PILOT": make_state("PILOT", role="pilot"),
        }
        flow = make_simple_flow(states)
        final, advanced, _ = advance_through_non_pilot("PILOT", flow, VARS, FLAGS)
        assert final == "PILOT"
        assert advanced == []

    def test_advances_through_atc_to_pilot(self):
        states = {
            "ATC": make_state("ATC", role="atc", auto_transitions=[make_transition("PILOT")]),
            "PILOT": make_state("PILOT", role="pilot"),
        }
        flow = make_simple_flow(states)
        final, advanced, _ = advance_through_non_pilot("ATC", flow, VARS, FLAGS)
        assert final == "PILOT"
        assert "PILOT" in advanced

    def test_advances_through_system_chain(self):
        states = {
            "SYS1": make_state("SYS1", role="system", auto_transitions=[make_transition("SYS2")]),
            "SYS2": make_state("SYS2", role="system", auto_transitions=[make_transition("PILOT")]),
            "PILOT": make_state("PILOT", role="pilot"),
        }
        flow = make_simple_flow(states)
        final, advanced, _ = advance_through_non_pilot("SYS1", flow, VARS, FLAGS)
        assert final == "PILOT"
        assert advanced == ["SYS2", "PILOT"]

    def test_stops_at_system_with_no_auto_transition(self):
        states = {
            "ATC": make_state("ATC", role="atc", auto_transitions=[make_transition("SYS")]),
            "SYS": make_state("SYS", role="system"),  # no auto_transitions
        }
        flow = make_simple_flow(states)
        final, advanced, _ = advance_through_non_pilot("ATC", flow, VARS, FLAGS)
        assert final == "SYS"

    def test_loop_detected_after_5_visits(self):
        # A → B → A → B → ... (loop)
        states = {
            "A": make_state("A", role="system", auto_transitions=[make_transition("B")]),
            "B": make_state("B", role="system", auto_transitions=[make_transition("A")]),
        }
        flow = make_simple_flow(states)
        with pytest.raises(LoopDetectedError) as exc_info:
            advance_through_non_pilot("A", flow, VARS, FLAGS)
        assert "visited" in str(exc_info.value).lower() or "loop" in str(exc_info.value).lower()

    def test_unknown_state_raises_key_error(self):
        states = {
            "A": make_state("A", role="atc", auto_transitions=[make_transition("GHOST")]),
        }
        flow = make_simple_flow(states)
        with pytest.raises(KeyError):
            advance_through_non_pilot("A", flow, VARS, FLAGS)

    def test_guard_filtered_auto_transition(self):
        from app.models import Guard
        guard = Guard(type="flag_check", name="open")
        states = {
            "ATC": make_state(
                "ATC", role="atc",
                auto_transitions=[make_transition("PILOT", condition=guard)],
            ),
            "PILOT": make_state("PILOT", role="pilot"),
        }
        flow = make_simple_flow(states)
        # open=False → guard fails → no auto-transition → stop at ATC
        final, advanced, _ = advance_through_non_pilot("ATC", flow, VARS, {"open": False})
        assert final == "ATC"
        assert advanced == []

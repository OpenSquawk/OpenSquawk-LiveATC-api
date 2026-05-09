"""Tests for the flow static validator."""

import pytest

from app.flow_validator import validate_flow
from tests.conftest import make_state, make_simple_flow, make_transition


class TestValidateFlow:
    def test_valid_clearance_flow(self, clearance_flow):
        result = validate_flow(clearance_flow)
        errors = [i for i in result.issues if i.severity == "error"]
        assert errors == [], f"Unexpected errors: {errors}"

    def test_missing_start_state(self):
        states = {
            "A": make_state("A", ok_next=[make_transition("B")]),
            "B": make_state("B", role="system"),
        }
        flow = make_simple_flow(states)
        flow.start_state = "NONEXISTENT"
        result = validate_flow(flow)
        errors = [i for i in result.issues if i.severity == "error"]
        assert any("start_state" in e.message for e in errors)

    def test_missing_end_state(self):
        states = {
            "A": make_state("A"),
            "B": make_state("B", role="system"),
        }
        flow = make_simple_flow(states)
        flow.end_states = ["GHOST"]
        result = validate_flow(flow)
        errors = [i for i in result.issues if i.severity == "error"]
        assert any("GHOST" in e.message for e in errors)

    def test_dangling_transition_reference(self):
        states = {
            "A": make_state("A", ok_next=[make_transition("GHOST")]),
            "B": make_state("B", role="system"),
        }
        flow = make_simple_flow(states)
        result = validate_flow(flow)
        errors = [i for i in result.issues if i.severity == "error"]
        assert any("GHOST" in e.message for e in errors)

    def test_unreachable_state_warning(self):
        states = {
            "A": make_state("A", ok_next=[make_transition("B")]),
            "B": make_state("B", role="system"),
            "ISLAND": make_state("ISLAND", role="system"),
        }
        flow = make_simple_flow(states)
        flow.end_states = ["B"]
        result = validate_flow(flow)
        warnings = [i for i in result.issues if i.severity == "warning"]
        assert any("ISLAND" in w.message for w in warnings)

    def test_deadlock_warning_for_atc_no_transitions(self):
        states = {
            "A": make_state("A", ok_next=[make_transition("B")]),
            "B": make_state("B", role="atc"),  # atc with no auto_transitions, not an end state
            "END": make_state("END", role="system"),
        }
        flow = make_simple_flow(states)
        # make_simple_flow sets end_states to last key ("END"), so "B" is not an end state
        result = validate_flow(flow)
        warnings = [i for i in result.issues if i.severity == "warning"]
        assert any("B" in w.message and "deadlock" in w.message.lower() for w in warnings)

    def test_valid_flag_guard_passes(self, clearance_flow):
        result = validate_flow(clearance_flow)
        # gates_clear is declared in flags — no warning expected for it
        flag_warnings = [
            i for i in result.issues
            if "gates_clear" in i.message and "not declared" in i.message
        ]
        assert flag_warnings == []

    def test_undeclared_flag_in_guard_warns(self):
        from app.models import Guard, Transition
        guard = Guard(type="flag_check", name="ghost_flag")
        trans = Transition(to="B", trigger="ok", condition=guard)
        states = {
            "A": make_state("A", ok_next=[trans]),
            "B": make_state("B", role="system"),
        }
        flow = make_simple_flow(states)
        result = validate_flow(flow)
        warnings = [i for i in result.issues if i.severity == "warning"]
        assert any("ghost_flag" in w.message for w in warnings)

"""Tests for Pydantic model parsing and validation."""

import pytest
from pydantic import ValidationError

from app.models import (
    Action,
    DecisionFlow,
    DecisionState,
    FlagDefinition,
    Guard,
    Transition,
    VariableDefinition,
)


class TestTransition:
    def test_valid_regex_trigger(self):
        t = Transition(to="NEXT", trigger="ready|pushback")
        assert t.to == "NEXT"

    def test_invalid_regex_trigger_raises(self):
        with pytest.raises(ValidationError):
            Transition(to="NEXT", trigger="[invalid")

    def test_no_trigger_is_auto(self):
        t = Transition(to="NEXT", trigger=None)
        assert t.trigger is None

    def test_is_emergency_default_false(self):
        t = Transition(to="NEXT")
        assert t.is_emergency is False

    def test_emergency_transition(self):
        t = Transition(to="MAYDAY", trigger="mayday", is_emergency=True)
        assert t.is_emergency is True

    def test_on_enter_actions_parsed(self):
        t = Transition(
            to="NEXT",
            on_enter_actions=[{"type": "set_flag", "target": "done", "value": True}],
        )
        assert len(t.on_enter_actions) == 1
        assert t.on_enter_actions[0].type == "set_flag"


class TestDecisionState:
    def test_basic_state(self):
        s = DecisionState(id="S1", role="pilot", name="S1", description="")
        assert s.role == "pilot"
        assert s.readback_mode == "none"

    def test_readback_fields(self):
        s = DecisionState(
            id="S1", role="pilot", name="S1", description="",
            readback_required=["runway", "callsign"],
            readback_mode="simple",
        )
        assert "runway" in s.readback_required

    def test_invalid_role(self):
        with pytest.raises(ValidationError):
            DecisionState(id="S1", role="tower", name="S1", description="")


class TestDecisionFlow:
    def test_ids_injected_from_dict_keys(self):
        flow = DecisionFlow(
            slug="test",
            schema_version="2.0",
            name="T",
            description="",
            start_state="A",
            end_states=["B"],
            variables={"callsign": {"type": "string", "initial": ""}},
            flags={"ready": {"initial": False}},
            states={
                "A": {"role": "pilot", "name": "A", "description": ""},
                "B": {"role": "system", "name": "B", "description": ""},
            },
        )
        assert flow.states["A"].id == "A"
        assert flow.states["B"].id == "B"
        assert flow.variables["callsign"].name == "callsign"
        assert flow.flags["ready"].name == "ready"

    def test_missing_required_field(self):
        with pytest.raises(ValidationError):
            DecisionFlow(
                # missing slug, name, etc.
                schema_version="2.0",
                start_state="A",
                end_states=["A"],
                states={"A": {"role": "pilot", "name": "A", "description": ""}},
            )


class TestGuard:
    def test_flag_check(self):
        g = Guard(type="flag_check", name="gates_clear")
        assert g.type == "flag_check"

    def test_comparison(self):
        g = Guard(type="comparison", name="speed", variable="speed", operator="gt", value=10)
        assert g.operator == "gt"

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            Guard(type="unknown_type", name="x")


class TestAction:
    def test_set_variable(self):
        a = Action(type="set_variable", target="callsign", value="DLH359")
        assert a.value == "DLH359"

    def test_set_flag(self):
        a = Action(type="set_flag", target="ready", value=True)
        assert a.type == "set_flag"

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            Action(type="explode", target="everything")

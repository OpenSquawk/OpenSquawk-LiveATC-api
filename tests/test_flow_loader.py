"""Tests for YAML flow loading."""

from pathlib import Path

import pytest

from app.flow_loader import get_flow, load_all_flows, load_flow_from_file
from app.models import DecisionFlow

FLOWS_DIR = Path(__file__).parent.parent / "flows"


class TestLoadFlowFromFile:
    def test_loads_clearance_flow(self, clearance_flow):
        assert clearance_flow.slug == "clearance-v1"
        assert clearance_flow.schema_version == "2.0"

    def test_start_state_in_states(self, clearance_flow):
        assert clearance_flow.start_state in clearance_flow.states

    def test_end_states_in_states(self, clearance_flow):
        for end in clearance_flow.end_states:
            assert end in clearance_flow.states

    def test_state_ids_injected(self, clearance_flow):
        for key, state in clearance_flow.states.items():
            assert state.id == key, f"State key '{key}' doesn't match id '{state.id}'"

    def test_variable_names_injected(self, clearance_flow):
        for key, var in clearance_flow.variables.items():
            assert var.name == key

    def test_flag_names_injected(self, clearance_flow):
        for key, flag in clearance_flow.flags.items():
            assert flag.name == key

    def test_transitions_parsed(self, clearance_flow):
        state = clearance_flow.states["INITIAL_CALL"]
        assert len(state.ok_next) > 0
        assert state.ok_next[0].trigger is not None

    def test_start_state_has_expected_pilot_template(self, clearance_flow):
        state = clearance_flow.states[clearance_flow.start_state]
        assert state.expected_pilot_template is not None

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_flow_from_file(Path("/does/not/exist.yaml"))


class TestLoadAllFlows:
    def test_loads_versioned_slugs(self):
        flows = load_all_flows(FLOWS_DIR)
        assert "clearance-v1" in flows
        assert "taxi-v1" in flows

    def test_returns_dict_of_decision_flows(self):
        flows = load_all_flows(FLOWS_DIR)
        for slug, flow in flows.items():
            assert isinstance(flow, DecisionFlow)
            assert flow.slug == slug


class TestVersionResolution:
    def test_bare_slug_resolves_to_latest(self):
        load_all_flows(FLOWS_DIR)
        flow = get_flow("clearance")
        assert flow.slug == "clearance-v1"

    def test_versioned_slug_direct_lookup(self):
        load_all_flows(FLOWS_DIR)
        flow = get_flow("clearance-v1")
        assert flow.slug == "clearance-v1"

    def test_unknown_slug_raises(self):
        load_all_flows(FLOWS_DIR)
        with pytest.raises(KeyError):
            get_flow("nonexistent-flow")

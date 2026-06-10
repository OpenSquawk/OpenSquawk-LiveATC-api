"""Shared pytest fixtures."""

import os

# Must run before any app.* import: keeps tests on the process-local store
# instead of writing a SQLite file into the repo.
os.environ.setdefault("SESSION_STORE_TYPE", "memory")

from pathlib import Path

import pytest

from app.models import (
    Action,
    DecisionFlow,
    DecisionState,
    FlagDefinition,
    Guard,
    Transition,
    VariableDefinition,
)

FLOWS_DIR = Path(__file__).parent.parent / "flows"


def make_transition(to: str, trigger: str | None = None, is_emergency: bool = False, condition=None) -> Transition:
    return Transition(to=to, trigger=trigger, is_emergency=is_emergency, condition=condition)


def make_state(
    state_id: str,
    role: str = "pilot",
    ok_next=None,
    bad_next=None,
    auto_transitions=None,
    readback_required=None,
    readback_mode: str = "none",
) -> DecisionState:
    return DecisionState(
        id=state_id,
        role=role,
        name=state_id,
        description="",
        ok_next=ok_next or [],
        bad_next=bad_next or [],
        auto_transitions=auto_transitions or [],
        readback_required=readback_required or [],
        readback_mode=readback_mode,
    )


def make_simple_flow(states: dict) -> DecisionFlow:
    """Build a minimal DecisionFlow from a states dict for testing."""
    state_ids = list(states.keys())
    return DecisionFlow(
        slug="test",
        schema_version="2.0",
        name="Test Flow",
        description="",
        start_state=state_ids[0],
        end_states=[state_ids[-1]],
        states=states,
    )


@pytest.fixture
def clearance_flow():
    from app.flow_loader import load_flow_from_file
    return load_flow_from_file(FLOWS_DIR / "clearance-v1.yaml")


@pytest.fixture
def taxi_flow():
    from app.flow_loader import load_flow_from_file
    return load_flow_from_file(FLOWS_DIR / "taxi-v1.yaml")

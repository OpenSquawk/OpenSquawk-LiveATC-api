"""Auto-advance through system/atc states until reaching a pilot state.

Includes loop detection: if any state is visited 5+ times, raise LoopDetectedError.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.guard_evaluator import evaluate_guard
from app.models import DecisionFlow, DecisionState, LoopDetectedError, Transition

logger = logging.getLogger(__name__)

MAX_HOPS = 50  # Hard safety cap


def find_auto_transition(
    state: DecisionState,
    variables: Dict[str, Any],
    flags: Dict[str, bool],
) -> Optional[Transition]:
    """Return the single valid auto-transition for this state, or None."""
    valid: List[Transition] = []
    for trans in state.auto_transitions:
        # Telemetry-gated edges are driven exclusively by process_telemetry; the
        # silent auto-advance walk must never take them.
        if trans.telemetry is not None:
            continue
        if trans.condition is not None:
            if evaluate_guard(trans.condition, variables, flags):
                valid.append(trans)
        else:
            valid.append(trans)

    if len(valid) == 1:
        return valid[0]
    if len(valid) > 1:
        logger.warning("Multiple valid auto-transitions in '%s' — taking first", state.id)
        return valid[0]
    return None


def advance_through_non_pilot(
    start_state_id: str,
    flow: DecisionFlow,
    variables: Dict[str, Any],
    flags: Dict[str, bool],
) -> Tuple[str, List[str], List[Transition]]:
    """Walk forward through system/atc states until reaching a pilot state.

    Returns (final_state_id, list_of_auto_advanced_state_ids, transitions_taken).
    The caller is responsible for executing the actions on the returned transitions.
    Raises LoopDetectedError if a state is visited 5+ times.
    """
    visited: Dict[str, int] = {}
    advanced: List[str] = []
    transitions_taken: List[Transition] = []
    current = start_state_id

    for _hop in range(MAX_HOPS):
        if current not in flow.states:
            raise KeyError(f"State '{current}' not found in flow '{flow.slug}'")

        state = flow.states[current]

        if state.role == "pilot":
            return current, advanced, transitions_taken

        visited[current] = visited.get(current, 0) + 1
        if visited[current] >= 5:
            loop_path = " → ".join(advanced + [current])
            raise LoopDetectedError(
                f"State '{current}' visited {visited[current]} times. "
                f"Loop path: {loop_path}"
            )

        next_trans = find_auto_transition(state, variables, flags)

        if next_trans is None:
            logger.debug("No auto-transition from '%s' — stopping auto-advance", current)
            return current, advanced, transitions_taken

        logger.debug("Auto-advance: '%s' → '%s'", current, next_trans.to)
        advanced.append(next_trans.to)
        transitions_taken.append(next_trans)
        current = next_trans.to

    raise LoopDetectedError(
        f"Max auto-advance hops ({MAX_HOPS}) reached starting from '{start_state_id}'"
    )

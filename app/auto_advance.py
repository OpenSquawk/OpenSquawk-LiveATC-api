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
) -> Tuple[str, List[str]]:
    """
    Walk forward through system/atc states until reaching a pilot state.

    Returns (final_state_id, list_of_auto_advanced_state_ids).
    Raises LoopDetectedError if a state is visited 5+ times.
    """
    visited: Dict[str, int] = {}
    advanced: List[str] = []
    current = start_state_id

    for _hop in range(MAX_HOPS):
        if current not in flow.states:
            raise KeyError(f"State '{current}' not found in flow '{flow.slug}'")

        state = flow.states[current]

        if state.role == "pilot":
            return current, advanced

        # Track visit count for loop detection
        visited[current] = visited.get(current, 0) + 1
        if visited[current] >= 5:
            loop_path = " → ".join(advanced + [current])
            raise LoopDetectedError(
                f"State '{current}' visited {visited[current]} times. "
                f"Loop path: {loop_path}"
            )

        # Find the next auto-transition
        next_trans = find_auto_transition(state, variables, flags)

        if next_trans is None:
            # No auto-transition; stop here (e.g. end state or waiting state)
            logger.debug("No auto-transition from '%s' — stopping auto-advance", current)
            return current, advanced

        logger.debug("Auto-advance: '%s' → '%s'", current, next_trans.to)
        advanced.append(next_trans.to)
        current = next_trans.to

    raise LoopDetectedError(
        f"Max auto-advance hops ({MAX_HOPS}) reached starting from '{start_state_id}'"
    )

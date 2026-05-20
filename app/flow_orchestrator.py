"""Foundation for multi-flow orchestration.

Three entry modes are modelled:
  replace   — switch to a new flow, discard the current position and stack
  interrupt — suspend the current flow (push onto stack), start the new one
  resume    — pop the stack and return to the interrupted flow

Parallel and linear entry modes are not yet implemented.
Stack depth is bounded by config.MAX_FLOW_STACK_DEPTH.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from app.config import MAX_FLOW_STACK_DEPTH
from app.models import DecisionFlow, RuntimeSession

logger = logging.getLogger(__name__)


class StackDepthError(RuntimeError):
    """Raised when the flow stack would exceed the configured depth limit."""


def replace_flow(session: RuntimeSession, new_flow_slug: str, new_state_id: str) -> None:
    """Switch to a new flow, discarding the current position and clearing the stack."""
    logger.info(
        "[session=%s] replace_flow: '%s'@'%s' → '%s'@'%s'",
        session.session_id,
        session.active_flow, session.current_state,
        new_flow_slug, new_state_id,
    )
    session.active_flow = new_flow_slug
    session.current_state = new_state_id
    session.flow_stack = []
    session.state_stack = []


def push_flow(session: RuntimeSession, new_flow_slug: str, new_state_id: str) -> None:
    """Suspend the current flow and start a new one (interrupt mode).

    The current flow and state are saved on the stack so they can be resumed
    when the interrupt flow completes.
    """
    if len(session.flow_stack) >= MAX_FLOW_STACK_DEPTH:
        raise StackDepthError(
            f"Flow stack depth limit ({MAX_FLOW_STACK_DEPTH}) reached; "
            f"cannot push '{new_flow_slug}'"
        )
    logger.info(
        "[session=%s] push_flow: suspending '%s'@'%s', starting '%s'@'%s'",
        session.session_id,
        session.active_flow, session.current_state,
        new_flow_slug, new_state_id,
    )
    session.flow_stack.append(session.active_flow)
    session.state_stack.append(session.current_state)
    session.active_flow = new_flow_slug
    session.current_state = new_state_id


def pop_flow(session: RuntimeSession) -> Tuple[Optional[str], Optional[str]]:
    """Resume the most recently interrupted flow.

    Returns (resumed_flow_slug, resumed_state_id), or (None, None) if the stack
    is empty (no interrupted flow to return to).
    """
    if not session.flow_stack:
        return None, None

    prev_flow = session.flow_stack.pop()
    prev_state = session.state_stack.pop()
    session.active_flow = prev_flow
    session.current_state = prev_state
    logger.info(
        "[session=%s] pop_flow: resumed '%s'@'%s'",
        session.session_id, prev_flow, prev_state,
    )
    return prev_flow, prev_state


def handle_flow_completion(
    session: RuntimeSession, flow: DecisionFlow
) -> Tuple[str, str]:
    """Call after every state transition.

    If the session is now at an end state and there is an interrupted flow on
    the stack, automatically resumes it and returns the (flow_slug, state_id)
    of the resumed position.  Otherwise returns the unchanged active flow and
    current state.
    """
    if session.current_state not in flow.end_states:
        return session.active_flow, session.current_state

    if not session.flow_stack:
        logger.info(
            "[session=%s] flow '%s' completed at end state '%s'",
            session.session_id, flow.slug, session.current_state,
        )
        return session.active_flow, session.current_state

    resumed_flow, resumed_state = pop_flow(session)
    logger.info(
        "[session=%s] flow '%s' completed — resumed '%s'@'%s'",
        session.session_id, flow.slug, resumed_flow, resumed_state,
    )
    return session.active_flow, session.current_state

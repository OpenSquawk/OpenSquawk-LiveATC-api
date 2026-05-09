"""Execute side effects (Actions) on session state."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.models import Action, RuntimeSession

logger = logging.getLogger(__name__)


def execute_actions(
    actions: List[Action],
    session: RuntimeSession,
) -> List[str]:
    """
    Apply a list of actions to the session in-place.

    Returns a list of log messages for the trace.
    """
    trace_msgs: List[str] = []
    for action in actions:
        try:
            msg = _execute_one(action, session)
            trace_msgs.append(msg)
        except Exception as exc:
            msg = f"Action {action.type}:{action.target} failed: {exc}"
            logger.error(msg)
            trace_msgs.append(msg)
    return trace_msgs


def _execute_one(action: Action, session: RuntimeSession) -> str:
    if action.type == "set_variable":
        session.variables[action.target] = action.value
        return f"set_variable '{action.target}' = {action.value!r}"

    if action.type == "set_flag":
        val = bool(action.value) if action.value is not None else True
        session.flags[action.target] = val
        return f"set_flag '{action.target}' = {val}"

    if action.type == "log":
        msg = f"log '{action.target}'"
        logger.info("Action log [session=%s]: %s", session.session_id, action.target)
        return msg

    if action.type == "call_service":
        # Stub: services not yet implemented
        msg = f"call_service '{action.target}' (stub — not implemented)"
        logger.warning(msg)
        return msg

    return f"unknown action type '{action.type}'"

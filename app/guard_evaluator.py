"""Evaluate Guard conditions against session runtime state."""

from __future__ import annotations

import logging
import operator as op
from typing import Any, Dict

from app.models import Guard

logger = logging.getLogger(__name__)

_OPERATORS = {
    "eq": op.eq,
    "ne": op.ne,
    "gt": op.gt,
    "lt": op.lt,
    "gte": op.ge,
    "lte": op.le,
}


def evaluate_guard(guard: Guard, variables: Dict[str, Any], flags: Dict[str, bool]) -> bool:
    """Return True if the guard condition passes."""
    try:
        if guard.type == "flag_check":
            return bool(flags.get(guard.name, False))

        if guard.type == "variable_match":
            var_name = guard.variable or guard.name
            return variables.get(var_name) == guard.value

        if guard.type == "comparison":
            var_name = guard.variable or guard.name
            actual = variables.get(var_name)
            if guard.operator is None:
                # Default: truthiness check
                return bool(actual)
            compare = _OPERATORS.get(guard.operator)
            if compare is None:
                logger.warning("Unknown operator '%s' in guard", guard.operator)
                return False
            return compare(actual, guard.value)

    except Exception as exc:
        logger.error("Guard evaluation crashed (%s): %s", guard, exc)
        return False

    logger.warning("Unknown guard type '%s'", guard.type)
    return False

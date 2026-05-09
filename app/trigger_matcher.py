"""Regex-first transition matching with emergency override.

Priority:
  1. Emergency transitions (is_emergency=True) checked first — any match wins immediately.
  2. Filter by guard conditions.
  3. Regex match remaining candidates.
  4. Return result + reason string for trace.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.guard_evaluator import evaluate_guard
from app.models import Transition

logger = logging.getLogger(__name__)


def _regex_match(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def select_transition(
    pilot_utterance: str,
    candidates: List[Transition],
    variables: Dict[str, Any],
    flags: Dict[str, bool],
) -> Tuple[Optional[Transition], str]:
    """
    Returns (selected_transition, reason).

    reason values:
      "emergency_override"  — is_emergency transition matched
      "regex_match"         — single unambiguous regex match
      "ambiguous_first"     — multiple matches; took first (validator should catch this)
      "no_match"            — no regex matched; caller should fall back to LLM
    """

    # --- Step 1: Emergency override ---
    for t in candidates:
        if t.is_emergency and t.trigger:
            if _regex_match(t.trigger, pilot_utterance):
                logger.debug("EMERGENCY override matched → %s", t.to)
                return t, "emergency_override"

    # --- Step 2: Filter by guard conditions ---
    guard_passed: List[Transition] = []
    for t in candidates:
        if t.is_emergency:
            continue  # already checked above
        if t.condition is not None:
            if evaluate_guard(t.condition, variables, flags):
                guard_passed.append(t)
            else:
                logger.debug("Guard failed for transition → %s", t.to)
        else:
            guard_passed.append(t)

    # --- Step 3: Regex match ---
    matching: List[Transition] = []
    for t in guard_passed:
        if t.trigger is None:
            continue  # auto-transition; not matched by utterance
        if _regex_match(t.trigger, pilot_utterance):
            matching.append(t)
            logger.debug("Trigger '%s' matched → %s", t.trigger, t.to)

    # --- Step 4: Decide ---
    if len(matching) == 1:
        return matching[0], "regex_match"

    if len(matching) == 0:
        return None, "no_match"

    logger.warning(
        "Ambiguous transitions for utterance '%s': %s — taking first",
        pilot_utterance,
        [t.to for t in matching],
    )
    return matching[0], "ambiguous_first"

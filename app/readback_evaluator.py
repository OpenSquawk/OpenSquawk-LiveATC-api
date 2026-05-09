"""Evaluate pilot readback correctness.

Simple mode: check whether the current value of each required variable
appears literally in the pilot utterance (case-insensitive).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


def evaluate_readback_simple(
    pilot_utterance: str,
    readback_required: List[str],
    variables: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Returns (passed, missing_fields).

    A field passes if its current variable value (converted to str) is found
    as a word/substring in the utterance.
    """
    missing = []
    for field in readback_required:
        expected = variables.get(field)
        if expected is None:
            # Variable not set — can't verify, treat as missing
            missing.append(field)
            continue
        expected_str = str(expected).strip()
        if not expected_str:
            continue  # Empty value — skip check
        if not re.search(re.escape(expected_str), pilot_utterance, re.IGNORECASE):
            missing.append(field)

    return (len(missing) == 0), missing


def check_readback(
    pilot_utterance: str,
    readback_required: List[str],
    readback_mode: str,
    variables: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    """
    Dispatch to the appropriate readback evaluator.

    Returns (passed, missing_fields).
    """
    if readback_mode == "none" or not readback_required:
        return True, []

    if readback_mode in ("simple", "strict"):
        # strict is reserved for Phase 6 phonetic matching; use simple for now
        return evaluate_readback_simple(pilot_utterance, readback_required, variables)

    return True, []

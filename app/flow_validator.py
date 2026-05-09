"""Static validator for DecisionFlow definitions.

Run at load time to catch design issues early.
"""

from __future__ import annotations

import re
from collections import deque
from typing import List, Set

from app.models import DecisionFlow, DecisionState, FlowValidationResult, ValidationIssue


def _reachable_states(flow: DecisionFlow, start: str) -> Set[str]:
    """BFS to find all state IDs reachable from start."""
    visited: Set[str] = set()
    queue = deque([start])
    while queue:
        sid = queue.popleft()
        if sid in visited or sid not in flow.states:
            continue
        visited.add(sid)
        state = flow.states[sid]
        for trans_list in [state.ok_next, state.bad_next, state.auto_transitions]:
            for trans in trans_list:
                if trans.to not in visited:
                    queue.append(trans.to)
    return visited


def _all_transitions(state: DecisionState):
    return state.ok_next + state.bad_next + state.auto_transitions


def _sample_inputs_from_triggers(transitions) -> List[str]:
    """Generate a small set of test inputs derived from trigger patterns."""
    samples = []
    for trans in transitions:
        if not trans.trigger:
            continue
        # Strip regex metacharacters to get a plausible literal sample
        literal = re.sub(r"[\\^$.*+?{}[\]|()]", "", trans.trigger)
        literal = literal.strip()
        if literal:
            samples.append(literal)
    return samples


def validate_flow(flow: DecisionFlow) -> FlowValidationResult:
    issues: List[ValidationIssue] = []

    # 1. Start state exists
    if flow.start_state not in flow.states:
        issues.append(ValidationIssue(
            severity="error",
            message=f"start_state '{flow.start_state}' not found in states",
        ))

    # 2. End states exist
    for end in flow.end_states:
        if end not in flow.states:
            issues.append(ValidationIssue(
                severity="error",
                message=f"end_state '{end}' not found in states",
            ))

    # 3. All referenced states exist
    for sid, state in flow.states.items():
        for trans in _all_transitions(state):
            if trans.to not in flow.states:
                issues.append(ValidationIssue(
                    severity="error",
                    message=f"State '{sid}' → transition to non-existent state '{trans.to}'",
                    state_id=sid,
                ))

    # 4. Reachability of end states
    if flow.start_state in flow.states:
        reachable = _reachable_states(flow, flow.start_state)
        for end in flow.end_states:
            if end not in reachable:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=f"End state '{end}' may not be reachable from start state '{flow.start_state}'",
                ))

        # Warn about states unreachable from start
        for sid in flow.states:
            if sid not in reachable:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=f"State '{sid}' is unreachable from start state '{flow.start_state}'",
                    state_id=sid,
                ))

    # 5. Deadlock detection: system/atc states with no way out
    for sid, state in flow.states.items():
        if state.role in ("system", "atc") and sid not in flow.end_states:
            all_trans = _all_transitions(state)
            if not all_trans:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=(
                        f"State '{sid}' is role='{state.role}' with no transitions "
                        "and is not an end state — potential deadlock"
                    ),
                    state_id=sid,
                ))

    # 6. Ambiguous trigger detection for pilot states
    for sid, state in flow.states.items():
        if state.role != "pilot":
            continue
        non_emergency = [
            t for t in state.ok_next + state.bad_next
            if not t.is_emergency and t.trigger
        ]
        samples = _sample_inputs_from_triggers(non_emergency)
        for sample in samples:
            matching = [
                t for t in non_emergency
                if t.trigger and re.search(t.trigger, sample, re.IGNORECASE)
            ]
            if len(matching) > 1:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=(
                        f"State '{sid}': ambiguous transitions for sample input '{sample}' "
                        f"— matched {[t.to for t in matching]}. "
                        "Make triggers mutually exclusive."
                    ),
                    state_id=sid,
                ))

    # 7. Guard references non-existent variable/flag
    all_var_names = set(flow.variables.keys())
    all_flag_names = set(flow.flags.keys())
    for sid, state in flow.states.items():
        for trans in _all_transitions(state):
            if not trans.condition:
                continue
            guard = trans.condition
            if guard.type == "flag_check" and guard.name not in all_flag_names:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=(
                        f"State '{sid}': guard flag_check '{guard.name}' not declared in flags"
                    ),
                    state_id=sid,
                ))
            elif guard.type in ("comparison", "variable_match"):
                target_var = guard.variable or guard.name
                if target_var not in all_var_names:
                    issues.append(ValidationIssue(
                        severity="warning",
                        message=(
                            f"State '{sid}': guard references variable '{target_var}' "
                            "not declared in variables"
                        ),
                        state_id=sid,
                    ))

    # 8. Readback fields reference declared variables
    for sid, state in flow.states.items():
        for field in state.readback_required:
            if field not in all_var_names and field not in all_flag_names:
                issues.append(ValidationIssue(
                    severity="warning",
                    message=(
                        f"State '{sid}': readback_required field '{field}' "
                        "not declared in variables or flags"
                    ),
                    state_id=sid,
                ))

    has_errors = any(i.severity == "error" for i in issues)
    return FlowValidationResult(
        flow_slug=flow.slug,
        valid=not has_errors,
        issues=issues,
    )

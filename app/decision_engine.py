"""Main decision engine — the 13-step algorithm from the blueprint.

Phase 2: deterministic routing only (regex + guards + readback).
LLM fallback is stubbed and will be wired in Phase 5.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.action_executor import execute_actions
from app.auto_advance import advance_through_non_pilot
from app.flow_loader import get_flow
from app.flow_orchestrator import handle_flow_completion
from app.guard_evaluator import evaluate_guard
from app.models import (
    DecisionFlow,
    DecisionRequest,
    DecisionResponse,
    DecisionState,
    LoopDetectedError,
    RuntimeSession,
    Transition,
    TransitionTrace,
)
from app.readback_evaluator import check_readback
from app.session_store import get_session, save_session
from app.template_renderer import render_template
from app.trigger_matcher import select_transition

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trace(t: str, msg: str) -> TransitionTrace:
    return TransitionTrace(type=t, message=msg)


def _get_state(flow: DecisionFlow, state_id: str) -> DecisionState:
    if state_id not in flow.states:
        raise KeyError(f"State '{state_id}' not found in flow '{flow.slug}'")
    return flow.states[state_id]


def _select_pilot_transition(
    pilot_utterance: str,
    state: DecisionState,
    variables: dict,
    flags: dict,
    trace: List[TransitionTrace],
) -> Tuple[Optional[Transition], str, bool]:
    """
    Two-phase matching: ok_next first, then bad_next as fallback.

    Returns (transition, reason, used_bad_next).

    Emergency transitions are checked across all candidates first.
    """
    all_candidates = state.ok_next + state.bad_next

    # Emergency override across everything
    for t in all_candidates:
        if t.is_emergency and t.trigger:
            import re
            if re.search(t.trigger, pilot_utterance, re.IGNORECASE):
                return t, "emergency_override", False

    # Phase 1: try ok_next only
    ok_trans, ok_reason = select_transition(
        pilot_utterance, state.ok_next, variables, flags
    )
    if ok_trans is not None:
        return ok_trans, ok_reason, False

    # Phase 2: fall back to bad_next (take first valid by guard, no regex needed)
    if state.bad_next:
        from app.guard_evaluator import evaluate_guard
        for t in state.bad_next:
            if t.condition is None or evaluate_guard(t.condition, variables, flags):
                return t, "bad_next_fallback", True

    return None, ok_reason, False  # Nothing matched at all


def _apply_transition_actions(
    transition: Transition,
    current_state: DecisionState,
    next_state: DecisionState,
    session: RuntimeSession,
    trace: List[TransitionTrace],
) -> None:
    """Execute on_exit (current state's transition) then on_enter (next state's transition)."""
    # on_exit actions live on the transition itself (from current state's perspective)
    exit_msgs = execute_actions(transition.on_exit_actions, session)
    for msg in exit_msgs:
        trace.append(_trace("action_execute", f"on_exit: {msg}"))

    # on_enter actions from the transition
    enter_msgs = execute_actions(transition.on_enter_actions, session)
    for msg in enter_msgs:
        trace.append(_trace("action_execute", f"on_enter: {msg}"))


# ---------------------------------------------------------------------------
# LLM fallback stub (Phase 5)
# ---------------------------------------------------------------------------

def _llm_fallback(
    pilot_utterance: str,
    state: DecisionState,
    candidates: List[Transition],
    session: RuntimeSession,
) -> Tuple[Optional[Transition], str]:
    """Stub — returns (None, reason) so caller falls back to bad_next."""
    logger.warning(
        "LLM fallback not yet implemented. Utterance: '%s', state: '%s'",
        pilot_utterance,
        state.id,
    )
    return None, "llm_not_implemented"


# ---------------------------------------------------------------------------
# Core engine
# ---------------------------------------------------------------------------

def process_transmission(
    session_id: str,
    request: DecisionRequest,
) -> DecisionResponse:
    """
    Execute the full 13-step decision algorithm.

    Raises:
      KeyError — session or state not found
      LoopDetectedError — auto-advance cycle detected
    """
    trace: List[TransitionTrace] = []
    fallback_used = False
    fallback_reason: Optional[str] = None

    # --- Step 1: Load session ---
    session = get_session(session_id)
    if session is None:
        raise KeyError(f"Session '{session_id}' not found")

    # --- Step 2: Timer check (stub — timers not yet implemented) ---
    # TODO: check expired active_timers and auto-advance accordingly

    # --- Step 3: Load current state ---
    flow = get_flow(session.active_flow)
    current_state = _get_state(flow, session.current_state)
    trace.append(_trace("state_enter", f"Current state: '{current_state.id}' (role={current_state.role})"))

    # --- Step 4: If current is system/atc, auto-advance to pilot state ---
    if current_state.role != "pilot":
        new_state_id, advanced, _ = advance_through_non_pilot(
            current_state.id, flow, session.variables, session.flags
        )
        for sid in advanced:
            trace.append(_trace("auto_advance", f"Auto-advanced through '{sid}'"))
        current_state = _get_state(flow, new_state_id)
        session.current_state = new_state_id

        if advanced:
            trace.append(_trace("state_enter", f"Reached pilot state: '{current_state.id}'"))

    # --- Step 5: Build candidates ---
    all_candidates = current_state.ok_next + current_state.bad_next
    trace.append(_trace("candidates", f"Built {len(all_candidates)} candidates: {[t.to for t in all_candidates]}"))

    # --- Step 6: Match utterance against candidates (emergency first, ok_next before bad_next) ---
    selected_transition, match_reason, used_bad_next = _select_pilot_transition(
        request.pilot_utterance,
        current_state,
        session.variables,
        session.flags,
        trace,
    )

    if match_reason == "emergency_override":
        trace.append(_trace("emergency_override", f"EMERGENCY transition matched → '{selected_transition.to}'"))
    elif match_reason == "regex_match":
        trace.append(_trace("regex_match", f"Trigger matched → '{selected_transition.to}' ({selected_transition.label or ''})"))
    elif match_reason == "ambiguous_first":
        trace.append(_trace("ambiguous", f"Ambiguous match — took first candidate '{selected_transition.to}'"))
    elif match_reason == "bad_next_fallback":
        trace.append(_trace("bad_next_fallback", f"No ok_next matched — using bad_next → '{selected_transition.to}'"))
    elif match_reason == "no_match":
        trace.append(_trace("no_regex_match", f"No trigger matched utterance '{request.pilot_utterance}'"))

    # --- Step 7: Readback evaluation (if required) ---
    if selected_transition is not None and current_state.readback_required and current_state.readback_mode != "none":
        passed, missing = check_readback(
            request.pilot_utterance,
            current_state.readback_required,
            current_state.readback_mode,
            session.variables,
        )
        if passed:
            trace.append(_trace("readback_pass", f"Readback OK — fields present: {current_state.readback_required}"))
        else:
            trace.append(_trace("readback_fail", f"Readback missing fields: {missing} — using bad_next"))
            # Override: push to bad_next
            if current_state.bad_next:
                selected_transition = current_state.bad_next[0]
                fallback_used = True
                fallback_reason = f"readback_missing: {missing}"
            else:
                trace.append(_trace("readback_fail", "No bad_next available; proceeding anyway"))

    # --- Step 8: LLM fallback if still no candidate ---
    if selected_transition is None:
        llm_trans, llm_reason = _llm_fallback(
            request.pilot_utterance, current_state, all_candidates, session
        )
        fallback_used = True
        if llm_trans is not None:
            selected_transition = llm_trans
            fallback_reason = f"llm_routed: {llm_reason}"
            trace.append(_trace("llm_call", f"LLM selected → '{selected_transition.to}'"))
        else:
            # Hard fallback: first bad_next
            fallback_reason = f"llm_failed ({llm_reason}) — using first bad_next"
            trace.append(_trace("fallback", fallback_reason))
            if current_state.bad_next:
                selected_transition = current_state.bad_next[0]
            else:
                raise ValueError(
                    f"No valid transition from state '{current_state.id}' "
                    f"for utterance '{request.pilot_utterance}' and no bad_next defined"
                )

    # --- Step 9: Validate selected state ---
    if selected_transition.to not in flow.states:
        raise KeyError(f"Selected next state '{selected_transition.to}' not found in flow '{flow.slug}'")

    next_state = _get_state(flow, selected_transition.to)
    trace.append(_trace("transition", f"'{current_state.id}' → '{next_state.id}'"))

    # --- Step 10: Apply side effects ---
    _apply_transition_actions(selected_transition, current_state, next_state, session, trace)

    # --- Step 11: Transition ---
    session.current_state = next_state.id

    # Auto-advance through any new system/atc states.
    # We also collect the first ATC "say_template" we pass through so the
    # frontend knows what the controller should speak after the transition.
    auto_advanced_states: List[str] = []
    atc_say_template: Optional[str] = None

    if next_state.role != "pilot":
        if next_state.say_template:
            atc_say_template = next_state.say_template

        final_state_id, advanced2, auto_transitions_taken = advance_through_non_pilot(
            next_state.id, flow, session.variables, session.flags
        )
        auto_advanced_states = advanced2
        for sid in advanced2:
            trace.append(_trace("auto_advance", f"Auto-advanced through '{sid}'"))
            if atc_say_template is None:
                intermediate = flow.states.get(sid)
                if intermediate and intermediate.say_template:
                    atc_say_template = intermediate.say_template

        for auto_trans in auto_transitions_taken:
            action_msgs = execute_actions(auto_trans.on_exit_actions + auto_trans.on_enter_actions, session)
            for msg in action_msgs:
                trace.append(_trace("action_execute", f"auto_advance: {msg}"))

        session.current_state = final_state_id
        next_state = _get_state(flow, final_state_id)

    # --- Flow completion / interrupt resume ---
    # If the new state is an end state and the flow stack is non-empty,
    # orchestrator pops back to the interrupted flow automatically.
    prev_flow_slug = flow.slug
    handle_flow_completion(session, flow)
    if session.active_flow != prev_flow_slug:
        flow = get_flow(session.active_flow)
        next_state = _get_state(flow, session.current_state)
        trace.append(_trace("flow_resume", f"Resumed flow '{flow.slug}' at '{next_state.id}'"))

    # --- Step 12: Generate response ---
    # Use ATC speech collected during auto-advance; fall back to the final state's template.
    say_template = atc_say_template or next_state.say_template
    rendered = render_template(say_template, session.variables)
    expected = render_template(next_state.expected_pilot_template, session.variables)

    # --- Step 13: Save session ---
    session.decision_history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pilot_utterance": request.pilot_utterance,
        "previous_state": current_state.id,
        "next_state": next_state.id,
        "match_reason": match_reason,
        "fallback_used": fallback_used,
    })
    save_session(session)

    return DecisionResponse(
        session_id=session_id,
        next_state_id=next_state.id,
        controller_say_template=say_template,
        controller_say_rendered=rendered,
        expected_pilot_template=expected,
        variables=dict(session.variables),
        flags=dict(session.flags),
        trace=trace,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        auto_advanced_states=auto_advanced_states,
    )


# ---------------------------------------------------------------------------
# Timeout / silence handler
# ---------------------------------------------------------------------------

def process_timeout(session_id: str) -> DecisionResponse:
    """Fire the silence timeout for the current pilot state.

    The frontend calls this endpoint when the ``auto_advance_timeout_ms`` timer
    expires without a pilot utterance.  The backend finds the first valid
    ``auto_transition`` (trigger=None, guard passes) and advances through it.

    Raises:
      KeyError  — session not found
      ValueError — current state doesn't support silence timeout
    """
    trace: List[TransitionTrace] = []

    session = get_session(session_id)
    if session is None:
        raise KeyError(f"Session '{session_id}' not found")

    flow = get_flow(session.active_flow)
    current_state = _get_state(flow, session.current_state)
    trace.append(_trace("timeout", f"Silence timeout fired in state '{current_state.id}'"))

    if current_state.role != "pilot":
        raise ValueError(
            f"Timeout only applies to pilot states; "
            f"'{current_state.id}' is role={current_state.role}"
        )

    if not current_state.auto_advance_on_silence:
        raise ValueError(
            f"State '{current_state.id}' does not have auto_advance_on_silence enabled"
        )

    # Find the first auto_transition with no trigger whose guard passes.
    timeout_trans: Optional[Transition] = None
    for t in current_state.auto_transitions:
        if t.trigger is not None:
            continue
        if t.condition is None or evaluate_guard(t.condition, session.variables, session.flags):
            timeout_trans = t
            break

    if timeout_trans is None:
        raise ValueError(
            f"No unconditional auto-transition found in state '{current_state.id}'"
        )

    if timeout_trans.to not in flow.states:
        raise KeyError(f"Timeout target state '{timeout_trans.to}' not found in flow '{flow.slug}'")

    next_state = _get_state(flow, timeout_trans.to)
    trace.append(_trace("timeout_advance", f"'{current_state.id}' → '{next_state.id}'"))

    # Apply side effects
    _apply_transition_actions(timeout_trans, current_state, next_state, session, trace)
    session.current_state = next_state.id

    # Auto-advance through non-pilot states
    auto_advanced_states: List[str] = []
    atc_say_template: Optional[str] = None

    if next_state.role != "pilot":
        if next_state.say_template:
            atc_say_template = next_state.say_template
        final_state_id, advanced, auto_transitions_taken = advance_through_non_pilot(
            next_state.id, flow, session.variables, session.flags
        )
        auto_advanced_states = advanced
        for sid in advanced:
            trace.append(_trace("auto_advance", f"Auto-advanced through '{sid}'"))
            if atc_say_template is None:
                intermediate = flow.states.get(sid)
                if intermediate and intermediate.say_template:
                    atc_say_template = intermediate.say_template
        for auto_trans in auto_transitions_taken:
            action_msgs = execute_actions(auto_trans.on_exit_actions + auto_trans.on_enter_actions, session)
            for msg in action_msgs:
                trace.append(_trace("action_execute", f"auto_advance: {msg}"))
        session.current_state = final_state_id
        next_state = _get_state(flow, final_state_id)

    handle_flow_completion(session, flow)

    say_template = atc_say_template or next_state.say_template
    rendered = render_template(say_template, session.variables)
    expected = render_template(next_state.expected_pilot_template, session.variables)

    session.decision_history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pilot_utterance": "__TIMEOUT__",
        "previous_state": current_state.id,
        "next_state": next_state.id,
        "match_reason": "silence_timeout",
        "fallback_used": False,
    })
    save_session(session)

    return DecisionResponse(
        session_id=session_id,
        next_state_id=next_state.id,
        controller_say_template=say_template,
        controller_say_rendered=rendered,
        expected_pilot_template=expected,
        variables=dict(session.variables),
        flags=dict(session.flags),
        trace=trace,
        fallback_used=False,
        fallback_reason=None,
        auto_advanced_states=auto_advanced_states,
    )

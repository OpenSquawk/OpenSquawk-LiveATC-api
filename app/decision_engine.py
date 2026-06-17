"""Main decision engine — the 13-step algorithm from the blueprint.

Phase 2: deterministic routing only (regex + guards + readback).
LLM fallback is stubbed and will be wired in Phase 5.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.action_executor import execute_actions
from app.auto_advance import advance_through_non_pilot
from app.flow_loader import get_flow
from app.flow_orchestrator import handle_flow_completion, push_flow
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

_SEP = "─" * 60

# ---------------------------------------------------------------------------
# Global emergency intercept
# ---------------------------------------------------------------------------
# Any pilot utterance matching this pattern triggers the emergency flow
# regardless of which state the session is currently in.  Flow authors do
# NOT need to add MAYDAY transitions to individual states.

_EMERGENCY_RE = re.compile(r"mayday|pan[.\s]pan", re.IGNORECASE)
_EMERGENCY_FLOW = "emergency-v1"
_EMERGENCY_ENTRY = "MAYDAY_DECLARED"


# ---------------------------------------------------------------------------
# Global greeting intercept
# ---------------------------------------------------------------------------
# At an initial-contact pilot state (allow_greeting=True) a pilot may make a
# bare courtesy call ("München Tower, DLH39A, good day") before passing their
# full request.  Standard ATC reply is "pass your message" / "go ahead".  The
# greeting is OPTIONAL: an utterance that also contains the actual request
# matches a normal ok_next trigger and never reaches this handler.

_GREETING_RE = re.compile(
    r"\b("
    r"hello|hallo|hi|hey|good\s*(morning|afternoon|evening|day)|"
    r"guten\s*(morgen|tag|abend)|gr(ü|ue)(ss|ß)\s*gott|servus|moin"
    r")\b",
    re.IGNORECASE,
)
_GREETING_REPLY = "{{callsign}}, pass your message"


def _is_greeting(utterance: str) -> bool:
    return bool(_GREETING_RE.search(utterance))


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _fmt_dict(d: dict, max_items: int = 8) -> str:
    """Compact single-line dict repr, trimmed to max_items."""
    items = list(d.items())[:max_items]
    suffix = ", …" if len(d) > max_items else ""
    return "{" + ", ".join(f"{k}: {v!r}" for k, v in items) + suffix + "}"


def _log_request(session_id: str, utterance: str, state_id: str, flow_slug: str,
                 variables: dict, flags: dict) -> None:
    logger.info(
        "▶ TRANSMIT  session=%.8s  flow=%s  state=%s\n"
        "  utterance : %r\n"
        "  variables : %s\n"
        "  flags     : %s",
        session_id, flow_slug, state_id,
        utterance,
        _fmt_dict(variables),
        _fmt_dict(flags),
    )


def _log_result(session_id: str, state_in: str, state_out: str, match_reason: str,
                auto_advanced: List[str], fallback_used: bool, fallback_reason: Optional[str],
                say_template: Optional[str], trace: List[Any]) -> None:
    auto_str = " → ".join(auto_advanced) if auto_advanced else "none"
    fallback_str = f"YES — {fallback_reason}" if fallback_used else "no"
    logger.info(
        "✓ RESULT    session=%.8s  %s → %s\n"
        "  match     : %s\n"
        "  auto-adv  : %s\n"
        "  fallback  : %s\n"
        "  say       : %s",
        session_id, state_in, state_out,
        match_reason,
        auto_str,
        fallback_str,
        (say_template or "—")[:120],
    )
    # Emit each trace entry at DEBUG so it's available when LOG_LEVEL=DEBUG
    # without polluting INFO output.
    for t in trace:
        logger.debug("  trace  [%s] %s", t.type, t.message)


def _log_error(session_id: str, state_id: str, utterance: str, exc: Exception) -> None:
    logger.error(
        "✗ FAILED    session=%.8s  state=%s\n"
        "  utterance : %r\n"
        "  error     : %s: %s",
        session_id, state_id,
        utterance,
        type(exc).__name__, exc,
    )


# ---------------------------------------------------------------------------
# Trace helper
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

    Note: global emergency interception (MAYDAY / PAN-PAN) is handled
    upstream in process_transmission before this function is called.
    """
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
# Stay-in-place response (greeting acknowledgement)
# ---------------------------------------------------------------------------

def _build_stay_response(
    session: RuntimeSession,
    state: DecisionState,
    say_template: str,
    pilot_utterance: str,
    match_reason: str,
    trace: List[TransitionTrace],
) -> DecisionResponse:
    """Acknowledge the pilot without leaving the current pilot state.

    Used for the optional greeting handshake: ATC says "pass your message"
    and the session remains at the same initial-contact state so the pilot can
    now make the full request.
    """
    rendered = render_template(say_template, session.variables)
    expected = render_template(state.expected_pilot_template, session.variables)

    session.decision_history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pilot_utterance": pilot_utterance,
        "previous_state": state.id,
        "next_state": state.id,
        "match_reason": match_reason,
        "fallback_used": False,
    })
    save_session(session)

    return DecisionResponse(
        session_id=session.session_id,
        next_state_id=state.id,
        active_flow=session.active_flow,
        controller_say_template=say_template,
        controller_say_rendered=rendered,
        expected_pilot_template=expected,
        variables=dict(session.variables),
        flags=dict(session.flags),
        trace=trace,
        fallback_used=False,
        fallback_reason=None,
        auto_advanced_states=[],
        session_complete=False,
    )


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
    readback_report: List[Dict[str, Any]] = []

    # --- Step 1: Load session ---
    session = get_session(session_id)
    if session is None:
        logger.warning("✗ TRANSMIT  session=%.8s  NOT FOUND", session_id)
        raise KeyError(f"Session '{session_id}' not found")

    # --- Step 2: Timer check (stub — timers not yet implemented) ---
    # TODO: check expired active_timers and auto-advance accordingly

    # --- Step 3: Load current state ---
    flow = get_flow(session.active_flow)
    current_state = _get_state(flow, session.current_state)

    # Log the incoming request with full session context.
    _log_request(
        session_id=session_id,
        utterance=request.pilot_utterance,
        state_id=current_state.id,
        flow_slug=flow.slug,
        variables=session.variables,
        flags=session.flags,
    )
    # --- Guard: reject transmissions when the session is at a flow end state ---
    if session.current_state in flow.end_states and not session.flow_stack:
        raise ValueError(
            f"Flow '{flow.slug}' is complete (at end state '{session.current_state}'). "
            "Start a new session to continue training."
        )

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

    # --- Step 5b: Global emergency intercept (MAYDAY / PAN-PAN) ---
    # Applied before any state-specific routing so every pilot state in every
    # flow is interruptible without needing explicit ok_next entries in the YAML.
    selected_transition: Optional[Transition] = None
    match_reason = "no_match"
    used_bad_next = False

    if _EMERGENCY_RE.search(request.pilot_utterance) and session.active_flow != _EMERGENCY_FLOW:
        try:
            emg_flow = get_flow(_EMERGENCY_FLOW)
            if _EMERGENCY_ENTRY in emg_flow.states:
                selected_transition = Transition(
                    to=_EMERGENCY_ENTRY,
                    trigger=_EMERGENCY_RE.pattern,
                    is_emergency=True,
                    interrupt_flow=_EMERGENCY_FLOW,
                    label="Global MAYDAY / PAN-PAN emergency override",
                )
                match_reason = "emergency_override"
                trace.append(_trace(
                    "emergency_override",
                    f"Global MAYDAY/PAN-PAN — suspending '{flow.slug}' → "
                    f"'{_EMERGENCY_FLOW}'@'{_EMERGENCY_ENTRY}'",
                ))
        except KeyError:
            logger.warning("Emergency flow '%s' not loaded — MAYDAY not intercepted", _EMERGENCY_FLOW)

    # --- Step 5c: Global greeting intercept (optional courtesy call) ---
    # Only at initial-contact states, and only when the utterance does NOT also
    # match a real request trigger (an ok_next match takes precedence below).
    if (
        selected_transition is None
        and current_state.allow_greeting
        and _is_greeting(request.pilot_utterance)
    ):
        ok_trans, _ = select_transition(
            request.pilot_utterance, current_state.ok_next,
            session.variables, session.flags,
        )
        if ok_trans is None:
            trace.append(_trace(
                "greeting",
                f"Greeting-only call at '{current_state.id}' — replying 'pass your message'",
            ))
            _log_result(
                session_id=session_id, state_in=current_state.id,
                state_out=current_state.id, match_reason="greeting",
                auto_advanced=[], fallback_used=False, fallback_reason=None,
                say_template=_GREETING_REPLY, trace=trace,
            )
            return _build_stay_response(
                session, current_state, _GREETING_REPLY,
                request.pilot_utterance, "greeting", trace,
            )

    # --- Step 6: Match utterance against state candidates (if not already an emergency) ---
    if selected_transition is None:
        selected_transition, match_reason, used_bad_next = _select_pilot_transition(
            request.pilot_utterance,
            current_state,
            session.variables,
            session.flags,
            trace,
        )

    if match_reason == "regex_match":
        trace.append(_trace("regex_match", f"Trigger matched → '{selected_transition.to}' ({selected_transition.label or ''})"))
    elif match_reason == "ambiguous_first":
        trace.append(_trace("ambiguous", f"Ambiguous match — took first candidate '{selected_transition.to}'"))
    elif match_reason == "bad_next_fallback":
        trace.append(_trace("bad_next_fallback", f"No ok_next matched — using bad_next → '{selected_transition.to}'"))
    elif match_reason == "no_match":
        trace.append(_trace("no_regex_match", f"No trigger matched utterance '{request.pilot_utterance}'"))

    # --- Step 7: Readback evaluation (if required) ---
    # Emergency overrides skip readback entirely — MAYDAY / PAN-PAN always takes
    # priority regardless of whether the required fields are present.
    if (
        selected_transition is not None
        and match_reason != "emergency_override"
        and current_state.readback_required
        and current_state.readback_mode != "none"
    ):
        passed, missing, readback_report = check_readback(
            request.pilot_utterance,
            current_state.readback_required,
            current_state.readback_mode,
            session.variables,
        )
        if passed:
            trace.append(_trace("readback_pass", f"Readback OK — fields present: {current_state.readback_required}"))
        else:
            recognised = ", ".join(
                f"{r['field']}={r['expected']!r}→{'✓ ' + str(r['matched_via']) if r['matched'] else '✗ missing'}"
                for r in readback_report
            )
            trace.append(_trace("readback_fail", f"Readback missing fields: {missing} — using bad_next | {recognised}"))
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

    # --- Step 9: Validate selected state + handle flow interrupt ---
    if selected_transition.interrupt_flow:
        # This transition suspends the current flow and activates a new one.
        interrupt_slug = selected_transition.interrupt_flow
        try:
            interrupt_flow_def = get_flow(interrupt_slug)
        except KeyError:
            raise KeyError(
                f"Interrupt flow '{interrupt_slug}' not found. "
                f"Available: {list({})}"
            )
        entry_state_id = selected_transition.to
        if entry_state_id not in interrupt_flow_def.states:
            raise KeyError(
                f"Interrupt entry state '{entry_state_id}' not found in flow '{interrupt_slug}'"
            )
        push_flow(session, interrupt_slug, entry_state_id)
        trace.append(_trace(
            "flow_interrupt",
            f"Suspended '{flow.slug}' at '{current_state.id}' → "
            f"started '{interrupt_slug}' at '{entry_state_id}'"
        ))
        flow = interrupt_flow_def
        next_state = _get_state(flow, entry_state_id)
    else:
        if selected_transition.to not in flow.states:
            raise KeyError(
                f"Selected next state '{selected_transition.to}' not found in flow '{flow.slug}'"
            )
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

    # --- Flow completion / interrupt resume / next_flow chain ---
    # handle_flow_completion may:
    #   a) pop the interrupt stack (MAYDAY resume), or
    #   b) replace the active flow with next_flow (clearance → taxi, etc.)
    prev_flow_slug = flow.slug
    handle_flow_completion(session, flow)
    if session.active_flow != prev_flow_slug:
        flow = get_flow(session.active_flow)
        next_state = _get_state(flow, session.current_state)
        trace.append(_trace("flow_changed", f"Flow switched to '{flow.slug}' at '{next_state.id}'"))

        # If the entry state of the new flow is non-pilot, auto-advance through it
        # so the response always lands on a pilot state (same contract as the rest
        # of the engine).
        if next_state.role != "pilot":
            if next_state.say_template and atc_say_template is None:
                atc_say_template = next_state.say_template
            final_state_id, advanced2, auto_trans2 = advance_through_non_pilot(
                next_state.id, flow, session.variables, session.flags
            )
            auto_advanced_states.extend(advanced2)
            for sid in advanced2:
                trace.append(_trace("auto_advance", f"Auto-advanced through '{sid}'"))
                if atc_say_template is None:
                    intermediate = flow.states.get(sid)
                    if intermediate and intermediate.say_template:
                        atc_say_template = intermediate.say_template
            for auto_trans in auto_trans2:
                action_msgs = execute_actions(
                    auto_trans.on_exit_actions + auto_trans.on_enter_actions, session
                )
                for msg in action_msgs:
                    trace.append(_trace("action_execute", f"auto_advance: {msg}"))
            session.current_state = final_state_id
            next_state = _get_state(flow, final_state_id)

    # --- Step 12: Generate response ---
    # Use ATC speech collected during auto-advance; fall back to the final state's template.
    say_template = atc_say_template or next_state.say_template
    rendered = render_template(say_template, session.variables)
    expected = render_template(next_state.expected_pilot_template, session.variables)

    # Session is complete when it rests at a terminal end state (no further
    # chaining will happen — either the flow has no next_flow, or no_chain is set).
    final_flow = get_flow(session.active_flow)
    session_complete = (
        session.current_state in final_flow.end_states
        and not session.flow_stack
        and (final_flow.next_flow is None or session.no_chain)
    )

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

    _log_result(
        session_id=session_id,
        state_in=current_state.id,
        state_out=next_state.id,
        match_reason=match_reason,
        auto_advanced=auto_advanced_states,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        say_template=say_template,
        trace=trace,
    )

    return DecisionResponse(
        session_id=session_id,
        next_state_id=next_state.id,
        active_flow=session.active_flow,
        controller_say_template=say_template,
        controller_say_rendered=rendered,
        expected_pilot_template=expected,
        variables=dict(session.variables),
        flags=dict(session.flags),
        trace=trace,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        auto_advanced_states=auto_advanced_states,
        session_complete=session_complete,
        readback_report=readback_report,
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

    timeout_trans: Optional[Transition] = None

    if current_state.auto_advance_on_silence:
        # Find the first auto_transition with no trigger whose guard passes.
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
    elif current_state.readback_required and current_state.bad_next:
        # No explicit silence transition, but this is a readback state: after the
        # silence window the controller re-requests the readback by routing to the
        # correction prompt (bad_next[0]), which loops back to the readback state.
        timeout_trans = current_state.bad_next[0]
        trace.append(_trace(
            "readback_silence",
            f"No readback after silence window — re-requesting via '{timeout_trans.to}'",
        ))
    else:
        raise ValueError(
            f"State '{current_state.id}' does not have auto_advance_on_silence enabled "
            f"and is not a readback state"
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

    prev_flow_slug_timeout = flow.slug
    handle_flow_completion(session, flow)
    if session.active_flow != prev_flow_slug_timeout:
        flow = get_flow(session.active_flow)
        next_state = _get_state(flow, session.current_state)
        trace.append(_trace("flow_changed", f"Flow switched to '{flow.slug}' at '{next_state.id}'"))

        if next_state.role != "pilot":
            if next_state.say_template and atc_say_template is None:
                atc_say_template = next_state.say_template
            final_state_id, advanced2, auto_trans2 = advance_through_non_pilot(
                next_state.id, flow, session.variables, session.flags
            )
            auto_advanced_states.extend(advanced2)
            for sid in advanced2:
                trace.append(_trace("auto_advance", f"Auto-advanced through '{sid}'"))
                if atc_say_template is None:
                    intermediate = flow.states.get(sid)
                    if intermediate and intermediate.say_template:
                        atc_say_template = intermediate.say_template
            for auto_trans in auto_trans2:
                action_msgs = execute_actions(
                    auto_trans.on_exit_actions + auto_trans.on_enter_actions, session
                )
                for msg in action_msgs:
                    trace.append(_trace("action_execute", f"auto_advance: {msg}"))
            session.current_state = final_state_id
            next_state = _get_state(flow, final_state_id)

    say_template = atc_say_template or next_state.say_template
    rendered = render_template(say_template, session.variables)
    expected = render_template(next_state.expected_pilot_template, session.variables)

    final_flow_t = get_flow(session.active_flow)
    session_complete_t = (
        session.current_state in final_flow_t.end_states
        and not session.flow_stack
        and (final_flow_t.next_flow is None or session.no_chain)
    )

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
        active_flow=session.active_flow,
        controller_say_template=say_template,
        controller_say_rendered=rendered,
        expected_pilot_template=expected,
        variables=dict(session.variables),
        flags=dict(session.flags),
        trace=trace,
        fallback_used=False,
        fallback_reason=None,
        auto_advanced_states=auto_advanced_states,
        session_complete=session_complete_t,
    )

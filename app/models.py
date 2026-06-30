"""Core domain models for the PM radio training backend."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Flow definition models
# ---------------------------------------------------------------------------

class Guard(BaseModel):
    """Deterministic condition evaluation (no LLM)."""
    type: Literal["comparison", "flag_check", "variable_match"]
    name: str
    # For comparison / variable_match
    variable: Optional[str] = None
    operator: Optional[Literal["eq", "ne", "gt", "lt", "gte", "lte"]] = None
    value: Optional[Any] = None


class Action(BaseModel):
    """Side effect executed when entering/exiting a state."""
    type: Literal["set_variable", "set_flag", "call_service", "log"]
    target: str
    value: Optional[Any] = None


class Transition(BaseModel):
    """A single edge in the flow graph."""
    to: str
    trigger: Optional[str] = None          # Regex pattern; None = auto transition
    condition: Optional[Guard] = None      # Guard must pass to use this transition
    is_emergency: bool = False             # MAYDAY / PAN-PAN — checked first
    label: Optional[str] = None
    on_enter_actions: List[Action] = Field(default_factory=list)
    on_exit_actions: List[Action] = Field(default_factory=list)
    # When set, the engine suspends the current flow (push onto stack) and
    # activates the named flow at the state given by `to`.
    interrupt_flow: Optional[str] = None

    @model_validator(mode="after")
    def _compile_trigger(self) -> "Transition":
        """Pre-compile regex so bad patterns fail at load time."""
        if self.trigger is not None:
            try:
                re.compile(self.trigger, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid trigger regex '{self.trigger}': {exc}") from exc
        return self


class DecisionState(BaseModel):
    """One node in the decision flow graph."""
    id: str = ""                           # Populated by flow loader from dict key
    role: Literal["pilot", "atc", "system"]
    phase: Optional[str] = None
    name: str
    description: str = ""

    # Templates
    say_template: Optional[str] = None
    expected_pilot_template: Optional[str] = None

    # Readback
    readback_required: List[str] = Field(default_factory=list)
    readback_mode: Literal["none", "simple", "strict"] = "none"

    # Auto-advance
    auto_advance_on_silence: bool = False
    auto_advance_timeout_ms: int = 30000

    # When True, a greeting-only utterance ("Tower, callsign, good day") at this
    # state is answered with a "pass your message" prompt and the session stays
    # put.  Set on initial-contact pilot states.  Handled globally in the engine.
    allow_greeting: bool = False

    # Transitions
    ok_next: List[Transition] = Field(default_factory=list)
    bad_next: List[Transition] = Field(default_factory=list)
    auto_transitions: List[Transition] = Field(default_factory=list)

    # Radio display
    frequency: Optional[str] = None
    frequency_name: Optional[str] = None

    # Emergency state marker (informational; routing uses Transition.is_emergency)
    is_emergency: bool = False


class VariableDefinition(BaseModel):
    """Definition of a runtime variable."""
    name: str = ""                         # Populated by loader from dict key
    type: Literal["string", "number", "boolean", "enum", "list"]
    enum_values: Optional[List[str]] = None
    initial: Any
    mutable_by: Literal["action_only", "none"] = "action_only"


class FlagDefinition(BaseModel):
    """Definition of a boolean flag."""
    name: str = ""                         # Populated by loader from dict key
    initial: bool = False


class DecisionFlow(BaseModel):
    """Complete flow definition loaded from a YAML file."""
    slug: str
    schema_version: str
    name: str
    description: str = ""

    start_state: str
    end_states: List[str]

    variables: Dict[str, VariableDefinition] = Field(default_factory=dict)
    flags: Dict[str, FlagDefinition] = Field(default_factory=dict)

    states: Dict[str, DecisionState]

    entry_mode: Literal["main", "linear", "parallel", "interrupt"] = "main"

    # Optional: when the flow ends (end_states reached, stack empty), automatically
    # chain to this flow slug.  Variables already in the session carry over; any
    # variables/flags declared in the target flow but not yet present are
    # initialised from their YAML defaults.
    next_flow: Optional[str] = None

    @model_validator(mode="after")
    def _inject_keys(self) -> "DecisionFlow":
        """Inject dict keys as name/id fields so models are self-contained."""
        for key, var in self.variables.items():
            var.name = key
        for key, flag in self.flags.items():
            flag.name = key
        for key, state in self.states.items():
            state.id = key
        return self


# ---------------------------------------------------------------------------
# Session models
# ---------------------------------------------------------------------------

class RuntimeSession(BaseModel):
    """Mutable session state — stored in memory by session_id."""
    session_id: str
    created_at: str

    main_flow: str
    active_flow: str
    current_state: str

    # ICAO codes the session was created for. A city can host several airports,
    # so the human ``airport`` variable (a city name) is not enough to recompute
    # OSM routes later — these keep the exact aerodrome.
    airport_icao: Optional[str] = None
    destination_icao: Optional[str] = None

    flow_stack: List[str] = Field(default_factory=list)
    state_stack: List[str] = Field(default_factory=list)

    variables: Dict[str, Any] = Field(default_factory=dict)
    flags: Dict[str, bool] = Field(default_factory=dict)

    message_history: List[Dict] = Field(default_factory=list)
    decision_history: List[Dict] = Field(default_factory=list)

    active_timers: List[Dict] = Field(default_factory=list)

    # When True, handle_flow_completion will not follow next_flow links.
    no_chain: bool = False


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    flow_slug: str
    # Optional overrides applied on top of the YAML defaults at session creation.
    # Keys must match variable names declared in the flow (unknown keys are ignored).
    variables: Optional[Dict[str, Any]] = None
    # When True the engine will NOT follow next_flow links at end states.
    # Use this for single-phase practice so clearance-v1 stops at CLEARANCE_COMPLETE
    # instead of automatically chaining to taxi-v1.
    no_chain: bool = False
    # Optional ICAO codes resolved against the bundled airport dataset at session
    # creation: the station supplies real <position>_freq values (inventing any it
    # does not publish), the destination supplies the spoken destination city.
    # Explicit `variables` still win over anything resolved here.
    airport_icao: Optional[str] = None
    destination_icao: Optional[str] = None


class ResolvedAirport(BaseModel):
    icao: str
    city_en: str
    city_de: Optional[str] = None
    # Logical positions whose frequency was invented (no published data).
    invented_positions: List[str] = Field(default_factory=list)


class CreateSessionResponse(BaseModel):
    session_id: str
    flow_slug: str
    current_state: str
    variables: Dict[str, Any]
    flags: Dict[str, bool]
    # Pre-rendered expected pilot phrase for the starting state (if any)
    expected_pilot_template: Optional[str] = None
    # Resolved station / destination airports (names + which freqs were invented),
    # surfaced so the frontend can confirm the German name or let the user skip it.
    station_airport: Optional[ResolvedAirport] = None
    destination_airport: Optional[ResolvedAirport] = None


class DecisionRequest(BaseModel):
    pilot_utterance: str
    audio_metadata: Optional[Dict] = None


class TransitionTrace(BaseModel):
    type: str
    message: str


class ReadbackFieldDetail(BaseModel):
    """Per-field readback diagnostic, surfaced to the comm log for debugging."""
    field: str
    expected: str
    matched: bool
    # Which accepted form actually matched the utterance ("two five right",
    # "icao_phonetic", …), or None when the field was missed.
    matched_via: Optional[str] = None
    # All spoken forms that were accepted as a match for this field.
    accepted_forms: List[str] = Field(default_factory=list)
    note: Optional[str] = None


class DecisionResponse(BaseModel):
    session_id: str
    next_state_id: str
    # The slug of the flow that owns next_state_id.  Usually unchanged, but
    # can differ from the request's flow when next_flow chaining kicks in.
    active_flow: str

    controller_say_template: Optional[str]
    controller_say_rendered: Optional[str]
    expected_pilot_template: Optional[str]

    variables: Dict[str, Any]
    flags: Dict[str, bool]

    trace: List[TransitionTrace]
    fallback_used: bool
    fallback_reason: Optional[str]

    auto_advanced_states: List[str] = Field(default_factory=list)

    # True when the session has reached a terminal end state (no further chaining
    # will happen).  The frontend uses this to show the completion screen.
    session_complete: bool = False

    # Per-field readback diagnostic for the pilot state just evaluated (empty when
    # the state required no readback).  Lets the comm log show exactly which
    # elements were recognised and which were missing.
    readback_report: List[ReadbackFieldDetail] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation result model
# ---------------------------------------------------------------------------

class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    message: str
    state_id: Optional[str] = None


class FlowValidationResult(BaseModel):
    flow_slug: str
    valid: bool
    issues: List[ValidationIssue]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LoopDetectedError(RuntimeError):
    """Raised when the auto-advance traversal detects a cycle."""


class InvalidCandidateError(RuntimeError):
    """Raised when the selected next state is not a valid candidate."""

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
    type: Literal["string", "number", "boolean", "enum"]
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

    flow_stack: List[str] = Field(default_factory=list)
    state_stack: List[str] = Field(default_factory=list)

    variables: Dict[str, Any] = Field(default_factory=dict)
    flags: Dict[str, bool] = Field(default_factory=dict)

    message_history: List[Dict] = Field(default_factory=list)
    decision_history: List[Dict] = Field(default_factory=list)

    active_timers: List[Dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    flow_slug: str
    # Optional overrides applied on top of the YAML defaults at session creation.
    # Keys must match variable names declared in the flow (unknown keys are ignored).
    variables: Optional[Dict[str, Any]] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    flow_slug: str
    current_state: str
    variables: Dict[str, Any]
    flags: Dict[str, bool]
    # Pre-rendered expected pilot phrase for the starting state (if any)
    expected_pilot_template: Optional[str] = None


class DecisionRequest(BaseModel):
    pilot_utterance: str
    audio_metadata: Optional[Dict] = None


class TransitionTrace(BaseModel):
    type: str
    message: str


class DecisionResponse(BaseModel):
    session_id: str
    next_state_id: str

    controller_say_template: Optional[str]
    controller_say_rendered: Optional[str]
    expected_pilot_template: Optional[str]

    variables: Dict[str, Any]
    flags: Dict[str, bool]

    trace: List[TransitionTrace]
    fallback_used: bool
    fallback_reason: Optional[str]

    auto_advanced_states: List[str] = Field(default_factory=list)


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

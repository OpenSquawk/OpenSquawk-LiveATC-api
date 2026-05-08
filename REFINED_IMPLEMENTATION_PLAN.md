# PM Python Runtime: Refined Implementation Plan

**Version:** 2.0 (Refined with architectural decisions)  
**Status:** Ready for LLM Implementation  
**Last Updated:** 2026-05-08

---

## Executive Summary

Rebuild the PM radio training backend as a **stateful Python runtime** that maintains session state in the backend, calculates valid transitions from flow definitions, and uses regex-first pattern matching with LLM fallback for semantic routing.

**Key Architectural Decisions:**
- ✅ **Stateful backend** - Backend owns and persists session state (not frontend)
- ✅ **Regex-first routing** - Deterministic pattern matching before LLM
- ✅ **Emergency override system** - `is_emergency` flag ensures MAYDAY always prioritized
- ✅ **YAML flow definitions** - Externally stored, editable, version-controlled
- ✅ **Backend-calculated candidates** - Backend builds valid next states from flow definition
- ✅ **Two-phase action execution** - on_exit (old state) + on_enter (new state)
- ✅ **Flow stack with max depth 5** - Support nested interrupts with auto-resume

---

## Core Domain Model (Pydantic)

### Flow Definition Structure

```python
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Literal, Any
from enum import Enum

class Guard(BaseModel):
    """Deterministic condition evaluation (no LLM)"""
    type: Literal["comparison", "flag_check", "variable_match"]
    name: str  # "gates_clear", "frequency_set", "weather_ok"
    # Implementation: Guard evaluator resolves name to function
    
class Action(BaseModel):
    """Side effect when entering/exiting a state"""
    type: Literal["set_variable", "set_flag", "call_service", "log"]
    target: str  # "frequency", "ready_for_pushback", "request_weather"
    value: Optional[Any] = None
    # Implementation: Action executor resolves type + target

class Transition(BaseModel):
    """Single transition to a possible next state"""
    to: str  # Target state ID
    trigger: Optional[str] = None  # Regex pattern (null = auto transition)
    condition: Optional[Guard] = None  # Must pass to use this transition
    is_emergency: bool = False  # MAYDAY, PAN-PAN (checked first)
    label: Optional[str] = None  # For display: "correct readback" vs "incorrect"
    on_enter_actions: List[Action] = Field(default_factory=list)
    on_exit_actions: List[Action] = Field(default_factory=list)

class DecisionState(BaseModel):
    """One node in the decision flow"""
    id: str  # "REQUESTING_CLEARANCE", "READBACK_ACK"
    role: Literal["pilot", "atc", "system"]  
    # pilot: waiting for pilot input
    # atc: controller speaks, auto-advance when done
    # system: internal logic, auto-advance when done
    
    phase: Optional[str] = None  # "clearance", "taxi", "tower" (for organization)
    name: str  # "Requesting Clearance"
    description: str  # Detailed explanation
    
    # Templates for rendering
    say_template: Optional[str] = None  # What ATC says: "Lufthansa {callsign}, cleared to..."
    expected_pilot_template: Optional[str] = None  # What pilot should say
    
    # Readback requirement
    readback_required: List[str] = Field(default_factory=list)  # ["callsign", "runway"]
    readback_mode: Literal["none", "simple", "strict"] = "none"
    # none: no readback needed
    # simple: check if required fields are mentioned
    # strict: [future] phonetic matching, word order, etc.
    
    # Auto-advance configuration
    auto_advance_on_silence: bool = False  # If no pilot input for 30s, advance
    auto_advance_timeout_ms: int = 30000
    
    # Transitions (categorized by outcome type)
    ok_next: List[Transition] = Field(default_factory=list)  # Correct pilot input
    bad_next: List[Transition] = Field(default_factory=list)  # Incorrect pilot input
    auto_transitions: List[Transition] = Field(default_factory=list)  # No pilot input
    
    # Frequency info (for radio display)
    frequency: Optional[str] = None
    frequency_name: Optional[str] = None

class VariableDefinition(BaseModel):
    """Definition of a runtime variable"""
    name: str
    type: Literal["string", "number", "boolean", "enum"]
    enum_values: Optional[List[str]] = None
    initial: Any
    mutable_by: Literal["action_only", "none"] = "action_only"

class FlagDefinition(BaseModel):
    """Definition of a boolean flag"""
    name: str
    initial: bool = False

class DecisionFlow(BaseModel):
    """Complete flow definition (clearance, taxi, tower, etc.)"""
    slug: str  # "clearance", "taxi", "tower"
    schema_version: str  # "2.0" (for compatibility tracking)
    name: str  # "Clearance Flow"
    description: str
    
    start_state: str  # State ID where flow begins
    end_states: List[str]  # State IDs that end the flow
    
    # Variable and flag definitions
    variables: Dict[str, VariableDefinition] = Field(default_factory=dict)
    flags: Dict[str, FlagDefinition] = Field(default_factory=dict)
    
    # All states in this flow
    states: Dict[str, DecisionState]  # Key is state ID
    
    # Entry mode (how to enter this flow)
    entry_mode: Literal["main", "linear", "parallel", "interrupt"] = "main"
    # main: becomes the active flow, replaces previous
    # linear: runs to completion, then resumes previous
    # parallel: runs alongside other flows
    # interrupt: suspends active flow, resumes when this ends

class RuntimeSession(BaseModel):
    """Mutable session state"""
    session_id: str
    created_at: str  # ISO timestamp
    
    # Current position in flows
    main_flow: str  # The primary flow (e.g., "clearance")
    active_flow: str  # Current flow being executed (could be interrupt)
    current_state: str  # Current state ID
    
    # State stack for nested flows
    flow_stack: List[str] = Field(default_factory=list)  # [main_flow, interrupt_flow]
    state_stack: List[str] = Field(default_factory=list)  # [main_state, interrupt_state]
    
    # Runtime data
    variables: Dict[str, Any] = Field(default_factory=dict)  # Instance values
    flags: Dict[str, bool] = Field(default_factory=dict)
    
    # Message history
    message_history: List[Dict] = Field(default_factory=list)
    decision_history: List[Dict] = Field(default_factory=list)
    
    # Active timers for timeout transitions
    active_timers: List[Dict] = Field(default_factory=list)
    # [{state_id: "READBACK_ACK", type: "readback_timeout", fire_at: timestamp}]

# ============================================================================

class DecisionRequest(BaseModel):
    """What frontend sends to backend"""
    session_id: str
    pilot_utterance: str  # "Lufthansa 359 ready for pushback"
    audio_metadata: Optional[Dict] = None

class TransitionTrace(BaseModel):
    """How we arrived at a decision (debugging)"""
    type: str  # "regex_match", "guard_pass", "guard_fail", "ambiguous", "llm_call"
    message: str

class DecisionResponse(BaseModel):
    """What backend returns to frontend"""
    session_id: str
    next_state_id: str
    
    # What to display/speak
    controller_say_template: str  # "Lufthansa {callsign}, cleared to..."
    controller_say_rendered: str  # Filled in with variables
    expected_pilot_template: Optional[str]  # What comes next
    
    # State update
    variables: Dict[str, Any]  # Updated variable values
    flags: Dict[str, bool]  # Updated flag values
    
    # Debugging
    trace: List[TransitionTrace]
    fallback_used: bool
    fallback_reason: Optional[str]
    
    # What auto-advanced (if any)
    auto_advanced_states: List[str] = Field(default_factory=list)

```

---

## Flow Definition Format (YAML Example)

**File:** `flows/clearance-v1.yaml`

```yaml
slug: clearance
schema_version: "2.0"
name: "Initial Clearance"
description: "Pilot requests and receives initial clearance to push back from gate"

start_state: REQUESTING_CLEARANCE
end_states: ["CLEARANCE_COMPLETE"]

variables:
  callsign:
    type: string
    initial: ""
    mutable_by: action_only
  runway:
    type: enum
    enum_values: ["25L", "25R", "07L", "07R"]
    initial: ""
    mutable_by: action_only
  gates_clear:
    type: boolean
    initial: false
    mutable_by: action_only

flags:
  weather_checked:
    initial: false
  frequency_set:
    initial: false

states:
  REQUESTING_CLEARANCE:
    role: pilot
    phase: clearance
    name: "Requesting Clearance"
    description: "Pilot ready to request clearance"
    expected_pilot_template: "{{airline_name}} {{callsign}}, ready for pushback"
    
    frequency: "121.800"
    frequency_name: "Ground"
    
    ok_next:
      - to: READBACK_CLEARANCE
        trigger: "ready|request.*push|request.*clear"
        condition:
          type: flag_check
          name: gates_clear
        label: "Correct readback"
    
    bad_next:
      - to: REQUESTING_CLEARANCE
        trigger: ".*"  # Fallback: anything else is wrong
        label: "Incorrect input"
    
    auto_transitions: []

  READBACK_CLEARANCE:
    role: atc
    phase: clearance
    name: "Controller Issues Clearance"
    description: "ATC reads back the clearance; pilot should confirm"
    say_template: "{{callsign}}, cleared to {{runway}}, push back approved, advise ready"
    expected_pilot_template: "Readback {{runway}}"
    
    readback_required: ["runway"]
    readback_mode: simple
    auto_advance_on_silence: true
    auto_advance_timeout_ms: 30000
    
    ok_next:
      - to: CLEARANCE_COMPLETE
        trigger: ".*runway.*|.*two.*five.*|.*07.*"
        label: "Pilot readback correct"
        readback_mode: simple
    
    bad_next:
      - to: READBACK_CLEARANCE
        label: "Pilot didn't get it, try again"
    
    auto_transitions:
      - to: REQUESTING_READBACK
        condition:
          type: flag_check
          name: readback_timeout
        label: "No response, ask again"
        trigger: null  # Auto (no pattern)
        on_enter_actions:
          - type: set_flag
            target: readback_timeout
            value: false

  CLEARANCE_COMPLETE:
    role: system
    phase: clearance
    name: "Clearance Granted"
    description: "Flow ends, ready for pushback"
    
    on_enter_actions:
      - type: set_flag
        target: frequency_set
        value: true
      - type: log
        target: clearance_complete
    
    ok_next: []
    bad_next: []
    auto_transitions: []

  MAYDAY_HANDLER:
    role: system
    name: "Emergency - MAYDAY"
    description: "Immediate handling of emergency declaration"
    is_emergency: true
    say_template: "{{callsign}}, say nature of emergency"
    
    ok_next:
      - to: EMERGENCY_ASSISTANCE
        trigger: ".*"
        label: "Listen to emergency description"
```

---

## Decision Algorithm (Detailed)

When a pilot transmits, the backend:

```
INPUT: session_id, pilot_utterance

1. LOAD SESSION
   - Retrieve RuntimeSession from store
   - If not found → 404 error

2. CHECK ACTIVE TIMERS
   - Are any timers expired?
   - If yes → Advance through their auto_transitions first
   - Update session.current_state and variables

3. LOAD CURRENT STATE
   - current_state = session.current_state
   - Get state definition from active flow

4. IF ROLE == "SYSTEM" OR "ATC"
   - Auto-advance through non-pilot states until reaching pilot state
   - Build list of auto_advanced_states in trace
   - Loop detection: if state visited 5+ times → ERROR

5. BUILD CANDIDATES
   - Collect all transitions from current state:
     * ok_next
     * bad_next
     * auto_transitions (if timeout fired)
   
   - Filter by guard conditions (deterministic)
   - Also include entry states from other flows (if declared as interrupt candidates)
   - Result: candidate_states = [state IDs that could be next]

6. MATCH PILOT UTTERANCE TO CANDIDATES
   - Apply emergency override first:
     * For each candidate with is_emergency=true:
       if regex_match(candidate.trigger, pilot_utterance):
         return candidate (STOP, no further checking)
   
   - Match normal candidates:
     * For each candidate (not is_emergency):
       if regex_match(candidate.trigger, pilot_utterance):
         add to matching_candidates
   
   - Result:
     * 1 match → Use it
     * 0 matches → Go to LLM (step 8)
     * 2+ matches → Validator should catch, fallback to first match

7. IF READBACK REQUIRED
   - Get readback_required field from current state
   - Parse pilot_utterance to extract values
   - Compare to expected values in variables
   - Result:
     * All required fields present → Use ok_next
     * Some missing → Use bad_next
     * Ambiguous → LLM for classification

8. IF STILL AMBIGUOUS (0 matches, OR multiple matches, OR readback unclear)
   - Call LLM router with:
     * Current state context
     * Candidate states and their descriptions
     * Pilot utterance
     * Variables and flags
   - LLM returns: selected candidate state ID
   - Validate: is returned state in allowed candidates? If not → fallback

9. VALIDATE SELECTED STATE
   - Is it in candidates list?
   - Does it exist in the flow?
   - If invalid → Use bad_next or error

10. APPLY SIDE EFFECTS
    - Find all on_exit_actions of current state
    - Execute: set variables, set flags, call services, log
    - Find all on_enter_actions of selected state
    - Execute: set variables, set flags, call services, log

11. TRANSITION
    - session.current_state = selected_state
    - Advance past ATC/system states (see step 4)

12. GENERATE RESPONSE
    - Get state definition of final current_state
    - Render templates with variables
    - Return DecisionResponse with:
      * next_state
      * rendered templates
      * updated variables/flags
      * trace (how we got here)
      * auto_advanced_states list

13. SAVE SESSION
    - Persist updated session to store
    - Record decision in decision_history

OUTPUT: DecisionResponse
```

---

## Transition Matching Logic (Priority System)

**Critical algorithm for selecting which candidate to use:**

```python
def select_transition(
    pilot_utterance: str,
    candidates: List[Transition],
    variables: Dict,
    flags: Dict
) -> Tuple[Optional[Transition], str]:
    """
    Returns: (selected_transition, decision_reason)
    
    Priority:
    1. Emergency override (always first)
    2. Deterministic regex match (if unique)
    3. Guard conditions (filter candidates)
    4. LLM routing (fallback)
    """
    
    # STEP 1: Emergency override
    for candidate in candidates:
        if candidate.is_emergency:
            if regex_match(candidate.trigger, pilot_utterance):
                return candidate, "emergency_override"
    
    # STEP 2: Filter by guard conditions
    valid_by_guard = []
    for candidate in candidates:
        if candidate.condition:
            if evaluate_guard(candidate.condition, variables, flags):
                valid_by_guard.append(candidate)
        else:
            valid_by_guard.append(candidate)
    
    # STEP 3: Match triggers
    matching = []
    for candidate in valid_by_guard:
        if candidate.trigger:
            if regex_match(candidate.trigger, pilot_utterance):
                matching.append(candidate)
    
    # STEP 4: Decide
    if len(matching) == 1:
        return matching[0], "regex_match"
    elif len(matching) == 0:
        return None, "no_regex_match"  # Will use LLM
    else:
        # Ambiguity (should be caught by validator)
        logger.warning(f"Ambiguous transitions: {[c.to for c in matching]}")
        return matching[0], "ambiguous_first_match"
```

---

## Loop Detection

```python
def advance_through_system_and_atc(
    current_state_id: str,
    flow: DecisionFlow,
    variables: Dict,
    flags: Dict
) -> Tuple[str, List[str]]:
    """
    Auto-advance through system/atc states until reaching pilot state.
    
    Returns: (final_state_id, list_of_auto_advanced_state_ids)
    """
    
    visited = {}  # state_id -> visit_count
    advanced = []
    current = current_state_id
    max_iterations = 50  # Safety limit
    
    for iteration in range(max_iterations):
        state = flow.states[current]
        
        if state.role == "pilot":
            return current, advanced  # Stop at pilot state
        
        # Track visits
        visited[current] = visited.get(current, 0) + 1
        if visited[current] >= 5:
            raise LoopDetectedError(
                f"State {current} visited {visited[current]} times. "
                f"Loop: {' -> '.join(advanced)}"
            )
        
        # Find next transition
        next_trans = find_auto_transition(state, variables, flags)
        
        if not next_trans:
            return current, advanced  # No more auto transitions
        
        current = next_trans.to
        advanced.append(current)
    
    raise LoopDetectedError("Max iterations reached (50 auto-advances)")

def find_auto_transition(
    state: DecisionState,
    variables: Dict,
    flags: Dict
) -> Optional[Transition]:
    """Find the one valid auto-transition in this state."""
    
    valid = []
    for trans in state.auto_transitions:
        if trans.condition:
            if evaluate_guard(trans.condition, variables, flags):
                valid.append(trans)
        else:
            valid.append(trans)
    
    if len(valid) == 1:
        return valid[0]
    elif len(valid) == 0:
        return None
    else:
        # Ambiguity
        logger.warning(f"Multiple auto-transitions in {state.id}")
        return valid[0]
```

---

## Flow Validator

```python
def validate_flow(flow: DecisionFlow) -> List[ValidationError]:
    """Run at authoring time to catch design issues."""
    
    errors = []
    
    # Check 1: All state IDs referenced exist
    for state in flow.states.values():
        for trans_list in [state.ok_next, state.bad_next, state.auto_transitions]:
            for trans in trans_list:
                if trans.to not in flow.states:
                    errors.append(
                        ValidationError(
                            severity="error",
                            message=f"State {state.id} references non-existent state {trans.to}"
                        )
                    )
    
    # Check 2: Start and end states exist
    if flow.start_state not in flow.states:
        errors.append(ValidationError(severity="error", message="Start state not found"))
    for end in flow.end_states:
        if end not in flow.states:
            errors.append(ValidationError(severity="error", message=f"End state {end} not found"))
    
    # Check 3: Reachability (can you reach end states from start?)
    reachable = find_reachable_states(flow, flow.start_state)
    for end in flow.end_states:
        if end not in reachable:
            errors.append(
                ValidationError(
                    severity="warning",
                    message=f"End state {end} may not be reachable from start state"
                )
            )
    
    # Check 4: Ambiguous transitions
    for state in flow.states.values():
        if state.role == "pilot":
            # Check if multiple non-emergency transitions could match same input
            non_emergency = [t for t in state.ok_next + state.bad_next 
                           if not t.is_emergency]
            
            test_inputs = generate_test_inputs_from_triggers(non_emergency)
            for test_input in test_inputs:
                matching = [t for t in non_emergency 
                           if t.trigger and regex_match(t.trigger, test_input)]
                
                if len(matching) > 1:
                    errors.append(
                        ValidationError(
                            severity="warning",
                            message=f"State {state.id}: Ambiguous transitions for input '{test_input}'. "
                                    f"Transitions: {[t.to for t in matching]}. "
                                    f"Refine triggers to be mutually exclusive.",
                            state_id=state.id
                        )
                    )
    
    # Check 5: Deadlock detection (system/atc states with no auto-transitions)
    for state in flow.states.values():
        if state.role in ["system", "atc"] and not state.auto_transitions:
            errors.append(
                ValidationError(
                    severity="warning",
                    message=f"State {state.id} is {state.role} but has no auto-transitions. "
                            f"Pilot will wait forever."
                )
            )
    
    return errors
```

---

## Implementation Phases

### Phase 1: Foundation & Static Runtime (Week 1)

**Goals:**
- Define all Pydantic models
- Implement flow definition parsing (YAML → Pydantic)
- Implement GET /api/decision-flows/runtime (returns all flows)
- Implement POST /api/radio/session (create session)
- Implement flow validator
- Unit tests for models and validation

**Deliverables:**
- models.py (all Pydantic classes)
- flow_loader.py (YAML parsing)
- flow_validator.py (validation logic)
- /routes/flow_routes.py (GET /api/decision-flows/runtime)
- Test: Valid flows load, invalid flows catch errors

### Phase 2: Deterministic Routing (Week 2)

**Goals:**
- Implement regex-based trigger matching
- Implement guard evaluation (conditions)
- Implement candidate builder
- Handle readback evaluation
- Implement state auto-advance logic

**Deliverables:**
- trigger_matcher.py (regex matching + emergency override)
- guard_evaluator.py (condition logic)
- candidate_builder.py (build valid next states)
- readback_evaluator.py (parse + match required fields)
- auto_advance.py (loop detection + safe traversal)
- Test: Taxi/clearance flows with correct/incorrect inputs, auto-advance sequences

### Phase 3: Session Management & Side Effects (Week 2-3)

**Goals:**
- Implement RuntimeSession persistence (in-memory or simple file store for phase 1)
- Implement action execution (set_variable, set_flag, log)
- Implement flow stack (normal flow, interrupt flow, resume)
- Implement timer queue (for readback timeouts)

**Deliverables:**
- session_store.py (create, read, update sessions)
- action_executor.py (execute side effects)
- flow_orchestrator.py (manage flow stack, entry/exit modes)
- timer_manager.py (readback timeouts)
- Test: Create session, modify variables, interrupt and resume flow

### Phase 4: Decision API & Full Integration (Week 3)

**Goals:**
- Implement POST /api/radio/session/{id}/transmissions (main decision endpoint)
- Integrate all components (routing + actions + flow switching)
- Implement response generation (render templates, trace)
- Implement error handling (invalid candidates, guard failures, etc.)

**Deliverables:**
- /routes/decision_routes.py (POST /api/radio/session/{id}/transmissions)
- decision_engine.py (orchestrate the full 13-step algorithm)
- response_builder.py (format DecisionResponse)
- Test: End-to-end: clearance → taxi → tower flow, with readback, auto-advance, incorrect responses

### Phase 5: LLM Router (Week 4)

**Goals:**
- Add LLM provider abstraction (Claude, GPT-4, etc.)
- Implement LLM fallback when regex doesn't match
- Validate LLM responses against candidates
- Record LLM calls in trace

**Deliverables:**
- llm_provider.py (interface + implementation)
- llm_router.py (when to call LLM + validation)
- Test: Ambiguous inputs routed to LLM, responses validated

### Phase 6: Speech & Phrase Management (Week 4-5)

**Goals:**
- Implement TTS provider (text to speech)
- Implement template rendering with variable substitution
- Optional: Phrase normalization (callsigns, frequencies, runways)

**Deliverables:**
- tts_provider.py (speech generation)
- template_renderer.py (fill templates with variables)
- Optional: phrase_normalizer.py
- Test: Templates render correctly, TTS returns audio

---

## API Contracts

### GET /api/decision-flows/runtime

**Returns all flow definitions for frontend bootstrap**

```json
{
  "flows": {
    "clearance": {
      "slug": "clearance",
      "schema_version": "2.0",
      "name": "Initial Clearance",
      "states": { ... }
    },
    "taxi": { ... },
    "tower": { ... }
  }
}
```

### POST /api/radio/session

**Create a new session**

```
Request:
{
  "flow_slug": "clearance"
}

Response:
{
  "session_id": "uuid-xxx",
  "flow_slug": "clearance",
  "current_state": "REQUESTING_CLEARANCE",
  "variables": {},
  "flags": {}
}
```

### POST /api/radio/session/{session_id}/transmissions

**Submit pilot utterance, get decision**

```
Request:
{
  "pilot_utterance": "Lufthansa 359 ready for pushback"
}

Response:
{
  "session_id": "uuid-xxx",
  "next_state_id": "READBACK_CLEARANCE",
  "controller_say_template": "{{callsign}}, cleared to {{runway}}, push back approved",
  "controller_say_rendered": "Lufthansa 359, cleared to 25R, push back approved",
  "variables": {
    "callsign": "Lufthansa 359",
    "runway": "25R"
  },
  "flags": {
    "frequency_set": false
  },
  "trace": [
    { "type": "regex_match", "message": "Trigger 'ready|request.*push' matched" },
    { "type": "guard_pass", "message": "Condition 'gates_clear' evaluated true" },
    { "type": "action_execute", "message": "Set flag 'frequency_set' = true" }
  ],
  "fallback_used": false,
  "auto_advanced_states": []
}
```

---

## Flow Storage & Hot Reload

```
Directory structure:
flows/
  ├── clearance-v1.yaml
  ├── taxi-v2.yaml
  ├── tower-v1.yaml
  └── emergency-v1.yaml

Startup:
  1. Load all .yaml files from flows/ directory
  2. Parse each to Pydantic DecisionFlow
  3. Validate each flow
  4. Store in in-memory cache (Dict[str, DecisionFlow])
  5. Build indexes for quick lookup

Hot Reload:
  Option A: Watch filesystem for changes
    - Use watchdog library
    - On file change: reload, re-validate, update cache
  
  Option B: Manual reload endpoint
    - POST /api/admin/flows/reload
    - Re-load all flows from disk
  
  Recommendation: Both
    - Watchdog for development
    - Manual endpoint for safety in production
```

---

## Configuration & Deployment

**Environment variables:**

```
FLOWS_DIR="./flows"
LLM_PROVIDER="claude"  # or "gpt-4", etc.
LLM_MODEL="claude-opus-4-6"
SESSION_STORE_TYPE="memory"  # or "file", "postgres" in future phases
MAX_FLOW_STACK_DEPTH=5
MAX_AUTO_ADVANCE_HOPS=50
READBACK_TIMEOUT_MS=30000
LOG_LEVEL="info"
```

---

## Error Handling Strategy

| Error Type | Response | Trace |
|-----------|----------|-------|
| Session not found | 404 | Session ID doesn't exist |
| Current state missing | 500 | Flow definition corrupt |
| Guard crash | Use bad_next | Log error, note in trace |
| No candidates match + LLM fails | Use first bad_next or error | Record LLM failure |
| Invalid LLM response | Use first valid candidate | Record validation failure |
| Loop detected in auto-advance | 500 error | Return path that created loop |
| Readback parse fails | Use bad_next | Note parse error in trace |

---

## Non-Goals (Phase 1-4)

- Do NOT refactor frontend to send session_id only (compatibility mode persists)
- Do NOT support multiple simultaneous sessions per user (phase 5+)
- Do NOT persist flows to database (YAML files only)
- Do NOT implement sophisticated phrase normalization (simple presence check for readback)
- Do NOT support flow editing UI (manual YAML files)
- Do NOT support user authentication (assume single user)

---

## Success Criteria

✅ Existing frontend can run against Python backend without changes  
✅ Session state persists correctly across multiple transmissions  
✅ Readback evaluation works (correct input → ok_next, incorrect → bad_next)  
✅ Auto-advance through ATC/system states works (no infinite loops, max depth)  
✅ Flow interrupts work (MAYDAY mid-taxi correctly handled)  
✅ LLM is called only when deterministic routing is ambiguous  
✅ Flow validator catches design issues before deployment  
✅ All decisions are traceable (trace field shows why decision was made)  
✅ Every flow definition is externally stored (YAML files)  
✅ No hardcoded state machine logic (all from flow definitions)

---

## Future Phases (Not in Scope)

**Phase 5+:**
- Stateful HTTP (frontend sends session_id only)
- Persistent sessions (database)
- Multi-user support
- Flow versioning in database
- Flow editor UI
- Sophisticated readback (phonetic matching)
- Flow performance analytics
- LLM fine-tuning on real sessions


# PM Python Runtime Plan: Critical Review & Recommendations

**Reviewer Assessment**: Plan has strong architectural vision but contains critical ambiguities and contradictions that MUST be resolved before LLM implementation. Below are findings organized by severity.

---

## 🔴 CRITICAL ISSUES (Must resolve before coding)

### 1. **Frontend vs Backend Candidate Building - FUNDAMENTAL CONTRADICTION**

**The Problem:**
- The **API Contract** shows: Frontend sends `candidates` array to `/api/llm/decide`
- The **Decision Algorithm** (step 3): "Build candidate states from allowed transitions, active parallel flows, and valid interrupt flows"
- These are **mutually exclusive approaches**

**Why This Matters:**
- If frontend builds candidates → Backend can't validate the decision flow, just route among received candidates (weak architecture)
- If backend builds candidates → Frontend doesn't need to understand flow transitions, but must send `state_id` and current context (different API)

**Current Impact:**
- During phases 1-4, which is it?
- Phase 5 onwards, which is it?
- The algorithm and API contract contradict each other

**Recommendation for LLM:**
```
DECISION: Compatibility phase (1-4) is STATELESS ROUTING:
- Frontend sends: current_state_id, pilot_utterance, pre-computed candidates, variables, flags
- Backend role: Validate candidates haven't been tampered with, apply guards/conditions/triggers to filter them, select one, return decision
- Do NOT try to rebuild candidates server-side in phases 1-4
- Backend CandidateBuilder exists only for INTERNAL validation that frontend candidates are valid (not used in response)
- This aligns with "keep frontend working without changes"
```

---

### 2. **Stateless vs Stateful Architecture Confusion**

**The Problem:**
The plan says: "The first compatibility implementation may still accept stateless frontend context" (line 247)

But also: "Internally, the runtime should be designed around sessions" (line 247-248)

And the RuntimeSession model has `session_id`, `flow_stack`, `parallel_flows` (clearly stateful)

**Questions This Raises:**
- Phase 1-4: Does the backend maintain sessions or not?
- If stateless: How does it know "current flow" without frontend sending it?
- If stateful: How does frontend know its `session_id`? Do we create one on first call?
- If frontend sends full context each time (stateless), why build RuntimeSession at all in phases 1-3?

**Recommendation for LLM:**
```
CLARIFICATION: Two-tier approach
PHASE 1-4 (Compatibility):
- Backend is FUNCTIONALLY STATELESS: it computes decisions from context
- Frontend sends: state_id, variables, flags, candidates in EVERY request
- Backend does NOT persist session state
- BUT internally, RuntimeSession is BUILT temporarily for the request
- This allows phase 4+ to flip a switch and start persisting sessions

Why this works:
- Frontend needs zero changes for phases 1-4
- Phase 5+: Add session_id to requests, start persisting, frontend gradually stops sending full context
- Smooth migration path
```

---

### 3. **The "Auto-Advance Through ATC/System States Until Next Pilot State" Is Dangerously Underspecified**

**The Problem (Algorithm Step 12):**
"Advance through ATC and system states until the next pilot state"

This is complex and error-prone:
- What's the max depth? (prevent infinite loops)
- If an ATC state has a handoff (flow switch), do we auto-advance across flows?
- If a system state triggers a timer, do we stop and return the timer state, or keep advancing?
- If there are multiple exit paths from ATC/system states (e.g., two guards on transitions), which do we take?

**Risk:**
- Easy to create infinite loops: System→ATC→System→ATC→System...
- Easy to skip important states: System auto-advance might change the scenario in ways the frontend doesn't know
- Hard to debug: Frontend doesn't see intermediate auto-advanced states

**Current Mention:**
Document says "max-hop limits, visited-state detection, and traceable loop errors" (line 423) but doesn't specify HOW

**Recommendation for LLM:**
```
SPECIFICATION NEEDED - Add to phase 2 implementation:

auto_advance_config:
  max_hops: 10          # Absolute limit
  loop_detection: "visited_state_set"  # Track visited state IDs, error if repeat
  stop_conditions:
    - When reaching any PILOT role state
    - When reaching a timer_next transition (return timer state, don't fire yet)
    - When reaching a state with handoff (return handoff state to frontend, let frontend request flow switch)
    - When hitting loop detection (return error with trace of cycle)

Example flow:
  CLEARANCE (pilot) 
    → [LLM selects ok_next] 
  → READBACK_ACK (atc) 
    → [auto transition with action=update_frequency] 
  → CHECK_FREQ (system) 
    → [guard=freq_valid, triggers auto transition] 
  → READY_FOR_TAXI (pilot) [STOP: pilot state reached]

Return to frontend:
  - next_state: "READY_FOR_TAXI"
  - auto_advanced_through: ["READBACK_ACK", "CHECK_FREQ"]
  - messages: [controller_say for READBACK_ACK, rendered READY_FOR_TAXI template]
```

---

### 4. **Undefined State Transition Structure Is Incoherent**

**The Problem:**
The state schema shows BOTH:
```json
"next": [{ "to": "STATE_ID", "label": "string", "guard": "string" }],
"ok_next": [{ "to": "STATE_ID" }],
"bad_next": [{ "to": "STATE_ID" }],
"timer_next": [{ "to": "STATE_ID", "after_s": 10 }],
"auto_transitions": [],
"triggers": [],
"conditions": [],
"actions": [],
```

**This is confusing:**
- Are `next`, `ok_next`, `bad_next`, `timer_next` transitions, or are `auto_transitions` + `triggers` + `conditions` transitions?
- What goes in `auto_transitions`? (A list of what? Objects? Strings?)
- What's the difference between `guard` on `next` vs `conditions` array?
- What goes in `triggers`? (Regex patterns? Function names? Objects?)
- What goes in `actions`? (Variable updates? Side effects? Syntax undefined)

**Current Impact:**
- Impossible to implement without guessing the structure
- Frontend can't build correct candidates if it doesn't know the transition structure

**Recommendation for LLM:**
```
UNIFIED TRANSITION MODEL:

All transitions defined consistently in a Transition object:

class Transition(BaseModel):
    type: Literal["next", "ok", "bad", "timer", "auto", "interrupt", "return"]
    to: str  # target state ID
    label: Optional[str] = None  # for display ("correct readback" vs "incorrect")
    guard: Optional[Guard] = None  # deterministic condition (not LLM)
    after_ms: Optional[int] = None  # for timer transitions
    
class Guard(BaseModel):
    # Example: check if frequency is valid
    type: Literal["condition", "comparison", "regex"]
    name: str  # "freq_is_valid", "callsign_matches", "speed_below_250"
    # Implementation can be Python function reference, SQL, or DSL

# In DecisionState, single field:
class DecisionState(BaseModel):
    ...
    transitions: List[Transition]
    # NOT: next, ok_next, bad_next, timer_next, auto_transitions, triggers, conditions, actions
    
# Actions are part of the selected transition's side effects:
class Transition(BaseModel):
    ...
    on_enter: Optional[List[Action]] = None  # execute when entering target state
    on_exit: Optional[List[Action]] = None   # execute when leaving current state

class Action(BaseModel):
    type: Literal["set_variable", "set_flag", "call_service", "log"]
    parameters: dict  # type-checked based on action type
```

---

### 5. **Readback Evaluation Logic Is Severely Underspecified**

**The Problem:**
State schema shows: `"readback_required": ["callsign", "runway"]`

Algorithm step 6: "If the current state requires a readback, run the centralized ReadbackEvaluator"

**Missing Details:**
- If `readback_required: ["callsign", "runway"]`, does the evaluator check:
  - That the pilot said A callsign (any callsign)?
  - That the pilot said THE CORRECT callsign (matches context)?
  - Same for runway?
  
- What's the matching criteria?
  - Exact string match?
  - Phonetic match (Three-Two-Five = 325)?
  - Partial match (just the runway number)?

- What if pilot readback is incomplete? ("Roger" instead of "Runway 25R")?
  - Is this ok_next (correct) or bad_next (incorrect)?
  - Does it matter how many required fields are missing?

- What are valid readback values? Where do they come from?
  - From variables? (callsign = "DLH123")
  - From the state template? (rendered "Runway 25 Right")

**Current Impact:**
- Can't implement ReadbackEvaluator without this
- Frontend doesn't know if its pilot input will pass readback

**Recommendation for LLM:**
```
READBACK SPECIFICATION:

class ReadbackConfig(BaseModel):
    required_fields: List[str]  # ["callsign", "runway"]
    field_specs: Dict[str, ReadbackField]

class ReadbackField(BaseModel):
    name: str  # "callsign"
    source: Literal["variable", "constant"]
    variable_name: Optional[str] = None  # if source=variable
    expected_value: Optional[str] = None  # if source=constant
    matching: Literal["exact", "phonetic", "normalized_digit", "partial"]
    
    # Examples:
    # callsign: variable_name="callsign", matching="phonetic"
    #   → check if pilot said phonetic version of context.callsign
    # runway: variable_name="runway", matching="normalized_digit"
    #   → check if pilot said "two five right" and variable is "25R"
    # frequency: constant, expected_value="118.7", matching="normalized_digit"
    #   → check if pilot said "one one eight decimal seven"

class ReadbackResult(BaseModel):
    is_correct: bool
    matched_fields: List[str]
    missing_fields: List[str]
    trace: List[str]  # debug info per field

# Algorithm:
1. Load state.readback_config
2. Parse pilot_utterance with RadioPhraseNormalizer
3. For each field in readback_config:
   - Extract expected value from variable or constant
   - Check if utterance contains normalized version
   - Record match/miss
4. Return ReadbackResult
5. If all fields matched → ok_next
   If some fields matched → decide via guard/LLM
   If no fields matched → bad_next
```

---

### 6. **Variable and Flag Type System Is Missing**

**The Problem:**
State schema shows: `"variables": {}`, `"flags": {}`

Algorithm step 10: "Apply variable and flag updates through a controlled update mechanism"

LLM Rules: "modify variables outside an allowed schema" is forbidden

**What's Missing:**
- How are variables defined? With types?
- Can a variable be a string? Number? Object? List?
- Can a guard modify variables, or only actions?
- What's the difference between variables (mutable data) and flags (boolean state)?
- How do we enforce "outside allowed schema"? 
- What validates the update against the schema?

**Current Impact:**
- Can't build the "controlled update mechanism" without this
- Frontend doesn't know what variables exist or what values are valid

**Recommendation for LLM:**
```
VARIABLE SCHEMA:

class FlowVariables(BaseModel):
    definitions: Dict[str, VariableDefinition]
    
class VariableDefinition(BaseModel):
    name: str  # "callsign", "frequency", "runway"
    type: Literal["string", "number", "enum", "object"]
    enum_values: Optional[List[str]] = None  # if type=enum
    initial: Any  # initial value
    mutable_by: Literal["action_only", "guard", "both", "none"]
    # mutable_by="action_only" → guards can read, actions can write
    # mutable_by="guard" → only conditions can modify (unusual)
    # mutable_by="both" → conditions and actions can modify
    # mutable_by="none" → read-only, set only by system

# Example:
variables:
  definitions:
    callsign:
      type: string
      initial: ""
      mutable_by: action_only
    runway:
      type: enum
      enum_values: ["25L", "25R", "07L", "07R"]
      initial: ""
      mutable_by: action_only
    approach_count:
      type: number
      initial: 0
      mutable_by: action_only

# Update validation:
def apply_update(variables: FlowVariables, updates: Dict[str, Any]):
    for var_name, new_value in updates.items():
        definition = variables.definitions[var_name]
        
        if definition.type == "enum":
            assert new_value in definition.enum_values
        
        if definition.type == "number":
            assert isinstance(new_value, (int, float))
        
        # More type checks...
        
        variables[var_name] = new_value
```

---

### 7. **Flow Definition Persistence Model Undefined**

**The Problem:**
The plan mentions "Import or author initial production flows" (phase 6) but never specifies:
- What format are flows authored in? JSON? YAML? Graphical UI?
- How are flows stored? Database? Filesystem? Git?
- How are flows versioned?
- How are flows tested before deploying?
- Can flows be edited at runtime, or must they be redeployed?

**Recommendation for LLM:**
```
FLOW PERSISTENCE STRATEGY:

Source Format: YAML (author-friendly)
Storage: PostgreSQL or SQLite + Git
Deployment: Load from git on startup, hot-reload on git pull
Versioning: flow_version field in schema

Example filesystem:
flows/
  clearance-v1.yaml
  taxi-v2.yaml
  tower-approach-v1.yaml
  departure-v3.yaml

Database schema:
flows:
  id: UUID
  slug: string  # "clearance", "taxi", "tower-approach"
  version: int  # 1, 2, 3...
  schema_version: "2.0"  # Track schema compatibility
  definition: YAML/JSON (the full flow definition)
  created_at: timestamp
  author: string
  status: "draft" | "testing" | "production"

Version management:
- Keep all versions
- Active version marked in config
- Rollback by changing active version
- Never delete old versions (audit trail)

Testing:
- Unit tests for each flow (path coverage, guard validation)
- Integration tests for multi-flow scenarios
- Property-based tests (all states reachable?)
```

---

## 🟡 MAJOR CONCERNS (Should address)

### 8. **API Response Includes Trace That Doesn't Match Frontend Expectations**

The algorithm builds a rich trace (13 steps), but the compatibility response shows:
```json
"trace": {
  "calls": [],
  "fallback": { "used": false },
  "candidateTimeline": { "steps": [] },
  "autoSelection": null
}
```

**Questions:**
- What goes in `calls`? Array of what?
- What's `candidateTimeline.steps`? 
- When is `autoSelection` not null?
- The trace should show: guards evaluated, candidates filtered, LLM called/not called, readback result, auto-advanced states
- But the schema doesn't define any of this

**Recommendation:**
```
EXTENDED TRACE (for debugging):

class DecisionTrace(BaseModel):
    received_at: datetime
    input_candidates: List[str]  # candidate state IDs
    
    # Deterministic filtering
    guards_evaluated: List[GuardEvaluation]
    conditions_evaluated: List[ConditionEvaluation]
    triggers_evaluated: List[TriggerEvaluation]
    candidates_after_guards: List[str]  # filtered candidates
    
    # Readback phase
    readback: Optional[ReadbackTrace] = None
    candidates_after_readback: List[str]
    
    # LLM phase
    llm_called: bool
    llm_input: Optional[dict] = None  # candidates sent to LLM
    llm_output: Optional[dict] = None  # LLM response
    llm_decision_id: Optional[str] = None
    
    # Auto-advance
    auto_advanced_states: List[str]  # states traversed
    auto_advance_loop_detected: bool = False
    
    # Fallback
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    fallback_to_state_id: Optional[str] = None
    
    # Final decision
    selected_state_id: str
    decision_time_ms: float

# For frontend compatibility, can strip this down:
class CompatibilityTrace(BaseModel):
    # Only include what frontend actually needs for display/debug
    fallback: {"used": bool, "reason": Optional[str]}
    llm_called: bool
    auto_advanced: bool
    selected_state: str
```

---

### 9. **Error Handling Strategy Missing**

**Current mention:** "Every LLM decision must be validated. Invalid output becomes a traceable fallback, not an unchecked runtime decision." (line 329)

**Missing:**
- What IS a fallback? Return an error to frontend? Auto-select a default candidate? Suspend the session?
- What if NO candidates are valid? (all guards failed)
- What if a guard evaluates crashes? (bug in guard code)
- What if template rendering fails? (missing variable)
- What if readback parsing crashes? (malformed audio transcription)
- What if the LLM returns a state ID that doesn't exist?

**Recommendation:**
```
ERROR HANDLING STRATEGY:

Errors categorized by severity:

RECOVERABLE (fallback):
- LLM returns invalid candidate → use first valid candidate with trace note
- Readback parsing fails → skip readback check, use bad_next
- Guard crashes → log error, treat as guard failed (false), continue
- Template render fails → use fallback template, log error
→ Return decision with fallback: {"used": true, "reason": "..."}, trace shows it

UNRECOVERABLE (500 error to frontend):
- Flow definition missing
- Current state ID doesn't exist
- No transitions from current state AND no fallback
- Database connection failure
→ Return 500 error with error_id for support ticket

INVALID INPUT (400 error to frontend):
- Missing required fields (state_id, pilot_utterance)
- Candidates field empty
- state_id doesn't match flow schema
→ Return 400 error with validation message

Auto-fallback strategy:
1. If LLM returns invalid state → first candidate with lowest guard cost
2. If no candidates pass guards → bad_next if exists, else error
3. If readback required but parsing fails → bad_next
4. If multiple ATC/system auto-advances → first valid transition

All fallbacks are traceable and logged.
```

---

### 10. **Concurrency and Race Conditions**

**Missing:**
- What if frontend sends two decisions in quick succession?
- What if frontend sends before getting response from first decision?
- In stateful mode (phase 5+), can concurrent requests conflict?

**Recommendation:**
```
For phases 1-4 (stateless): No concurrency issues, requests are independent

For phases 5+ (stateful):
- Add session locking (pessimistic lock)
- One request in flight per session at a time
- Frontend must wait for response before sending next request
- Timeout after 30 seconds, release lock with error
- Or: use optimistic locking (version field)

Alternatively: Queue decisions per session and process sequentially
- This is safer but adds latency
```

---

### 11. **API Versioning Not Addressed**

What if the flow schema changes in phase 6?
- Older frontends still use old `/api/llm/decide` shape
- New flows might require new fields
- How do we support both?

**Recommendation:**
```
API Versioning strategy:

Option 1: Accept both old and new request shapes
- Detect old format (missing new required fields)
- Adapt to new internal format
- Return old response shape

Option 2: Separate endpoints
- /api/v1/llm/decide (stateless, compatibility)
- /api/v2/radio/session/{id}/transmissions (stateful, new)
- Both can coexist

Recommend Option 1 for phases 1-4 (backward compat automatic)
Transition to Option 2 in phase 5 (deprecate v1 after migration period)
```

---

## 🟢 STRONG POINTS (Keep these)

1. **Layered Architecture** - API, domain, services, infrastructure is clean
2. **Determinism First** - Guards before LLM is correct
3. **Centralized Normalization** - One place for phrase handling prevents bugs
4. **Incremental Phases** - Each phase adds a layer, validates before next
5. **Frontend Compatibility** - Doesn't break existing frontend
6. **Trace-First Design** - Debuggability built in from start
7. **Pydantic Models** - Type safety and validation from the start
8. **Non-Goals Clarity** - Avoids scope creep

---

## 📋 RECOMMENDED CHANGES TO SEND TO LLM

### Before Implementation Starts:

**1. Clarify Architecture (Pick ONE approach):**
```
COMPATIBILITY STATELESS APPROACH (Phases 1-4):
- Frontend sends: {state_id, pilot_utterance, candidates[], variables{}, flags{}}
- Backend role: Validate candidates, apply guards/conditions/triggers, route to one
- Backend does NOT build candidates (frontend does)
- Backend does NOT persist state
- Simplest and most compatible with existing frontend
- Later migrate to stateful (phase 5) with session_id

VS.

PURE STATEFUL APPROACH (Phases 1+):
- Frontend sends: {session_id, pilot_utterance, audio}
- Backend builds candidates from flow definition
- Backend manages full session state including variables/flags
- Frontend becomes very thin
- Requires rewriting frontend to send session_id
- Breaks compatibility in phase 1

RECOMMENDATION: Go with Compatibility Stateless for phases 1-4.
```

**2. Add Concrete Examples:**
```
Provide 3 complete flow definitions:
- A simple clearance flow (1-2 state transitions)
- A taxi flow with auto-advance (ATC speaks, system updates, back to pilot)
- A tower flow with handoff and readback

Each example should show:
- Complete state definitions with all fields populated
- Guard syntax with examples
- Action syntax with examples
- Variable definitions
- Transition types
```

**3. Unify Transition Model:**
- Remove: next, ok_next, bad_next, timer_next, auto_transitions, triggers, conditions, actions as separate fields
- Add: Single `transitions: List[Transition]` where Transition has type, guard, action

**4. Specify Auto-Advance Algorithm:**
```
def advance_through_system_and_atc(state):
    visited = set()
    current = state
    max_hops = 10
    advanced_states = []
    
    while current.role != "pilot" and len(advanced_states) < max_hops:
        if current.id in visited:
            raise LoopDetectedError(f"Loop detected: {visited}")
        
        visited.add(current.id)
        
        # Find next transition
        next_transitions = [t for t in current.transitions 
                           if evaluate_guard(t.guard) and 
                           not is_timer_transition(t) and 
                           not is_handoff_state(current)]
        
        if len(next_transitions) == 0:
            break  # Stop, return current state
        
        # If ambiguous (multiple valid), error or pick first
        if len(next_transitions) > 1:
            error: "Ambiguous auto-advance"
        
        current = flow.get_state(next_transitions[0].to)
        advanced_states.append(current.id)
    
    return current, advanced_states
```

**5. Define Variable/Flag Type System**
- Example: `variables: {callsign: {type: "string", mutable: "action_only"}}`
- Show how updates are validated

**6. Show Example Trace Output**
```
{
  "candidates_considered": ["READBACK_ACK", "FREQ_CHECK", "RADIO_CHECK"],
  "guards_filtered_to": ["READBACK_ACK", "FREQ_CHECK"],
  "readback_required": ["callsign"],
  "readback_result": {"matched": ["callsign"], "missing": []},
  "candidates_after_readback": ["READBACK_ACK", "FREQ_CHECK"],
  "llm_called": true,
  "llm_returned": "READBACK_ACK",
  "auto_advanced": ["FREQ_ACK", "READY_TAXI"],
  "selected_state": "READY_TAXI",
  "fallback": false
}
```

---

## 🎯 FINAL ASSESSMENT

**Overall Quality**: 7/10

**Why:**
- ✅ Good architectural vision
- ✅ Strong principles (determinism first, centralized logic)
- ✅ Incremental phases allow validation
- ❌ Critical ambiguities that will cause implementation rework
- ❌ API contract and algorithm contradict each other
- ❌ State transition model is incoherent
- ❌ Auto-advance behavior is dangerously vague
- ❌ Readback logic underspecified
- ❌ Type system for variables missing

**Can It Be Implemented?** Yes, but the LLM will need to make assumptions that might not match your intent.

**Recommendation**: Clarify the 5 critical issues above before sending to LLM. Add the concrete examples. The plan is 80% done - these fixes get it to 95% ready for implementation.

**Time Investment**: 2-3 hours to write clarifications + examples will save 40+ hours of implementation rework.


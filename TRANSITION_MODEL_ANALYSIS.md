# Transition Model Analysis & Critical Questions

**Your Clarifications Received:**
1. Backend maintains state (breaks frontend compatibility, but cleaner architecture)
2. Backend calculates valid candidates from flow definition (not frontend)
3. Candidates include entry nodes from other flows
4. Timer-based readback retry via state flag (not timer_next field)
5. Remove `next`, always use `ok_next` as fallback

---

## Current Transition Model Assessment

**Your Current Categorization:**

```
next / ok_next / bad_next / timer_next
  → Outcome-based transition categories

triggers
  → Linguistic pattern matching (which input maps to which candidate?)

conditions
  → Guard clauses (is this candidate allowed in current context?)

auto_transitions
  → System-automatic transitions (no pilot input needed)

actions
  → Side effects (what happens when entering this state?)
```

### ✅ **This Model Makes Sense**

**Why it's good:**
- Outcome-categorized transitions are elegant: you explicitly declare "if pilot is correct, go here; if incorrect, go there"
- Triggers separate linguistic matching from logical routing
- Conditions enable context-aware branching
- Auto-transitions handle ATC/system progression
- Actions are pure side effects, isolated from transition logic

**This is cleaner than open-ended candidate systems** because:
- The flow author explicitly models expected scenarios (correct/incorrect/default)
- The backend doesn't have to guess which transitions are valid
- Much easier to test and debug (explicit paths vs. computed paths)

---

## 🔴 Critical Questions to Answer Before Implementation

### **Question 1: Trigger Evaluation Timing**

**The Problem:**
Each transition can have a `trigger` (linguistic pattern). When do we evaluate triggers?

**Option A: Deterministic First**
```
1. Evaluate all triggers with regex/string matching against pilot_utterance
2. If exactly one trigger matches → select that transition
3. If multiple triggers match → call LLM to disambiguate among matched transitions
4. If zero triggers match → call LLM to choose from all valid transitions (guards/conditions only)
```

**Option B: LLM First**
```
1. Filter transitions by condition (deterministic guards)
2. Call LLM with all valid transitions including their triggers
3. LLM uses triggers as context to make better decision
```

**Option C: LLM Only**
```
1. Filter transitions by condition
2. Don't use triggers, just pass all valid transitions to LLM
3. LLM makes decision purely on semantic understanding
```

**My recommendation:** Option A (deterministic first)
- Faster (no LLM call if regex matches)
- More predictable (exact match → guaranteed outcome)
- Fallback to LLM only when ambiguous
- Better trace visibility

**Your preference?** Or different approach?

---

### **Question 2: Multiple Valid Transitions from Single State**

**Scenario:**
```
State: REQUESTING_CLEARANCE
Pilot says: "Lufthansa 359 ready for pushback"

ok_next (expecting readback):
  - to: READBACK_CLEARANCE
    trigger: "ready|ready_for|request.*clearance"
    condition: "weather_ok && gates_clear"

ok_next (alternate valid):
  - to: HOLDING_PATTERN  
    trigger: "holding.*pattern|wait"
    condition: "gates_clear"  # Different condition!

conditions evaluate:
  - weather_ok: TRUE
  - gates_clear: TRUE
  - So both transitions are valid!
```

**Question:** If multiple transitions have:
- Matching trigger AND passing condition
- Same category (both in ok_next)

How do we choose?

**A)** First match wins  
**B)** LLM chooses based on probability  
**C)** Score-based (e.g., trigger regex specificity)  
**D)** Error - author must ensure only one matches  

**My recommendation:** D or A
- D is safest (flow author's job to avoid ambiguity)
- A is pragmatic (deterministic, fast)
- B is complex (adds LLM call)

**Your preference?**

---

### **Question 3: The "No Pilot Input" 30-Second Readback Timeout**

**You said:** "Flag auf einem Node dass er wenn kein readback erfolgreich war automatisch nach 30s nachfragt"

**Need to clarify:**

**A) Timer lives in backend state?**
```
RuntimeSession.active_timers = [
  {state_id: "READBACK_ACK", type: "readback_timeout", fire_at: now + 30s}
]

After 30s:
  → Backend auto-transitions to the "ask_again" state
  → Returns to frontend: "ask readback again"
```

**B) Timer instruction sent to frontend?**
```
Response to frontend:
{
  state_id: "READBACK_ACK",
  template: "Lufthansa 359, readback please",
  timer: {after_ms: 30000, action: "retry_readback"}
}

Frontend waits 30s, then sends empty transmission
```

**Which approach?**

**My recommendation:** A (backend timer)
- Frontend doesn't need to manage timers
- More reliable (backend controls timing)
- Easier to resume if frontend crashes
- Simpler client code

**Your preference?**

---

### **Question 4: Auto-Transitions - Avoiding Infinite Loops**

**The problem:**
```
state_A (role: system):
  auto_transitions:
    - to: state_B
      condition: "condition_x == true"

state_B (role: system):
  auto_transitions:
    - to: state_A
      condition: "condition_x == false"  # But A set condition_x = true!

Loop: A → B → A → B → A ...
```

**How do we prevent this?**

**A) Max hop count**
```
max_auto_advance_hops = 10
if advanced_states.count > 10:
  ERROR: "Infinite auto-advance detected"
  return trace with path
```

**B) Visited state tracking (stricter)**
```
visited_states = set()
while current.role != "pilot":
  if current.id in visited_states:
    ERROR: "Loop detected in auto-advance"
    return state that started loop
  visited_states.add(current.id)
  ...
```

**C) Developer annotation (strictest)**
```
state_A:
  role: system
  max_auto_depth: 1  # Only one automatic hop allowed
  auto_transitions: ...
```

**D) Separate "deterministic" vs "loopable" states**
```
state_A:
  role: "system_deterministic"  # Must have exactly 1 transition
  auto_transitions:
    - to: state_B  # Required, no condition

state_B:
  role: "system_guarded"  # Can have multiple, guards prevent loops
  auto_transitions:
    - to: state_C
      condition: "X == true"
```

**My recommendation:** B (visited state tracking)
- Catches loops deterministically
- No arbitrary limits
- Clear error message
- Easy to debug with trace

**Your preference?**

---

### **Question 5: Actions - When Do They Execute?**

**Scenario:**
```
state: TOWER_LINEUP
transitions:
  ok_next:
    - to: TAKEOFF_CLEAR
      actions:
        - set_variable: runway_clear = true
        - call_service: request_airfield_data
        - log: "Transitioned to TAKEOFF_CLEAR"
```

**When do `actions` execute?**

**A) When leaving TOWER_LINEUP**
```
Select transition → Execute on_exit actions → Move to TAKEOFF_CLEAR
```

**B) When entering TAKEOFF_CLEAR**
```
Select transition → Move to TAKEOFF_CLEAR → Execute on_enter actions
```

**C) Both (on_exit from old state, on_enter to new state)?**
```
Execute old_state.on_exit → Move state → Execute new_state.on_enter
```

**D) On the transition itself (not tied to state)**
```
Transition is an object that knows what to do when selected
```

**My recommendation:** C (both phases)
- Most flexible (can have cleanup and setup)
- Clear separation
- Matches state machine patterns

**But also clarify:** Where in your schema do actions live?
- On the transition object? 
- On the state?
- Both?

---

### **Question 6: Backend-Calculated Candidates Including Other Flow Entry Points**

**You said:** "Entry nodes aus anderen Flows mit drin haben"

**Scenario:**
```
TAXI flow, state: NEAR_RUNWAY_25R
Next valid transitions:
  - ok_next to: LINEUP  (within TAXI flow)
  - auto_transition to: TOWER_HANDOFF (leaves TAXI, enters TOWER)
  - interrupt to: RADIO_CHECK (entry point of RADIO_CHECK flow)

Candidates for next decision:
  [LINEUP, TOWER_HANDOFF, RADIO_CHECK_START]
```

**Questions:**

A) **Can the pilot input trigger a flow switch, or only the backend?**
   - Pilot says something → triggers interrupt to RADIO_CHECK?
   - Or only auto-transitions/system can switch flows?

B) **Flow entry points - do they need special marking?**
   ```
   RADIO_CHECK flow:
     entry_points:
       - state_id: RADIO_CHECK_START
         trigger: "radio.*check|check.*radio"
   ```
   Or are ALL pilot-role states valid entry points?

C) **Flow stack - how deep can it go?**
   - Can you interrupt an interrupt? (TAXI → RADIO_CHECK → EMERGENCY_DIVERT)?
   - Max depth?

D) **Return behavior:**
   ```
   Pilot in RADIO_CHECK (interrupt)
   Completes readback
   → auto_transition to RADIO_CHECK_COMPLETE
   → resume_previous (go back to TAXI)
   ```
   Is this automatic, or does backend need to know when to return?

**My recommendation:**
- Only backend can initiate flow switches (not triggered by pilot input)
- All `role: pilot` states are valid entry points
- Track flow_stack, max depth = 5 (arbitrary, adjustable)
- Auto-resume when flow ends (if in interrupt mode)

**Your design?**

---

### **Question 7: The Readback Evaluation - Deferred Decision**

**You said:** "später zu entscheiden, auch je nach Zuverlässigkeit"

**This is wise.** But clarify for phase 1-2:

**Phase 1-2 approach (simple)?**
```
readback_required: ["callsign"]

Pilot says: "Lufthansa three five nine"
ReadbackEvaluator checks:
  - Does utterance contain ANY 3-digit number that matches "359"?
  - YES → ok_next
  - NO → bad_next
```

**Or more sophisticated?**
```
readback_required: ["callsign", "runway"]

Pilot says: "Lufthansa three five niner, runway two five right"
Check:
  - Contains airline name? YES
  - Contains tail number? YES (359)
  - Contains runway in correct format? YES (25R, two five right are equivalent)
  - All required fields present? YES → ok_next
  - Some missing? → bad_next
```

**My recommendation:** Start simple for phase 1
- Just check if required fields are mentioned
- Deferred: phonetic matching, word order sensitivity, confidence scoring

**Agree?**

---

## Summary: State Your Preferences

Please clarify:

1. **Trigger evaluation timing:** Deterministic first (regex) → LLM if ambiguous?
2. **Multiple valid transitions:** First match wins, or author ensures unique?
3. **Readback timeout:** Backend timer queue, or frontend waits and retries?
4. **Loop detection:** Visited-state tracking with error?
5. **Actions execution:** On transition selection, or on state entry/exit?
6. **Flow switching:** Backend-only, or pilot can trigger via input?
7. **Readback phase 1:** Simple presence check, defer sophisticated matching?

Once these are answered, the transition model is ready to code.


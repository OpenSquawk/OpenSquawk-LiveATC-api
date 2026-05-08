# Transition Matching Priority - Recommendation

## Your Statement
"It should always be only one valid transition. But MAYDAY should be prioritized. Should we add a check to the flow validator or does MAYDAY automatically win because it only matches on 'mayday'?"

## The Problem Case
```
State: REQUESTING_CLEARANCE

Pilot inputs different utterances:

Case 1: "Lufthansa three five nine ready for pushback"
  → READBACK trigger: "ready|ready_for|request.*push"  ✓ MATCHES
  → MAYDAY trigger: "mayday|emergency|help"            ✗ NO MATCH
  → Result: Only READBACK valid → No ambiguity

Case 2: "MAYDAY MAYDAY MAYDAY"
  → READBACK trigger: "ready|ready_for"                ✗ NO MATCH
  → MAYDAY trigger: "mayday|emergency"                 ✓ MATCHES
  → Result: Only MAYDAY valid → No ambiguity

Case 3: "Ready help I need help" (distressed pilot)
  → READBACK trigger: "ready"                          ✓ MATCHES
  → MAYDAY trigger: "help|emergency|distress"          ✓ MATCHES
  → Result: TWO valid transitions → AMBIGUITY! ❌
```

## Why Triggers Alone Aren't Enough

In aviation, **MAYDAY is not a competing transition - it's a safety override**. A pilot in genuine distress might say multiple things ("Ready, help, can't breathe, emergency"). We cannot rely on regex alone to prioritize correctly.

**Solution: Two-tier matching system**

---

## Recommended Design

### **Tier 1: Emergency Override (Always checked first)**

```python
class Transition(BaseModel):
    to: str
    trigger: str  # Regex pattern
    condition: Optional[Guard] = None
    
    # NEW: Emergency flag
    is_emergency: bool = False  # Mayday, Pan-Pan, etc.
    
class DecisionState(BaseModel):
    role: Literal["pilot", "atc", "system"]
    transitions: List[Transition]

# Example:
REQUESTING_CLEARANCE:
  transitions:
    - to: READBACK
      trigger: "ready|request.*clear"
      condition: gates_clear
      is_emergency: false
      
    - to: MAYDAY_HANDLER
      trigger: "mayday|pan.*pan|emergency|distress|help"
      is_emergency: true  # <-- Always prioritized
      
    - to: HOLDING
      trigger: "hold.*pattern|stand.*by"
      is_emergency: false
```

### **Selection Algorithm**

```python
def find_matching_transition(state, pilot_utterance, context):
    """
    1. Check emergency transitions first (safety-critical)
    2. Check normal transitions
    3. If ambiguous → validator should have caught this
    4. If none match → use LLM for semantic routing
    """
    
    # STEP 1: Emergency override (highest priority)
    for transition in state.transitions:
        if transition.is_emergency:
            if regex_match(transition.trigger, pilot_utterance):
                return transition  # Immediately return, no further checks
    
    # STEP 2: Normal transitions
    matching = [t for t in state.transitions 
                if not t.is_emergency 
                and regex_match(t.trigger, pilot_utterance)]
    
    # STEP 3: Decide what to do
    if len(matching) == 1:
        return matching[0]  # ✓ Exactly one valid
        
    elif len(matching) == 0:
        return None  # No regex match, escalate to LLM
        
    else:
        # len(matching) > 1 ← AMBIGUITY
        # This should have been caught by flow validator
        # Fallback: return first, log warning
        logger.warning(f"Ambiguous transitions for input '{pilot_utterance}': {[t.to for t in matching]}")
        return matching[0]
```

---

## Flow Validator Check

The validator should flag ambiguity **at authoring time**, so you fix it before deployment:

```python
def validate_flow(flow: DecisionFlow) -> List[ValidationError]:
    errors = []
    
    for state in flow.states.values():
        if state.role == "pilot":
            # For each state, check if two non-emergency transitions 
            # could match the same input
            
            non_emergency = [t for t in state.transitions if not t.is_emergency]
            
            # Generate test inputs based on triggers
            test_inputs = generate_test_inputs(non_emergency)
            
            for test_input in test_inputs:
                matching = [t for t in non_emergency 
                           if regex_match(t.trigger, test_input)]
                
                if len(matching) > 1:
                    errors.append(
                        ValidationError(
                            state_id=state.id,
                            message=f"Ambiguous transitions for input '{test_input}': {[t.to for t in matching]}. "
                                    f"Refine your triggers to make them mutually exclusive.",
                            matching_transitions=[(t.to, t.trigger) for t in matching]
                        )
                    )
    
    return errors

# When author runs validator:
# "State REQUESTING_CLEARANCE: Ambiguous transitions for 'ready help':
#   - READBACK (trigger: 'ready|request')
#   - MAYDAY (trigger: 'help|emergency')
#  
#  Fix: Refine triggers to be mutually exclusive, or mark one as is_emergency=true"
```

---

## Summary

| Case | Behavior |
|------|----------|
| **One normal transition matches** | ✓ Select it, no LLM needed |
| **One emergency + others match** | ✓ Select emergency, ignore others |
| **Zero transitions match** | → Use LLM for semantic routing |
| **Two normal transitions match** | ❌ Validator catches at authoring time, author must fix |

**Benefits:**
- ✅ MAYDAY always wins (safety)
- ✅ No arbitrary priority numbers
- ✅ Flow author forced to write good triggers
- ✅ Validator catches design issues early
- ✅ Self-documenting (`is_emergency` flag is explicit)

---

## Your Decision

**Do you want:**

**A) This system** (2-tier: emergency override + normal transitions with validator check)

**B) Simple priority field** (all transitions can have `priority: 1..100`, highest wins)

**C) Something else?**

Once you confirm, I'll write the unified refined plan with all your decisions baked in.


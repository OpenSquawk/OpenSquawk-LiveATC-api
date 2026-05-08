# LLM Handoff Checklist

## Was du dem LLM geben musst

### ✅ MUSS: Hauptdokument

**REFINED_IMPLEMENTATION_PLAN.md** 
- Das ist das komplette Blueprint
- Enthält: Models, Pydantic Schemas, YAML Format, Algorithm, Phasen, APIs
- Dies ist das einzige absolute Must-Have

---

### 📚 OPTIONAL: Context-Dokumente (für Verständnis)

Diese helfen dem LLM, die Design-Entscheidungen nachzuvollziehen:

**PLAN_REVIEW.md**
- Meine ursprüngliche Bewertung (7/10)
- Shows welche Probleme gelöst wurden
- Useful wenn LLM fragen hat "warum wurde X so entschieden"
- OPTIONAL aber empfohlen

**TRANSITION_PRIORITY_RECOMMENDATION.md**
- Erklärt why emergency override System
- OPTIONAL, nur falls LLM confused ist über is_emergency logic

**Oder:** Kurz-Zusammenfassung schreiben (siehe unten)

---

## Was du NICHT mehr brauchst

❌ Altes Plan-Dokument (2026-05-06-pm-python-runtime-contract.md) - ist veraltet  
❌ TRANSITION_MODEL_ANALYSIS.md - Fragen sind beantwortet, Antworten sind im refined plan

---

## Empfohlener Prompt für den LLM

Schreib einen klaren Prompt, z.B.:

```
You are implementing the PM radio training backend in Python.
Use this blueprint: [REFINED_IMPLEMENTATION_PLAN.md]

SPECIFIC INSTRUCTIONS:

1. TECHNOLOGY STACK:
   - Framework: FastAPI
   - Models: Pydantic v2
   - Session Storage: In-memory dictionary (Dict[str, RuntimeSession])
   - Flow Storage: YAML files in ./flows/ directory
   - Tests: pytest

2. SCOPE - Implement Phase 1 + Phase 2 (Weeks 1-2):
   - Phase 1: Foundation (models, flow loader, validator, routes)
   - Phase 2: Deterministic routing (regex, guards, readback)
   - Do NOT implement: LLM, TTS, Persistence (those are phases 5-6)

3. REQUIREMENTS:
   - All Pydantic models from section "Core Domain Model"
   - All methods from "Decision Algorithm" section
   - Flow validator with checks from "Flow Validator" section
   - Unit tests for each component
   - Example flow: Use the clearance-v1.yaml from the document

4. CODE STRUCTURE (from plan):
   app/api/
     decision_routes.py
     flow_routes.py
   app/domain/
     models.py
     flow_loader.py
   app/services/
     decision_engine.py
     trigger_matcher.py
     guard_evaluator.py
     readback_evaluator.py
     auto_advance.py
   app/infrastructure/
     flow_validator.py

5. IMPORTANT:
   - Keep emergency override (is_emergency flag) as specified
   - Loop detection: visited-state tracking, error at 5+ repeats
   - Readback phase 1: Simple (check if required fields mentioned)
   - All state transitions defined in YAML, not hardcoded
   - Comprehensive trace output for debugging

6. DELIVERABLES:
   - Working FastAPI server
   - GET /api/decision-flows/runtime (returns all flows)
   - POST /api/radio/session (create session)
   - POST /api/radio/session/{id}/transmissions (main decision endpoint)
   - Full test suite (pytest)
   - Proper error handling with meaningful messages
   - Clear code comments explaining decision algorithm

Start with Phase 1 (models + validation), then Phase 2 (routing logic).
```

---

## Alternativer: Kurz-Summary statt ganzer Docs

Wenn du nicht alle Dokumente geben willst, schreib ein Kurz-Summary:

```markdown
# Architecture Summary (für LLM)

## Key Decisions:
1. Stateful backend (backend owns session state)
2. Regex-first routing → LLM fallback (only if ambiguous)
3. Emergency override: is_emergency flag on transitions (MAYDAY always wins)
4. YAML flow definitions (externally stored, editable)
5. Loop detection: visited-state tracking, error at 5+ repeats
6. Two-phase actions: on_exit (old state) + on_enter (new state)
7. Flow stack: max depth 5, auto-resume when interrupt ends

## Flow Structure:
- States: pilot (wait for input), atc (speak), system (auto-advance)
- Transitions: ok_next, bad_next, auto_transitions
- Triggers: Regex patterns for input matching
- Guards: Deterministic conditions
- Actions: Side effects (set variables, flags)
- Readback: Simple phase 1 (field presence check)

## Algorithm:
1. Load session
2. Check timers
3. Auto-advance through system/atc states (loop detection)
4. Build candidates (valid next states)
5. Match pilot input to candidates (regex-first)
   - Emergency override (is_emergency=true checked first)
   - Normal matches (only one should be valid)
   - No match → LLM
6. Evaluate readback if required
7. Execute side effects (on_exit + on_enter actions)
8. Save session
9. Return decision response with trace

See REFINED_IMPLEMENTATION_PLAN.md for complete Pydantic models, YAML format, API contracts.
```

---

## Zusammenfassung: Minimal vs. Complete

### Minimal (nur essentiell):
- REFINED_IMPLEMENTATION_PLAN.md ✅
- Ein klarer, spezifischer Prompt (wie oben) ✅
- Fertig

### Complete (für mehr Context):
- REFINED_IMPLEMENTATION_PLAN.md
- PLAN_REVIEW.md
- TRANSITION_PRIORITY_RECOMMENDATION.md
- Ein klarer Prompt
- Best für Complex-Questions später

---

## Meine Empfehlung:

**Gib dem LLM:**

1. ✅ **REFINED_IMPLEMENTATION_PLAN.md** (Main)
2. ✅ **Einen Custom Prompt** (z.B. wie oben, mit Tech Stack + Phase Scope)
3. ⚠️ **Optional**: PLAN_REVIEW.md (nur wenn du das Dokument kurz referenzieren willst als "hier wurde diese Issue gelöst")

Das ist clean, nicht überfordert, aber vollständig.

---

## Tipps für LLM-Conversation:

1. **Immer Phase-weise arbeiten** - "Erst Phase 1 (models + validation), dann Phase 2"
2. **Test-first** - "Schreib tests während du development machst"
3. **YAML Examples** - "Zeige dir wie die flows in YAML aussehen sollen"
4. **Validation first** - "Implementier flow validator vor decision engine"
5. **Trace Output** - "Der trace muss debugging unterstützen, clear messages"


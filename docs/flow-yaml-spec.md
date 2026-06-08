# Flow YAML Specification — OpenSquawk LiveATC

**Schema version**: `2.0`
**Authoritative source**: `app/models.py` (Pydantic models are the canonical truth)

A flow YAML file describes one scenario segment (e.g., clearance delivery, ground taxi, tower,
departure). The engine loads all `flows/*.yaml` files at startup.

---

## Top-level structure

```yaml
slug: clearance-v1            # required · unique ID used in URLs and cross-flow references
schema_version: "2.0"         # required · always "2.0"
name: "IFR Clearance Delivery"  # required · human-readable display name
description: "..."            # optional · longer description shown in UI

start_state: INITIAL_CALL     # required · ID of the first state pilots enter
end_states:                   # required · one or more terminal state IDs
  - CLEARANCE_COMPLETE

entry_mode: main              # optional · default: main
                              #   main      – replace the current main flow
                              #   linear    – enter and return when complete
                              #   parallel  – run beside the current flow
                              #   interrupt – suspend current flow (used for emergency-v1)

next_flow: taxi-v1            # optional · slug of the flow to start automatically
                              #   when this flow reaches an end_state and the stack
                              #   is empty. Session-creation variables are forwarded.

variables:
  # see Variables section

flags:
  # see Flags section

states:
  # see States section
```

---

## Variables

Variables are named values that can be referenced in templates with `{{variable_name}}`.

```yaml
variables:
  callsign:
    type: string              # string | number | boolean | enum
    initial: "DLH39A"        # default value applied at session creation
    mutable_by: action_only   # action_only | none
    enum_values:              # only for type: enum
      - "option_a"
      - "option_b"
```

**Rules:**
- Variables declared here are merged with any values passed at session creation (`POST /api/radio/session`).
- Unknown keys passed at session creation are also stored (they carry over to subsequent flows).
- Variables are read-only during normal flow progression; only `set_variable` actions may change them.

---

## Flags

Flags are boolean-only variables, always stored separately.

```yaml
flags:
  clearance_issued:
    initial: false            # true | false
  readback_correct:
    initial: false
```

**Rules:**
- Changed via `set_flag` actions.
- Useful as guards in transition conditions.

---

## States

Each state has a role (`pilot`, `atc`, `system`) that determines how the engine handles it.

```yaml
states:
  STATE_ID:
    role: pilot               # pilot | atc | system
    phase: clearance          # optional · logical grouping (clearance, taxi, tower, …)
    name: "Human Name"        # required · shown in UI and debug panels
    description: "..."        # optional · longer explanation

    # --- ATC/system states only ---
    say_template: "{{callsign}}, cleared to {{destination}} via {{sid}}"
    # Template rendered with current variables and sent to TTS.
    # Use {{variable_name}} placeholders. No hardcoded frequencies — use frequency_name.

    # --- Pilot states only ---
    expected_pilot_template: "Cleared {{destination}}, {{sid}}, {{callsign}}"
    # Shown as a hint. Does NOT affect routing. Optional.

    readback_required:        # list of variable names that must appear in the utterance
      - squawk
      - initial_altitude
    readback_mode: simple     # none (default) | simple | strict
    # simple  – checks that the literal variable value appears in the utterance
    #           also accepts common phonetic variants (see Phonetic Readback)
    # strict  – reserved for stricter future matching
    # none    – no readback check performed

    # --- Radio display ---
    frequency_name: "Clearance Delivery"
    # PREFERRED: logical name resolved from airport data at runtime.
    # Allowed values (resolved automatically):
    #   "ATIS", "Clearance Delivery", "Ground", "Tower",
    #   "Departure", "Approach", "Center"

    frequency: "121.800"
    # AVOID unless there is no dynamic airport data.
    # Hardcodes the frequency and bypasses airport-specific resolution.

    # --- Transitions ---
    ok_next: [...]            # see Transitions section
    bad_next: [...]
    auto_transitions: [...]
```

---

## Transitions

### ok_next — pilot states

Used when the pilot's utterance matches expectations. The engine tries `ok_next` before `bad_next`.

```yaml
ok_next:
  - to: ATC_ISSUES_CLEARANCE
    trigger: "information|request.*clear|IFR|clearance|stand"
    # required · Python regex (re.IGNORECASE) · matched against pilot utterance
    # Use | for alternatives, .* for wildcards.
    # Use ".*" to match any utterance (always matches — useful for readback states).

    label: "Pilot called with required elements"
    # optional · human description for debug trace

    condition:
      # optional · guard that must pass for this transition to be eligible
      # see Guards section
```

### bad_next — pilot states

Used when no `ok_next` matches, or when a readback check fails.

```yaml
bad_next:
  - to: ATC_INITIAL_CALL_INCORRECT
    # ⚠️  MUST point to an ATC state, not back to a pilot state directly.
    # The ATC state provides spoken feedback via say_template, then
    # auto_transitions back to the pilot state.
    # Never loop bad_next → same pilot state — the pilot gets no feedback.

    label: "Incomplete call — controller corrects"
    # optional · no trigger needed (bad_next is selected by fallback, not regex)

    condition:
      # optional · guard (rarely needed on bad_next)
```

**Pattern for bad_next feedback (mandatory):**

```yaml
states:
  PILOT_STATE:
    role: pilot
    ok_next:
      - to: NEXT_STEP
        trigger: "..."
    bad_next:
      - to: ATC_PILOT_STATE_INCORRECT    # ← ATC state, never the pilot state itself

  ATC_PILOT_STATE_INCORRECT:
    role: atc
    say_template: "Negative, {{callsign}}, say again: ..."
    ok_next: []
    bad_next: []
    auto_transitions:
      - to: PILOT_STATE                  # ← loops back to pilot state AFTER ATC speaks
        label: "Correction given — pilot to retry"
```

### auto_transitions — ATC and system states

Used on ATC/system states to advance automatically (no pilot input needed).

```yaml
auto_transitions:
  - to: PILOT_LINEUP_READBACK
    label: "Line-up issued — awaiting readback"

    on_enter_actions:          # executed when entering the target state
      - type: set_flag
        target: lineup_clearance_issued
        value: true

    on_exit_actions:           # executed when leaving the current state
      - type: log
        target: lineup_issued
```

**Rules:**
- ATC states must have exactly one `auto_transition` (no `ok_next`, no `bad_next`).
- System end states have no transitions at all.
- Pilot states must NOT have `auto_transitions` (that field is ignored for pilot roles).

### Transition fields reference

| Field | Applies to | Type | Description |
|---|---|---|---|
| `to` | all | string | Target state ID |
| `trigger` | ok_next | regex string | Pattern matched against pilot utterance |
| `label` | all | string | Human description (trace/debug) |
| `condition` | all | Guard object | Guard that must pass |
| `on_enter_actions` | all | Action list | Actions run when entering `to` |
| `on_exit_actions` | all | Action list | Actions run when leaving current state |
| `interrupt_flow` | ok_next | string | Flow slug to push onto stack (for MAYDAY etc.) |
| `is_emergency` | ok_next | bool | Marks emergency bypass — set automatically by engine |

---

## Guards (conditions)

Guards are optional deterministic conditions. A transition is only eligible if its guard passes.

```yaml
condition:
  type: comparison          # comparison | flag_check | variable_match
  name: "guard_name"        # arbitrary label for debug

  # For type: comparison
  variable: squawk
  operator: eq              # eq | ne | gt | lt | gte | lte
  value: "7700"

  # For type: flag_check
  variable: clearance_issued
  # (checks that flag == true)

  # For type: variable_match
  variable: destination
  value: "Munich"           # exact string match
```

---

## Actions

Actions are side effects executed when a transition fires.

```yaml
on_enter_actions:
  - type: set_flag
    target: lineup_clearance_issued
    value: true

  - type: set_variable
    target: squawk
    value: "7700"

  - type: log
    target: clearance_complete      # emits a named event to the trace

  - type: call_service
    target: service_name            # reserved for future external integrations
```

---

## Phonetic readback

When `readback_required` contains a variable, the engine checks the pilot utterance for:
1. The **literal value** (e.g., `"FL150"`)
2. Automatically inferred **spoken variants** based on the value pattern:

| Pattern | Example | Spoken forms accepted |
|---|---|---|
| `^\d{3}\.\d+$` | `121.805` | `"one two one decimal eight zero five"` |
| `^FL\d+$` | `FL150` | `"flight level one five zero"` |
| `^\d{2}[LCR]?$` | `25L` | `"two five left"` |
| `^\d+$` (any integer) | `5000` | `"five thousand"` **and** `"five zero zero zero"` |
| `^\d+$` (4-digit) | `2341` | `"two three four one"` **and** `"two thousand three hundred forty one"` |

All forms are accepted simultaneously — a pilot saying either `"FL one five zero"` or `"flight level one five zero"` passes the check.

---

## MAYDAY / emergency handling

**Do not add MAYDAY to individual state `ok_next` lists.**

The engine globally intercepts any pilot utterance matching `mayday` or `pan pan` **before** state-specific routing. This fires regardless of which state or flow is active (except when already inside `emergency-v1`).

When intercepted:
- The current flow is pushed onto the stack (`interrupt`).
- The session enters `emergency-v1` at `MAYDAY_DECLARED`.
- When `emergency-v1` ends, the engine pops the stack and resumes the previous flow.

---

## Template syntax

Templates use `{{variable_name}}` placeholders.

```
"{{callsign}}, runway {{runway}}, line up and wait"
→ "DLH39A, runway 25L, line up and wait"
```

Rules:
- Unknown variable names render as empty string.
- Flags are accessible with `{{flag_name}}` (renders as `"true"` / `"false"`).
- No logic or conditionals — templates are pure substitution.

---

## Naming conventions

| Thing | Convention | Example |
|---|---|---|
| Flow slugs | `kebab-case-v1` | `clearance-v1`, `tower-v1` |
| State IDs | `SCREAMING_SNAKE_CASE` | `PILOT_READBACK`, `ATC_ISSUES_CLEARANCE` |
| Variable names | `snake_case` | `callsign`, `departure_freq` |
| Flag names | `snake_case` | `clearance_issued`, `airborne` |
| ATC correction states | `ATC_<PILOT_STATE_ID>_INCORRECT` | `ATC_PILOT_READBACK_INCORRECT` |
| End states | `<FLOW_SLUG_UPPER>_COMPLETE` | `CLEARANCE_COMPLETE`, `TOWER_COMPLETE` |

---

## Complete minimal example

```yaml
slug: radio-check-v1
schema_version: "2.0"
name: "Radio Check"
description: "Pilot requests a radio check and receives a readability report"

start_state: PILOT_RADIO_CHECK
end_states:
  - RADIO_CHECK_COMPLETE

entry_mode: linear           # returns to calling flow when complete

variables:
  callsign:
    type: string
    initial: "N12345"
    mutable_by: action_only

flags:
  check_complete:
    initial: false

states:

  PILOT_RADIO_CHECK:
    role: pilot
    phase: radio_check
    name: "Pilot Requests Radio Check"
    expected_pilot_template: "{{callsign}}, radio check"
    frequency_name: "Ground"

    ok_next:
      - to: ATC_RADIO_CHECK_REPLY
        trigger: "radio.*check|radio check"
        label: "Pilot requested radio check"

    bad_next:
      - to: ATC_RADIO_CHECK_INCORRECT
        label: "Request unclear — controller prompts"

    auto_transitions: []

  ATC_RADIO_CHECK_INCORRECT:
    role: atc
    phase: radio_check
    name: "ATC Prompts for Radio Check"
    description: "Controller asks pilot to say radio check"
    say_template: "Station calling, say again, radio check"
    frequency_name: "Ground"

    ok_next: []
    bad_next: []
    auto_transitions:
      - to: PILOT_RADIO_CHECK
        label: "Prompt given — pilot to retry"

  ATC_RADIO_CHECK_REPLY:
    role: atc
    phase: radio_check
    name: "ATC Reports Readability"
    say_template: "{{callsign}}, readability 5"
    frequency_name: "Ground"

    ok_next: []
    bad_next: []
    auto_transitions:
      - to: RADIO_CHECK_COMPLETE
        label: "Radio check complete"
        on_enter_actions:
          - type: set_flag
            target: check_complete
            value: true

  RADIO_CHECK_COMPLETE:
    role: system
    phase: radio_check
    name: "Radio Check Complete"
    description: "Radio check done. Readability confirmed."
    ok_next: []
    bad_next: []
    auto_transitions: []
```

---

## Common mistakes

| Mistake | Problem | Fix |
|---|---|---|
| `bad_next → same pilot state` | Pilot gets no ATC feedback, appears frozen | Route bad_next to an ATC correction state that auto_transitions back |
| `frequency: "121.800"` on every state | Hardcoded, breaks for non-EDDF airports | Use `frequency_name: "Ground"` |
| MAYDAY in `ok_next` | Redundant, already handled globally | Remove — the engine intercepts it before state routing |
| Pilot state with `auto_transitions` | Ignored, confusing | Only valid on atc/system states |
| ATC state without `auto_transitions` | Flow gets stuck — pilot can't advance | Add exactly one `auto_transition` |
| More than one `auto_transition` on ATC state | First matching one is taken; may be surprising | Keep one unconditional `auto_transition` unless guards are intentional |
| Trigger `".*"` on a readback state | Readback evaluator still runs — if readback fails, bad_next is used | This is correct; `".*"` just means "any utterance triggers this path if readback passes" |

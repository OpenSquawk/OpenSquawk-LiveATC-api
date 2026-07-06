# Telemetry-driven ATC — design & rollout

Status: **backbone merged to main; authority model CONFIRMED (backend-authoritative);
airborne PoC live in tower-v1 (PILOT_AWAIT_AIRBORNE); frontend silence
auto-advance wired (pm.vue timer → /timeout)**
Last updated: 2026-07-06

## Goal

Use live sim telemetry from the OpenSquawk Bridge (MSFS today, X-Plane next) to
make `/pm` ATC sessions proactive and state-aware: the controller reacts to what
the aircraft is actually doing (airborne, top-of-descent, established on the
localizer, runway vacated…) instead of only to pilot speech.

Hard requirement: **sessions flown without a bridge must behave exactly as they
do today.** Telemetry is purely additive.

---

## The one decision that is yours: authority model

Two engines exist:

- **Python backend** — *authoritative* for `/pm`. Owns session state; advances on
  pilot utterances (`/transmissions`) and silence (`/timeout`).
- **Frontend `communicationsEngine.ts`** — mirrors the backend cursor and renders
  text. It *also* contains a full, unused telemetry-trigger machinery.

**Recommended (and implemented): backend stays authoritative.** The frontend is
the telemetry *normaliser*, not a second decider:

```
Bridge ─POST /api/bridge/data─► Nuxt store (per user)          [existing]
pm.vue ─poll /api/bridge/live─► raw telemetry                  [existing]
   │  normalizeBridgeTelemetry() → sim-agnostic scalars
   ▼
pm.vue ─POST /session/{id}/telemetry─► backend                 [NEW, done]
   backend: merge telemetry, evaluate telemetry-gated
            auto_transitions (once + hysteresis), return the
            SAME shape as /transmissions
   ▼
pm.vue applyBackendDecision(response)  (one shared path)        [done]
```

Why: one source of truth (no drift — the class of bug the `cada00d` fix was
about); the frontend does sim-specific + geo work (it has coordinates, the
backend's `airports.min.csv` has none), the backend only compares plain scalars.

The alternative — *frontend decides on telemetry, then notifies the backend* —
reuses the existing frontend machinery faster but reintroduces dual authority.
**Not recommended.** If you pick it, say so and we retarget.

> **What you need to confirm:** keep backend-authoritative (yes/no). Everything
> below assumes yes. Nothing shipped so far forecloses the alternative.

---

## What is implemented (additive, all tests green)

### Backend (`OpenSquawk-LiveATC-api`)
- `TelemetryCondition` model + `Transition.telemetry` field (`app/models.py`).
- `RuntimeSession.telemetry` / `fired_telemetry` / `telemetry_pending` bookkeeping.
- `process_telemetry()` in `app/decision_engine.py`: merges the tick, fires the
  first matching telemetry-gated `auto_transition` on the current pilot state
  (honours `once` + `for_ms` hysteresis), reuses a `_finalize_transition()`
  helper mirroring the transmit path (flow interrupts, non-pilot auto-advance,
  `next_flow` chaining, completion). Idle ticks → `telemetry_fired=false`,
  state unchanged.
- `POST /api/radio/session/{id}/telemetry` (`app/routes/decision_routes.py`).
- `DecisionResponse.telemetry_fired`.
- **Isolation:** telemetry edges are skipped by silence-timeout and the silent
  auto-advance walk, so they *only* fire on real telemetry ticks.
- Tests: `tests/test_telemetry.py` (eval, idle no-op, threshold fire, `once`,
  sparse-merge, hysteresis, timeout-isolation). Full suite: **171 passed**.

### Frontend (`OpenSquawk`)
- `NormalizedTelemetry` contract + `sendTelemetry()` + `telemetry_fired`
  (`app/composables/useRadioBackend.ts`).
- `applyBackendDecision()` extracted from the transmit handler and shared, so
  telemetry-fired responses land through the identical code path (`pm.vue`).
- `normalizeBridgeTelemetry()` + change-detection + `forwardTelemetryToBackend()`
  wired into `pollBridgeTelemetry()` (`pm.vue`). Typecheck clean, **132 passed**.

**Dormant until a flow opts in:** no shipped flow has a telemetry transition yet,
so runtime behavior is unchanged. Adding the PoC below turns it on.

---

## The canonical telemetry contract (frozen — build both bridges to this)

Sim-agnostic. Keep `NormalizedTelemetry` (TS) and `TelemetryParameter` (Python)
in lock-step.

| key | unit | notes |
|---|---|---|
| `altitude_ft` | ft MSL | |
| `ias_kts` | kt | indicated |
| `gs_kts` | kt | groundspeed |
| `vs_fpm` | ft/min | +climb / −descent |
| `heading_deg` | ° | **currently true; needs mag-var before localizer/heading triggers** |
| `on_ground` | bool | |
| `distance_to_dest_nm` | nm | **not wired yet — needs an airport-coord source in pm.vue** |
| `distance_to_dep_nm` | nm | same |

---

## Recommended proof-of-concept: airborne → contact Departure

Today `tower-v1` hands off to Departure the instant the pilot *reads back* the
takeoff clearance (`PILOT_TAKEOFF_READBACK` →(".*")→ `ATC_AIRBORNE_HANDOFF`).
That is unrealistic — real Tower hands you off once you are climbing out. This
is the cleanest first telemetry win (boolean only, no geo, instantly testable by
flying).

Insert a wait-for-airborne pilot state between them:

```yaml
  PILOT_TAKEOFF_READBACK:
    # ok_next now points at the new wait state instead of ATC_AIRBORNE_HANDOFF
    ok_next:
      - to: PILOT_AWAIT_AIRBORNE
        trigger: ".*"

  PILOT_AWAIT_AIRBORNE:
    role: pilot
    phase: tower
    name: "Climbing out — awaiting handoff"
    description: "Tower hands off to Departure once the aircraft is airborne."
    auto_advance_on_silence: true
    auto_advance_timeout_ms: 20000
    ok_next:
      # No-bridge fallback: pilot reports airborne/passing.
      - to: ATC_AIRBORNE_HANDOFF
        trigger: "airborne|passing|climbing|with you"
    auto_transitions:
      # With a bridge: fire as soon as the wheels leave the ground (held 2s).
      - to: ATC_AIRBORNE_HANDOFF
        telemetry: { parameter: on_ground, operator: eq, value: false, for_ms: 2000 }
        label: "Airborne — Tower hands off to Departure"
      # No-bridge ultimate fallback: silence timeout also hands off.
      - to: ATC_AIRBORNE_HANDOFF
        label: "No report after silence window — hand off anyway"
```

Three exits (telemetry / pilot phrase / silence) = works identically with or
without a bridge. Requires the frontend to send `on_ground` — already wired.

> **Decision:** approve this PoC edit to `tower-v1.yaml` (and its expected-flow
> test update), or nominate a different first slice.

---

## Backlog (after PoC proves the loop)

1. **Airport-coordinate source in pm.vue** → compute `distance_to_dest_nm` /
   `distance_to_dep_nm` (haversine) → unlocks TOD descent, approach/tower handoff
   rings. Biggest unlock; needs coords (VATSIM/OpenAIP or a bundled table).
2. **Mag-var** on `heading_deg` → localizer-established → Tower.
3. Remaining cues from research: level-off, transition-level altimeter, speed
   reductions, runway-vacated → Ground, sector handoffs.
4. **Retire the frontend telemetry decider** in `communicationsEngine.ts` (now
   redundant) so there is provably one decider.
5. Ops hardening: drop `console.table` per-tick logging in `bridge/data.post.ts`;
   assert `session.user == bridgeToken.user` when forwarding (IDOR); privacy-notice
   line for live position data.

## Watch-outs (carried from review)
- Hysteresis + `once` are mandatory (done) or ATC machine-guns at thresholds.
- Never fire on stale telemetry (frontend 12s freshness gate — reused).
- Gate proactive speech on correct tuned frequency + not mid-transmission.
- Freeze the contract; keep SimConnect/dataref names at the bridge boundary only.

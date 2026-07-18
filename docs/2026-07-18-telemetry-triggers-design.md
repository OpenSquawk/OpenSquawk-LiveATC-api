# Telemetry triggers rollout — altitude/speed compliance, approach pacing, handoffs

Status: design approved (user: "arbeite direkt los"), implementing.
Builds on `docs/TELEMETRY-PLAN.md` (backbone + tower-v1 airborne PoC, both live).

## Goal

ATC reacts to what the aircraft actually does, wherever an instruction carries a
measurable target:

- **Assigned altitude/speed are monitored** — ATC waits for you to actually
  reach a cleared level before the next instruction, and calls out level busts
  / speed deviations.
- **Approach is paced by real progress** — vectors, ILS clearance and tower
  handoff fire from altitude/distance, not instantly after the readback.
- **Handoffs depend on position/speed** — Departure hands to Center when you
  approach the cleared level; Tower notices the vacated runway from groundspeed.

Hard requirement (unchanged): **no-bridge sessions keep working** — every
telemetry-gated wait state also exits via pilot phrase and silence timeout
(the frontend's silence timer already drives `/timeout`).

## Engine extensions (backend)

1. **Variable-resolved condition values.** `TelemetryCondition.value` may be a
   `"{{var}}"` template. Resolved against session variables at eval time;
   altitude strings parse as `"FL150"` → `15000`, `"5000"` → `5000`.
   Unresolvable → condition is False (never fires).
2. **`offset` field** (float, default 0) added to the resolved numeric value.
   Lets flows express "1000 ft before the cleared level"
   (`value: "{{climb_altitude}}", offset: -1000, operator: gte`) or tolerance
   bands (`offset: 300, operator: gt` = level bust).
3. **Per-edge bookkeeping keys.** `fired_telemetry` / `telemetry_pending` keys
   become `state::<index>::to` so two telemetry edges from one state to the same
   target (e.g. altitude OR distance) don't share `once`/hysteresis state.
4. **Backend computes distances.** Telemetry ticks now carry `lat`/`lon`.
   At merge time, if the session's `airport_icao`/`destination_icao` resolve to
   coordinates, the engine derives `distance_to_dep_nm`/`distance_to_dest_nm`
   (haversine). `data/airports.min.csv` gains `lat,lon` columns (OurAirports).
   The TS/Python parameter contract itself is unchanged — the two distance
   parameters simply become available.

## Flow changes

Pattern for every new wait state (mirrors `PILOT_AWAIT_AIRBORNE`):
telemetry edge(s) + pilot-phrase `ok_next` + `bad_next` roger + silence fallback.

- **departure-v1** — after `ATC_CLIMB_CONFIRMED`, new `PILOT_AWAIT_CLIMB`:
  - approaching cleared level (`altitude_ft ≥ climb_altitude − 1000`, 3 s) →
    new `ATC_CENTER_HANDOFF` ("contact center {{handoff_freq}}") →
    `PILOT_CENTER_FREQ_READBACK` → confirm → `DEPARTURE_COMPLETE`.
  - level bust callout (`altitude_ft > climb_altitude + 300`, 8 s, once) →
    `ATC_LEVEL_BUST` ("check altitude…") → back to the wait state.
  - New `handoff_freq` variable (frontend already sends it).
- **ifr-enroute-arrival-v1** — `PILOT_AWAIT_DESCENT_1` gates `ATC_DESCENT_2` on
  `altitude_ft ≤ descent_level_1 + 2000`; `PILOT_AWAIT_HANDOFF` gates
  `ATC_APPROACH_HANDOFF` on `distance_to_dest_nm ≤ 40` (or altitude ≤
  descent_level_2 + 1000 as second edge).
- **ifr-arrival-v1** —
  - `PILOT_AWAIT_DESCENT` gates `ATC_INTERMEDIATE_VECTOR` on
    `altitude_ft ≤ intermediate_altitude + 1500`; speed callout
    `ias_kts > speed_initial + 15` (12 s, once).
  - `PILOT_AWAIT_INTERCEPT` gates `ATC_ILS_CLEARANCE` on
    `altitude_ft ≤ intercept_altitude + 500` or `distance_to_dest_nm ≤ 18`;
    speed callout vs `speed_intermediate`.
  - `PILOT_AWAIT_ESTABLISHED` gates `ATC_TOWER_HANDOFF` on
    `distance_to_dest_nm ≤ 10`; phrase "established" still works.
- **ifr-tower-landing-v1** — `PILOT_RUNWAY_VACATED` gains a telemetry edge:
  `gs_kts < 30` held 8 s (only possible after rollout) → Tower initiates the
  vacated handoff even when the pilot forgets to report.
- **tower-v1** — unchanged (airborne handoff already live).

## Frontend (OpenSquawk)

- `NormalizedTelemetry` + `normalizeBridgeTelemetry()` gain `lat`/`lon`
  (from `PLANE_LATITUDE/LONGITUDE`, 0/0 no-data guard) and the change-detection
  signature includes rounded position so a moving aircraft keeps posting.
- No other change: distance math and all decisions live in the backend.
- Mag-var/heading triggers deliberately deferred (no heading-gated edge shipped).

## Testing

- Unit: value resolution (FL parsing, offset, unresolvable), per-index keys,
  distance derivation from lat/lon ticks.
- Flow-level: departure climb handoff (telemetry + phrase + silence exits),
  arrival wait states, vacated-by-groundspeed; existing walkthrough tests
  updated with the new pilot reports.
- E2E: WebSim `setup_approach` → ILS spawn 10 nm out → tower handoff fires from
  distance; takeoff → climb → center handoff.

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.airport_data import AirportInfo, resolve_for_session
from app.flow_loader import get_flow
from app.models import CreateSessionRequest, CreateSessionResponse, ResolvedAirport
from app.session_store import (
    create_session,
    delete_session,
    get_session,
    list_session_ids,
    save_session,
)
from app.taxi_route_service import TAXI_FLOW_SPECS, maybe_compute_taxi_route
from app.template_renderer import render_template


def _to_resolved(info: AirportInfo | None) -> ResolvedAirport | None:
    if info is None:
        return None
    return ResolvedAirport(
        icao=info.icao,
        city_en=info.city_en,
        city_de=info.city_de,
        invented_positions=[p for p, was in info.invented.items() if was],
    )

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radio", tags=["sessions"])


def _taxi_flow_in_chain(flow) -> str | None:
    """First taxi flow reachable from ``flow`` via next_flow links, or None.

    A full departure enters at clearance-v1, which chains clearance -> taxi-v1
    -> tower. The taxi clearance is issued in taxi-v1, so we look down the chain
    to find which taxi flow to compute for.
    """
    seen = {flow.slug}
    next_slug = flow.next_flow
    for _ in range(6):  # chains are short; cap guards against a cycle
        if not next_slug or next_slug in seen:
            return None
        if next_slug in TAXI_FLOW_SPECS:
            return next_slug
        seen.add(next_slug)
        try:
            next_slug = get_flow(next_slug).next_flow
        except KeyError:
            return None
    return None


def _compute_taxi_route_in_background(session_id: str, flow_slug: str, icao: str) -> None:
    """Resolve the OSM taxi route and write it onto the live session.

    Runs after the create response is sent, so the live Overpass call never
    blocks session creation. The taxi clearance is issued several R/T exchanges
    later, so the result is in place by the time the flow renders it; if the
    computation is slow or fails, the YAML default simply stands.
    """
    session = get_session(session_id)
    if session is None:
        return
    computed = maybe_compute_taxi_route(flow_slug=flow_slug, icao=icao, variables=session.variables)
    if not computed:
        return
    # Re-fetch so we write onto the freshest session snapshot, then persist.
    session = get_session(session_id)
    if session is None:
        return
    session.variables.update(computed)
    save_session(session)
    logger.info("TAXI ROUTE  session=%.8s  %s  →  %s", session_id, icao, computed.get("taxi_route"))


@router.post("/session", response_model=CreateSessionResponse, status_code=201)
def create_radio_session(body: CreateSessionRequest, background: BackgroundTasks):
    """Create a new training session for the given flow."""
    try:
        flow = get_flow(body.flow_slug)
    except KeyError as exc:
        logger.warning("SESSION CREATE  flow=%s  NOT FOUND", body.flow_slug)
        raise HTTPException(status_code=404, detail=str(exc))

    # Resolve airport names + real frequencies, if ICAO codes were supplied.
    # These act as defaults; any explicit `variables` from the caller win.
    resolution = resolve_for_session(body.airport_icao, body.destination_icao)
    merged_overrides = {**resolution.variables, **(body.variables or {})}

    # Compute the real OSM taxi route and use it in place of the YAML default.
    # Which taxi flow to compute for: the entry flow itself (taxi-only drill) or
    # one reached via the next_flow chain (full departure via clearance-v1). A
    # no_chain session never reaches a chained flow, so only its own counts.
    if flow.slug in TAXI_FLOW_SPECS:
        taxi_flow_slug: str | None = flow.slug
    elif body.no_chain:
        taxi_flow_slug = None
    else:
        taxi_flow_slug = _taxi_flow_in_chain(flow)

    # Skip only when the caller pinned taxi_route deliberately (kept out of the
    # frontend payload so this can run). Timing:
    #   - Entry flow IS the taxi flow: the clearance is seconds away, so compute
    #     synchronously behind the frontend's "calculating taxi route" spinner.
    #   - Taxi flow is down the chain: compute in the background; minutes of R/T
    #     precede it, so the result is in place in time.
    # Any failure keeps the flow's YAML default.
    autocompute = (
        "taxi_route" not in (body.variables or {})
        and bool(body.airport_icao)
        and taxi_flow_slug is not None
    )
    compute_sync = autocompute and flow.slug in TAXI_FLOW_SPECS
    if compute_sync:
        computed = maybe_compute_taxi_route(
            flow_slug=taxi_flow_slug,
            icao=body.airport_icao,
            variables={**{k: v.initial for k, v in flow.variables.items()}, **merged_overrides},
        )
        if computed:
            merged_overrides.update(computed)

    session = create_session(
        flow,
        variable_overrides=merged_overrides,
        no_chain=body.no_chain,
        airport_icao=body.airport_icao,
        destination_icao=body.destination_icao,
    )

    if autocompute and not compute_sync:
        background.add_task(
            _compute_taxi_route_in_background, session.session_id, taxi_flow_slug, body.airport_icao
        )

    # Render the expected pilot phrase for the start state so the frontend
    # can show the correct hint immediately without a round-trip transmission.
    start_state = flow.states.get(session.current_state)
    expected_pilot = render_template(
        start_state.expected_pilot_template if start_state else None,
        session.variables,
    )

    logger.info(
        "SESSION CREATE  session=%.8s  flow=%s  start=%s  vars=%s  flags=%s",
        session.session_id,
        session.active_flow,
        session.current_state,
        dict(session.variables),
        dict(session.flags),
    )
    return CreateSessionResponse(
        session_id=session.session_id,
        flow_slug=session.active_flow,
        current_state=session.current_state,
        variables=session.variables,
        flags=session.flags,
        expected_pilot_template=expected_pilot,
        station_airport=_to_resolved(resolution.station),
        destination_airport=_to_resolved(resolution.destination),
    )


@router.get("/session/{session_id}")
def get_radio_session(session_id: str):
    """Inspect the current state of a session."""
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return session.model_dump()


@router.delete("/session/{session_id}", status_code=204)
def delete_radio_session(session_id: str):
    """Terminate a session."""
    if not delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    logger.info("SESSION DELETE  session=%.8s", session_id)


@router.get("/sessions")
def list_sessions():
    """List all active session IDs (debug endpoint)."""
    return {"session_ids": list_session_ids()}

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
    # Timing strategy:
    #   - Entry flow IS a taxi flow (taxi-only training): the clearance comes
    #     within seconds, so compute synchronously and let the frontend show a
    #     "calculating taxi route" spinner over the create request.
    #   - Taxi flow reached by chaining (full departure): compute in the
    #     background; minutes of R/T happen first, so it's ready in time.
    # A caller-supplied taxi_route always wins; failure keeps the YAML default.
    autocompute = "taxi_route" not in (body.variables or {}) and bool(body.airport_icao)
    compute_sync = autocompute and flow.slug in TAXI_FLOW_SPECS
    if compute_sync:
        computed = maybe_compute_taxi_route(
            flow_slug=flow.slug,
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
            _compute_taxi_route_in_background, session.session_id, flow.slug, body.airport_icao
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

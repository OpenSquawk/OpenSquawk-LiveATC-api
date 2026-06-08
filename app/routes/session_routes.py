import logging

from fastapi import APIRouter, HTTPException

from app.flow_loader import get_flow
from app.models import CreateSessionRequest, CreateSessionResponse
from app.session_store import create_session, delete_session, get_session, list_session_ids
from app.template_renderer import render_template

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radio", tags=["sessions"])


@router.post("/session", response_model=CreateSessionResponse, status_code=201)
def create_radio_session(body: CreateSessionRequest):
    """Create a new training session for the given flow."""
    try:
        flow = get_flow(body.flow_slug)
    except KeyError as exc:
        logger.warning("SESSION CREATE  flow=%s  NOT FOUND", body.flow_slug)
        raise HTTPException(status_code=404, detail=str(exc))

    session = create_session(flow, variable_overrides=body.variables, no_chain=body.no_chain)

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

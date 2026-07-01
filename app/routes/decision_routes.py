import logging

from fastapi import APIRouter, HTTPException

from app.decision_engine import process_telemetry, process_timeout, process_transmission
from app.models import DecisionRequest, DecisionResponse, LoopDetectedError, TelemetryRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/radio", tags=["decisions"])


@router.post("/session/{session_id}/transmissions", response_model=DecisionResponse)
def transmit(session_id: str, body: DecisionRequest):
    """Submit a pilot utterance and receive an ATC decision."""
    try:
        return process_transmission(session_id, body)
    except KeyError as exc:
        logger.warning("✗ TRANSMIT  session=%.8s  404  %s", session_id, exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except LoopDetectedError as exc:
        logger.error("✗ TRANSMIT  session=%.8s  LOOP  %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Loop detected: {exc}")
    except ValueError as exc:
        logger.error("✗ TRANSMIT  session=%.8s  422  %s", session_id, exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("✗ TRANSMIT  session=%.8s  UNHANDLED  %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")


@router.post("/session/{session_id}/timeout", response_model=DecisionResponse)
def session_timeout(session_id: str):
    """Fire the silence timeout for the current pilot state.

    Call this when the frontend's ``auto_advance_timeout_ms`` timer expires
    without a pilot utterance.  The backend advances through the state's
    configured ``auto_transitions`` (trigger=None).
    """
    try:
        return process_timeout(session_id)
    except KeyError as exc:
        logger.warning("✗ TIMEOUT   session=%.8s  404  %s", session_id, exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except LoopDetectedError as exc:
        logger.error("✗ TIMEOUT   session=%.8s  LOOP  %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Loop detected: {exc}")
    except ValueError as exc:
        logger.warning("✗ TIMEOUT   session=%.8s  422  %s", session_id, exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("✗ TIMEOUT   session=%.8s  UNHANDLED  %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")


@router.post("/session/{session_id}/telemetry", response_model=DecisionResponse)
def session_telemetry(session_id: str, body: TelemetryRequest):
    """Apply a sim-bridge telemetry tick.

    The frontend forwards normalised telemetry (altitude, speed, distances, …)
    here on each poll. The backend updates the session's last-known telemetry
    and fires any telemetry-gated transition whose threshold is met, returning
    the same shape as ``/transmissions``. Idle ticks return ``telemetry_fired:
    false`` with the state unchanged. Flows without telemetry transitions are
    unaffected, so sessions flown without a bridge never call this at all.
    """
    try:
        return process_telemetry(session_id, body.telemetry)
    except KeyError as exc:
        logger.warning("✗ TELEMETRY session=%.8s  404  %s", session_id, exc)
        raise HTTPException(status_code=404, detail=str(exc))
    except LoopDetectedError as exc:
        logger.error("✗ TELEMETRY session=%.8s  LOOP  %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Loop detected: {exc}")
    except ValueError as exc:
        logger.warning("✗ TELEMETRY session=%.8s  422  %s", session_id, exc)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("✗ TELEMETRY session=%.8s  UNHANDLED  %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Internal error: {exc}")

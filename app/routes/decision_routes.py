from fastapi import APIRouter, HTTPException

from app.decision_engine import process_transmission
from app.models import DecisionRequest, DecisionResponse, LoopDetectedError

router = APIRouter(prefix="/api/radio", tags=["decisions"])


@router.post("/session/{session_id}/transmissions", response_model=DecisionResponse)
def transmit(session_id: str, body: DecisionRequest):
    """Submit a pilot utterance and receive an ATC decision."""
    try:
        return process_transmission(session_id, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except LoopDetectedError as exc:
        raise HTTPException(status_code=500, detail=f"Loop detected: {exc}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

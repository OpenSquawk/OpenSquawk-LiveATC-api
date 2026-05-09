from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.flow_loader import get_all_flows, reload_flows
from app.flow_validator import validate_flow

router = APIRouter(prefix="/api/decision-flows", tags=["flows"])


@router.get("/runtime")
def get_runtime_flows():
    """Return all loaded flow definitions for frontend bootstrap."""
    flows = get_all_flows()
    return {"flows": {slug: flow.model_dump() for slug, flow in flows.items()}}


@router.get("/runtime/{slug}")
def get_flow(slug: str):
    """Return a single flow definition."""
    flows = get_all_flows()
    if slug not in flows:
        raise HTTPException(status_code=404, detail=f"Flow '{slug}' not found")
    return flows[slug].model_dump()


@router.get("/runtime/{slug}/validate")
def validate_flow_route(slug: str):
    """Run the static validator on a flow and return issues."""
    flows = get_all_flows()
    if slug not in flows:
        raise HTTPException(status_code=404, detail=f"Flow '{slug}' not found")
    result = validate_flow(flows[slug])
    return result.model_dump()


@router.post("/admin/reload")
def reload_flows_route():
    """Hot-reload all flow files from disk."""
    from app.config import FLOWS_DIR
    flows = reload_flows(FLOWS_DIR)
    return {"reloaded": list(flows.keys()), "count": len(flows)}
